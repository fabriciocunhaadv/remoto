"""Agente: roda no computador a ser acessado (Windows/macOS).
Captura a tela, envia por WebRTC e executa mouse/teclado recebidos."""
import argparse
import asyncio
import json
import logging
import secrets
import struct
import sys
from pathlib import Path
import time

import aiohttp
import mss
import numpy as np
import pyautogui
import pyperclip
from concurrent.futures import ThreadPoolExecutor

from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription, VideoStreamTrack)
from aiortc.codecs import h264, vpx
from av import VideoFrame

logging.basicConfig(filename=str(Path.home() / ".remotedesk.log"), level=logging.INFO,
                    format="%(asctime)s %(message)s")
log_file = logging.getLogger("remotedesk")

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
MAX_WIDTH = 1600

# A aiortc começa em 0,5 Mbps e limita em 1,5 Mbps (VP8): pouco para tela.
vpx.DEFAULT_BITRATE, vpx.MIN_BITRATE, vpx.MAX_BITRATE = 3_000_000, 1_000_000, 8_000_000
h264.DEFAULT_BITRATE, h264.MIN_BITRATE, h264.MAX_BITRATE = 3_000_000, 1_000_000, 8_000_000

KEYMAP = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc",
    "ArrowLeft": "left", "ArrowRight": "right", "ArrowUp": "up", "ArrowDown": "down",
    "Delete": "delete", "Home": "home", "End": "end", "PageUp": "pageup",
    "PageDown": "pagedown", " ": "space", "Shift": "shift", "Control": "ctrl",
    "Alt": "alt", "Meta": "win", "CapsLock": "capslock",
}


class ScreenTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        with mss.mss() as sct:
            self.monitor = sct.monitors[1]
        w, h = self.monitor["width"], self.monitor["height"]
        scale = min(1, MAX_WIDTH / w)
        self.out_size = (int(w * scale) // 2 * 2, int(h * scale) // 2 * 2)
        # captura e conversão rodam numa thread própria (a mss exige a mesma thread)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.sct = None
        self.last = None

    def _grab(self):
        try:
            if self.sct is None:
                self.sct = mss.mss()
            shot = self.sct.grab(self.monitor)
            arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
            frame = VideoFrame.from_ndarray(arr, format="bgra")
            self.last = frame.reformat(width=self.out_size[0], height=self.out_size[1], format="yuv420p")
        except Exception as e:  # tela bloqueada, troca de sessão etc.: não derruba o vídeo
            log_file.warning("falha ao capturar a tela: %r", e)
            self.sct = None
            if self.last is None:
                black = VideoFrame(*self.out_size, format="yuv420p")
                for i, plane in enumerate(black.planes):  # Y=0; U,V=128 é preto
                    plane.update(bytes([0 if i == 0 else 128]) * plane.buffer_size)
                self.last = black
        return self.last

    async def recv(self):
        pts, time_base = await self.next_timestamp()  # já limita a ~30 fps
        frame = await asyncio.get_running_loop().run_in_executor(self.pool, self._grab)
        frame.pts, frame.time_base = pts, time_base
        return frame


IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
BUTTONS = ["left", "middle", "right"]


def type_unicode(ch):
    """Digita um caractere qualquer (ç, ã, é, emojis...) sem depender do layout do teclado."""
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        units = struct.unpack(f"<{len(ch.encode('utf-16-le')) // 2}H", ch.encode("utf-16-le"))
        for unit in units:
            for flags in (0x0004, 0x0004 | 0x0002):  # KEYEVENTF_UNICODE, depois KEYUP
                inp = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, unit, flags, 0, 0)))
                ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    else:  # macOS/Linux: usa a área de transferência
        pyperclip.copy(ch)
        pyautogui.hotkey("command" if IS_MAC else "ctrl", "v")


class InputHandler:
    """Executa mouse/teclado vindos do técnico e sincroniza a área de transferência."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.pressed = set()
        self.channel = None

    def release_all(self):
        """Solta teclas que ficaram pressionadas se a conexão cair."""
        for k in list(self.pressed):
            try:
                pyautogui.keyUp(k)
            except Exception:
                pass
        self.pressed.clear()

    def handle(self, raw):
        ev = json.loads(raw)
        t = ev["type"]
        m = self.monitor
        x = m["left"] + int(ev.get("x", 0) * m["width"])
        y = m["top"] + int(ev.get("y", 0) * m["height"])
        if t == "move":
            pyautogui.moveTo(x, y, _pause=False)
        elif t in ("down", "up"):
            (pyautogui.mouseDown if t == "down" else pyautogui.mouseUp)(x, y, button=BUTTONS[ev["button"]])
        elif t == "scroll":
            pyautogui.scroll(-int(ev["dy"]) // 10)
        elif t in ("keydown", "keyup"):
            self.key(t == "keydown", ev["key"])
        elif t == "paste":
            pyperclip.copy(ev["text"])
            pyautogui.hotkey("command" if IS_MAC else "ctrl", "v")

    def key(self, down, k):
        name = KEYMAP.get(k)
        if name is None and len(k) == 1:
            if ord(k) > 127:  # caractere acentuado/especial
                if down:
                    type_unicode(k)
                return
            name = k.lower()
        if not name:
            return
        if IS_MAC and name == "ctrl":
            name = "command"  # Ctrl+C do técnico vira Cmd+C no Mac do cliente
        if down:
            copying = name in ("c", "x") and bool(self.pressed & {"ctrl", "command"})
            before = pyperclip.paste() if copying else None  # antes do aplicativo copiar
            pyautogui.keyDown(name)
            self.pressed.add(name)
            if copying:
                asyncio.ensure_future(self.send_clipboard(before))
        else:
            pyautogui.keyUp(name)
            self.pressed.discard(name)

    async def send_clipboard(self, before):
        """Depois de um Ctrl+C do técnico, envia o texto copiado para ele (só nesse caso)."""
        loop = asyncio.get_running_loop()
        for delay in (0.3, 0.5, 0.8):
            await asyncio.sleep(delay)
            text = await loop.run_in_executor(None, pyperclip.paste)
            if text and text != before:
                if self.channel and self.channel.readyState == "open":
                    self.channel.send(json.dumps({"type": "clipboard", "text": text[:500_000]}))
                return


async def fetch_ice(session, server):
    """Servidores STUN/TURN configurados no servidor (None se indisponível)."""
    base = server.replace("wss://", "https://").replace("ws://", "http://")
    try:
        async with session.get(f"{base}/api/ice", timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
        return [RTCIceServer(x["urls"], x.get("username") or None, x.get("credential") or None) for x in data]
    except Exception as e:
        log_file.warning("não foi possível obter /api/ice: %r", e)
        return None


async def run(server, room_id, password, ice, log=print, approve=None, extra=None):
    """extra: campos adicionais do registro (acesso permanente: unattended, name,
    orgKey, deviceSecret; para revogar: só deviceSecret)."""
    pc = None
    handler = None
    try:
        async with aiohttp.ClientSession() as session:
            config = RTCConfiguration(await fetch_ice(session, server) or [RTCIceServer(u) for u in ice])
            async with session.ws_connect(f"{server}/ws/agent", heartbeat=20) as ws:
                await ws.send_json({"type": "register", "id": room_id, "password": password, **(extra or {})})
                log("waiting")
                async for msg in ws:
                    data = json.loads(msg.data)
                    t = data["type"]
                    if t == "error":
                        log(f"Erro: {data.get('message')}")
                    elif t in ("viewer-joined", "viewer-left"):
                        if pc:
                            await pc.close()
                            pc = None
                        if handler:
                            handler.release_all()
                        if t == "viewer-left":
                            log("waiting")
                            continue
                        unattended = bool(data.get("unattended"))
                        if not unattended and approve and not await approve():
                            await ws.send_json({"type": "denied"})
                            continue
                        log("connected_unattended" if unattended else "connected")
                        pc = RTCPeerConnection(config)

                        @pc.on("iceconnectionstatechange")
                        def on_ice_state(pc=pc):
                            log_file.info("ICE: %s", pc.iceConnectionState)

                        track = ScreenTrack()
                        pc.addTrack(track)
                        handler = InputHandler(track.monitor)

                        @pc.on("datachannel")
                        def on_dc(ch, handler=handler):
                            handler.channel = ch

                            @ch.on("message")
                            def on_message(m):
                                try:
                                    handler.handle(m)
                                except Exception as e:
                                    log_file.warning("erro de entrada: %r", e)
                    elif t == "offer" and pc:
                        await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                        await pc.setLocalDescription(await pc.createAnswer())
                        await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})
    finally:
        if pc:
            await pc.close()
        if handler:
            handler.release_all()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="ws://localhost:8080")
    p.add_argument("--id", default=str(secrets.randbelow(900_000_000) + 100_000_000))
    p.add_argument("--password", default=secrets.token_urlsafe(6))
    p.add_argument("--stun", nargs="*", default=["stun:stun.l.google.com:19302"])
    a = p.parse_args()
    asyncio.run(run(a.server, a.id, a.password, a.stun))


if __name__ == "__main__":
    main()
