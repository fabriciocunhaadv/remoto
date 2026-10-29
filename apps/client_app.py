"""RemoteDesk Cliente: o cliente instala e abre; mostra ID/senha e pede permissão.
Opcionalmente, o cliente habilita o acesso permanente para o técnico."""
import asyncio
import concurrent.futures
import json
import os
import platform
import secrets
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402
from config import server_url  # noqa: E402

SETTINGS_FILE = Path.home() / ".remotedesk.json"
OLD_ID_FILE = Path.home() / ".remotedesk_id"
ICE = ["stun:stun.l.google.com:19302"]
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sem 0/O, 1/I/L
STATUS = {
    "waiting": "Aguardando o técnico...",
    "connected": "Técnico conectado",
    "connected_unattended": "⚠ Técnico conectado (acesso permanente)",
}


def load_settings():
    try:
        st = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        st = {}
    if "id" not in st:
        try:
            st["id"] = OLD_ID_FILE.read_text().strip()
        except OSError:
            st["id"] = str(secrets.randbelow(900_000_000) + 100_000_000)
    st.setdefault("device_secret", secrets.token_urlsafe(24))
    st.setdefault("unattended", False)
    st.setdefault("name", platform.node())
    st.setdefault("org_code", "")
    st.setdefault("autostart", False)
    return st


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(settings))


def set_autostart(enabled):
    """Inicia com o Windows (chave Run do usuário atual)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, "RemoteDeskCliente", 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
        else:
            try:
                winreg.DeleteValue(k, "RemoteDeskCliente")
            except FileNotFoundError:
                pass


def already_running():
    """Impede duas cópias (duas conexões com o mesmo ID). Só no Windows."""
    if sys.platform != "win32":
        return False
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "RemoteDeskCliente-single-instance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


if already_running():
    tk.Tk().withdraw()
    messagebox.showinfo("RemoteDesk", "O RemoteDesk já está em execução.\nProcure o ícone perto do relógio (bandeja).")
    sys.exit(0)

settings = load_settings()
save_settings()
room_id = settings["id"]
password = "".join(secrets.choice(ALPHABET) for _ in range(8))

root = tk.Tk()
root.title("RemoteDesk Cliente")
root.geometry("380x520")
root.resizable(False, False)
status = tk.StringVar(value="Conectando ao servidor...")

tk.Label(root, text="Seu ID", font=("Segoe UI", 10)).pack(pady=(14, 0))
tk.Label(root, text=f"{room_id[:3]} {room_id[3:6]} {room_id[6:]}", font=("Segoe UI", 22, "bold")).pack()
tk.Label(root, text="Senha (acesso avulso)", font=("Segoe UI", 10)).pack(pady=(8, 0))
tk.Label(root, text=password, font=("Consolas", 18, "bold")).pack()
tk.Label(root, textvariable=status, fg="#2f7df6", wraplength=340).pack(pady=10)

box = tk.LabelFrame(root, text=" Acesso permanente do técnico ", padx=10, pady=8)
box.pack(fill="x", padx=14, pady=4)
v_unatt = tk.BooleanVar(value=settings["unattended"])
v_name = tk.StringVar(value=settings["name"])
v_code = tk.StringVar(value=settings["org_code"])
v_auto = tk.BooleanVar(value=settings["autostart"])
tk.Checkbutton(box, text="Permitir que o técnico acesse este\ncomputador SEM pedir autorização",
               variable=v_unatt, justify="left").pack(anchor="w")
tk.Label(box, text="Nome deste computador:").pack(anchor="w", pady=(6, 0))
tk.Entry(box, textvariable=v_name).pack(fill="x")
tk.Label(box, text="Código de ativação (fornecido pelo técnico):").pack(anchor="w", pady=(6, 0))
tk.Entry(box, textvariable=v_code, show="*").pack(fill="x")
if sys.platform == "win32":
    tk.Checkbutton(box, text="Iniciar com o Windows", variable=v_auto).pack(anchor="w", pady=(6, 0))
tk.Label(box, text="Você pode desativar a qualquer momento.\nSempre que o técnico conectar, este programa avisa.",
         fg="#666", font=("Segoe UI", 8), justify="left").pack(anchor="w", pady=(6, 0))

# ---------- bandeja do sistema: o X só esconde a janela; o acesso continua ativo ----------
tray = None


def show_window():
    root.deiconify()
    root.lift()
    root.focus_force()


def quit_app():
    if messagebox.askyesno("Encerrar RemoteDesk",
                           "Se encerrar, o técnico não conseguirá mais acessar este computador.\nEncerrar mesmo assim?"):
        if tray:
            tray.stop()
        os._exit(0)  # encerra tudo, inclusive a thread do agente


def hide_window():
    root.withdraw() if tray else root.iconify()  # sem bandeja (ex.: Mac): minimiza para o Dock


def make_tray():
    if sys.platform != "win32":
        return None  # no Mac a bandeja exige a thread principal, ocupada pelo Tk
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    img = Image.new("RGB", (64, 64), "#2f7df6")
    ImageDraw.Draw(img).ellipse((16, 16, 48, 48), fill="white")
    menu = pystray.Menu(
        pystray.MenuItem("Abrir", lambda: root.after(0, show_window), default=True),
        pystray.MenuItem("Encerrar", lambda: root.after(0, quit_app)))
    icon = pystray.Icon("RemoteDesk", img, "RemoteDesk Cliente", menu)
    icon.run_detached()
    return icon


tray = make_tray()
root.protocol("WM_DELETE_WINDOW", hide_window)
if "--minimized" in sys.argv:
    root.after(100, hide_window)

loop = asyncio.new_event_loop()
current = {"task": None}


def apply_settings():
    if v_unatt.get() and not v_code.get().strip():
        messagebox.showwarning("Código de ativação", "Digite o código de ativação fornecido pelo técnico.")
        return
    settings.update(unattended=v_unatt.get(), name=v_name.get().strip() or platform.node(),
                    org_code=v_code.get().strip(), autostart=v_auto.get())
    save_settings()
    try:
        set_autostart(settings["autostart"] and settings["unattended"])
    except OSError as e:
        messagebox.showwarning("Iniciar com o Windows", f"Não foi possível configurar: {e}")
    status.set("Aplicando...")
    if current["task"]:
        loop.call_soon_threadsafe(current["task"].cancel)  # o loop reconecta com as novas opções


tk.Button(box, text="Salvar", command=apply_settings).pack(anchor="e", pady=(6, 0))


async def approve():
    fut = concurrent.futures.Future()
    root.after(0, lambda: fut.set_result(messagebox.askyesno(
        "Permitir acesso", "Um técnico quer ver e controlar seu computador.\nPermitir?")))
    return await asyncio.wrap_future(fut)


def log(ev):
    root.after(0, lambda: status.set(STATUS.get(ev, ev)))
    if ev == "connected_unattended" and tray:
        try:
            tray.notify("Um técnico conectou a este computador.", "RemoteDesk")
        except Exception:
            pass


def register_extra():
    if settings["unattended"]:
        return {"unattended": True, "name": settings["name"], "orgKey": settings["org_code"],
                "deviceSecret": settings["device_secret"]}
    return {"deviceSecret": settings["device_secret"]}  # revoga um acesso permanente antigo


async def main_loop():
    ws_url = server_url().replace("https://", "wss://").replace("http://", "ws://")
    while True:  # reconecta se o servidor cair ou se as opções mudarem
        current["task"] = asyncio.ensure_future(
            agent.run(ws_url, room_id, password, ICE, log, approve, register_extra()))
        try:
            await current["task"]
        except asyncio.CancelledError:
            continue  # opções alteradas: reconecta já
        except Exception as e:
            log(f"Sem conexão ({e.__class__.__name__}). Tentando de novo...")
        await asyncio.sleep(5)


# a conexão só começa com a janela já em funcionamento (o Tk não aceita chamadas de outras threads antes disso)
root.after(200, lambda: threading.Thread(target=lambda: loop.run_until_complete(main_loop()), daemon=True).start())
root.mainloop()
