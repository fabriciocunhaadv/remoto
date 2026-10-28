"""Agente: roda no computador a ser acessado (Windows/macOS).
Captura a tela, envia por WebRTC e executa mouse/teclado recebidos."""
import argparse
import asyncio
import json
import logging
import secrets
from pathlib import Path
import time

import aiohttp
import mss
import numpy as np
import pyautogui
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


def handle_input(raw, monitor):
    ev = json.loads(raw)
    t = ev["type"]
    x = monitor["left"] + int(ev.get("x", 0) * monitor["width"])
    y = monitor["top"] + int(ev.get("y", 0) * monitor["height"])
    if t == "move":
        pyautogui.moveTo(x, y, _pause=False)
    elif t == "down":
        pyautogui.mouseDown(x, y, button=["left", "middle", "right"][ev["button"]])
    elif t == "up":
        pyautogui.mouseUp(x, y, button=["left", "middle", "right"][ev["button"]])
    elif t == "scroll":
        pyautogui.scroll(-int(ev["dy"]) // 10)
    elif t in ("keydown", "keyup"):
        key = KEYMAP.get(ev["key"], ev["key"].lower() if len(ev["key"]) == 1 else None)
        if key:
            (pyautogui.keyDown if t == "keydown" else pyautogui.keyUp)(key)


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

                        @pc.on("datachannel")
                        def on_dc(ch, track=track):
                            @ch.on("message")
                            def on_message(m):
                                try:
                                    handle_input(m, track.monitor)
                                except Exception as e:
                                    print("input error:", e)
                    elif t == "offer" and pc:
                        await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                        await pc.setLocalDescription(await pc.createAnswer())
                        await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})
    finally:
        if pc:
            await pc.close()


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
