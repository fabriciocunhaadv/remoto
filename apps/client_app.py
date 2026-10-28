"""RemoteDesk Cliente: o cliente instala e abre; mostra ID/senha e pede permissão."""
import asyncio
import concurrent.futures
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

ID_FILE = Path.home() / ".remotedesk_id"
STATUS = {"waiting": "Aguardando o técnico...", "connected": "Técnico conectado"}


def get_id():
    try:
        return ID_FILE.read_text().strip()
    except OSError:
        i = str(secrets.randbelow(900_000_000) + 100_000_000)
        ID_FILE.write_text(i)
        return i


root = tk.Tk()
root.title("RemoteDesk Cliente")
root.geometry("340x260")
root.resizable(False, False)
room_id, password = get_id(), secrets.token_urlsafe(6)
status = tk.StringVar(value="Conectando ao servidor...")

tk.Label(root, text="Seu ID", font=("Segoe UI", 10)).pack(pady=(18, 0))
tk.Label(root, text=f"{room_id[:3]} {room_id[3:6]} {room_id[6:]}", font=("Segoe UI", 22, "bold")).pack()
tk.Label(root, text="Senha", font=("Segoe UI", 10)).pack(pady=(10, 0))
tk.Label(root, text=password, font=("Consolas", 18, "bold")).pack()
tk.Label(root, textvariable=status, fg="#2f7df6").pack(pady=14)
tk.Label(root, text="Informe o ID e a senha ao técnico.\nFeche esta janela para encerrar o acesso.",
         fg="#666", font=("Segoe UI", 8)).pack()


async def approve():
    fut = concurrent.futures.Future()
    root.after(0, lambda: fut.set_result(messagebox.askyesno(
        "Permitir acesso", "Um técnico quer ver e controlar seu computador.\nPermitir?")))
    return await asyncio.wrap_future(fut)


def log(ev):
    root.after(0, lambda: status.set(STATUS.get(ev, ev)))


def worker():
    ws_url = server_url().replace("https://", "wss://").replace("http://", "ws://")
    while True:  # reconecta se o servidor cair
        try:
            asyncio.run(agent.run(ws_url, room_id, password,
                                  ["stun:stun.l.google.com:19302"], log, approve))
        except Exception as e:
            log(f"Sem conexão ({e.__class__.__name__}). Tentando de novo...")
        threading.Event().wait(5)


threading.Thread(target=worker, daemon=True).start()
root.mainloop()
