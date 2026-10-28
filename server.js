// Servidor de sinalização: serve o visualizador web, repassa mensagens SDP/ICE
// entre o agente (PC remoto) e o visualizador (navegador) e guarda a lista de
// computadores com acesso permanente.
//
// Variáveis de ambiente (configure no painel da Hostinger):
//   TECH_PASSWORD  senha do técnico (obrigatória para usar a lista de computadores)
//   ORG_KEY        código de ativação que o cliente digita para habilitar o acesso permanente
//   DATA_DIR       pasta onde a lista é salva (padrão: ~/.remotedesk-data)
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { WebSocketServer } = require("ws");

const PORT = process.env.PORT || 8080;
const TECH_PASSWORD = process.env.TECH_PASSWORD || "";
const ORG_KEY = process.env.ORG_KEY || "";
const DATA_DIR = process.env.DATA_DIR || path.join(os.homedir(), ".remotedesk-data");
const DB_FILE = path.join(DATA_DIR, "computers.json");
const INDEX = path.join(__dirname, "server", "static", "index.html");
const TOKEN_TTL = 30 * 24 * 3600 * 1000;

const rooms = new Map(); // id -> { password, agent, viewer, unattended }

// ---------- lista persistente de computadores ----------
let computers = {}; // id -> { name, secretHash, lastSeen }
try { computers = JSON.parse(fs.readFileSync(DB_FILE, "utf8")); } catch {}
let saveTimer;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(DB_FILE, JSON.stringify(computers));
    } catch (e) { console.error("falha ao salvar lista:", e.message); }
  }, 500);
}

// ---------- utilitários ----------
const sha = (s) => crypto.createHash("sha256").update(String(s)).digest("hex");
const eqExact = (a, b) => crypto.timingSafeEqual(
  crypto.createHash("sha256").update(String(a)).digest(),
  crypto.createHash("sha256").update(String(b)).digest());
// senha de sessão avulsa: ignora maiúsculas e espaços
const eqPassword = (a, b) => eqExact(String(a).trim().toUpperCase(), String(b).trim().toUpperCase());
const send = (ws, obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

// token do técnico sem estado: sobrevive a reinícios do servidor
const tokenKey = () => crypto.createHash("sha256").update("tok:" + TECH_PASSWORD).digest();
const signToken = (exp) => crypto.createHmac("sha256", tokenKey()).update(String(exp)).digest("hex");
function makeToken() {
  const exp = Date.now() + TOKEN_TTL;
  return `${exp}.${signToken(exp)}`;
}
function validToken(t) {
  if (!TECH_PASSWORD || typeof t !== "string") return false;
  const [exp, sig] = t.split(".");
  if (!exp || !sig || Number(exp) < Date.now()) return false;
  const good = signToken(exp);
  return sig.length === good.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(good));
}

const attempts = new Map(); // ip -> { n, reset }
function tooManyAttempts(ip) {
  const now = Date.now();
  const a = attempts.get(ip);
  if (!a || a.reset < now) { attempts.set(ip, { n: 1, reset: now + 10 * 60 * 1000 }); return false; }
  return ++a.n > 8;
}
const clientIp = (req) => (req.headers["x-forwarded-for"] || req.socket.remoteAddress || "").split(",")[0].trim();

function readJson(req) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (c) => { body += c; if (body.length > 4096) req.destroy(); });
    req.on("end", () => { try { resolve(JSON.parse(body || "{}")); } catch { resolve({}); } });
  });
}
const json = (res, code, obj) => {
  res.writeHead(code, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(obj));
};
const authed = (req) => validToken((req.headers.authorization || "").replace(/^Bearer /, ""));

// ---------- HTTP ----------
const server = http.createServer(async (req, res) => {
  const url = req.url.split("?")[0];
  try {
    if (req.method === "GET" && url === "/") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return fs.createReadStream(INDEX).pipe(res);
    }
    if (url === "/health") return res.writeHead(200).end(`ok pid=${process.pid} salas=${rooms.size}`);

    if (req.method === "POST" && url === "/api/login") {
      if (!TECH_PASSWORD) return json(res, 503, { error: "TECH_PASSWORD não configurada no servidor" });
      if (tooManyAttempts(clientIp(req))) return json(res, 429, { error: "Muitas tentativas. Aguarde alguns minutos." });
      const { password } = await readJson(req);
      if (!password || !eqExact(password, TECH_PASSWORD)) return json(res, 401, { error: "Senha incorreta" });
      return json(res, 200, { token: makeToken() });
    }

    if (url.startsWith("/api/computers")) {
      if (!authed(req)) return json(res, 401, { error: "Não autorizado" });
      const parts = url.split("/").filter(Boolean); // api, computers, :id, [rename]
      if (req.method === "GET" && parts.length === 2) {
        const list = Object.entries(computers).map(([id, c]) => ({
          id, name: c.name, online: !!rooms.get(id)?.unattended, lastSeen: c.lastSeen,
        }));
        return json(res, 200, list.sort((a, b) => a.name.localeCompare(b.name)));
      }
      const id = parts[2];
      if (id && computers[id]) {
        if (req.method === "DELETE" && parts.length === 3) {
          delete computers[id]; save();
          return json(res, 200, { ok: true });
        }
        if (req.method === "POST" && parts[3] === "rename") {
          const { name } = await readJson(req);
          if (!name) return json(res, 400, { error: "Nome vazio" });
          computers[id].name = String(name).slice(0, 60); save();
          return json(res, 200, { ok: true });
        }
      }
      return json(res, 404, { error: "Não encontrado" });
    }
    res.writeHead(404).end("not found");
  } catch (e) {
    console.error(e);
    json(res, 500, { error: "Erro interno" });
  }
});

// ---------- WebSocket ----------
const wss = new WebSocketServer({ noServer: true, maxPayload: 256 * 1024 });

server.on("upgrade", (req, socket, head) => {
  const url = req.url.split("?")[0];
  if (url !== "/ws/agent" && url !== "/ws/viewer") return socket.destroy();
  wss.handleUpgrade(req, socket, head, (ws) => (url === "/ws/agent" ? onAgent : onViewer)(ws));
});

function heartbeat(ws) {
  ws.alive = true;
  ws.on("pong", () => (ws.alive = true));
}
const fail = (ws, message) => { send(ws, { type: "error", message }); ws.close(); };

function register(ws, data) {
  const id = String(data.id).replace(/\D/g, "");
  if (!id) return fail(ws, "ID inválido"), null;
  const known = computers[id];
  const secret = typeof data.deviceSecret === "string" ? data.deviceSecret : "";
  const owner = known && secret.length >= 16 && eqExact(sha(secret), known.secretHash);

  if (data.unattended) {
    if (!ORG_KEY || !eqExact(data.orgKey || "", ORG_KEY)) return fail(ws, "Código de ativação inválido"), null;
    if (secret.length < 16) return fail(ws, "Registro inválido"), null;
    if (known && !owner) return fail(ws, "Este ID já pertence a outro computador"), null;
    computers[id] = { name: String(data.name || known?.name || id).slice(0, 60), secretHash: sha(secret), lastSeen: Date.now() };
    save();
  } else if (known) {
    if (!owner) return fail(ws, "Este ID já pertence a outro computador"), null;
    delete computers[id]; // o próprio cliente desativou o acesso permanente
    save();
  }
  rooms.set(id, { password: String(data.password), agent: ws, viewer: null, unattended: !!data.unattended });
  return id;
}

function onAgent(ws) {
  heartbeat(ws);
  let id = null;
  ws.on("message", (raw) => {
    let data;
    try { data = JSON.parse(raw); } catch { return; }
    if (data.type === "register" && id === null) {
      id = register(ws, data);
    } else if (id !== null) {
      const room = rooms.get(id);
      if (room && room.viewer) send(room.viewer, data);
    }
  });
  ws.on("close", () => {
    const room = id !== null && rooms.get(id);
    if (room && room.agent === ws) {
      if (room.viewer) room.viewer.close();
      rooms.delete(id);
      if (computers[id]) { computers[id].lastSeen = Date.now(); save(); }
    }
  });
}

function onViewer(ws) {
  heartbeat(ws);
  let room = null;
  ws.on("message", (raw) => {
    let data;
    try { data = JSON.parse(raw); } catch { return; }
    if (data.type === "join" && room === null) {
      const r = rooms.get(String(data.id).replace(/\D/g, ""));
      const byToken = data.token !== undefined;
      if (byToken) {
        if (!validToken(data.token)) return fail(ws, "Sessão do técnico expirada. Entre novamente.");
        if (!r || !r.unattended) return fail(ws, "Computador offline");
      } else if (!r || !eqPassword(r.password, data.password ?? "")) {
        return fail(ws, "ID ou senha inválidos");
      }
      if (r.viewer) return fail(ws, "Já existe uma sessão ativa");
      room = r;
      room.viewer = ws;
      send(room.agent, { type: "viewer-joined", unattended: byToken });
    } else if (room !== null) {
      send(room.agent, data);
    }
  });
  ws.on("close", () => {
    if (room && room.viewer === ws) {
      room.viewer = null;
      send(room.agent, { type: "viewer-left" });
    }
  });
}

// derruba conexões mortas (proxies costumam cortar conexões ociosas)
setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.alive) { ws.terminate(); continue; }
    ws.alive = false;
    ws.ping();
  }
}, 20000);

server.listen(PORT, () => console.log(`RemoteDesk na porta ${PORT}`));
