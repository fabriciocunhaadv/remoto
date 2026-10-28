// Servidor de sinalização: serve o visualizador web e repassa mensagens
// SDP/ICE entre o agente (PC remoto) e o visualizador (navegador).
const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { WebSocketServer } = require("ws");

const PORT = process.env.PORT || 8080;
const INDEX = path.join(__dirname, "server", "static", "index.html");
const rooms = new Map(); // id -> { password, agent, viewer }

const safeEqual = (a, b) => {
  const ha = crypto.createHash("sha256").update(String(a)).digest();
  const hb = crypto.createHash("sha256").update(String(b)).digest();
  return crypto.timingSafeEqual(ha, hb);
};
const send = (ws, obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

const server = http.createServer((req, res) => {
  if (req.url === "/" || req.url.startsWith("/?")) {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    fs.createReadStream(INDEX).pipe(res);
  } else if (req.url === "/health") {
    res.writeHead(200).end("ok");
  } else {
    res.writeHead(404).end("not found");
  }
});

const wss = new WebSocketServer({ noServer: true, maxPayload: 256 * 1024 });

server.on("upgrade", (req, socket, head) => {
  const url = req.url.split("?")[0];
  if (url !== "/ws/agent" && url !== "/ws/viewer") return socket.destroy();
  wss.handleUpgrade(req, socket, head, (ws) => {
    (url === "/ws/agent" ? onAgent : onViewer)(ws);
  });
});

function heartbeat(ws) {
  ws.alive = true;
  ws.on("pong", () => (ws.alive = true));
}

function onAgent(ws) {
  heartbeat(ws);
  let id = null;
  ws.on("message", (raw) => {
    let data;
    try { data = JSON.parse(raw); } catch { return; }
    if (data.type === "register" && id === null) {
      id = String(data.id);
      rooms.set(id, { password: String(data.password), agent: ws, viewer: null });
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
      const r = rooms.get(String(data.id));
      if (!r || !safeEqual(r.password, data.password ?? "")) {
        send(ws, { type: "error", message: "ID ou senha inválidos" });
        return ws.close();
      }
      if (r.viewer) {
        send(ws, { type: "error", message: "Já existe uma sessão ativa" });
        return ws.close();
      }
      room = r;
      room.viewer = ws;
      send(room.agent, { type: "viewer-joined" });
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
