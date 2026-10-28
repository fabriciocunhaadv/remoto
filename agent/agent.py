"""Agente: roda no computador a ser acessado (Windows/macOS).
Captura a tela, envia por WebRTC e executa mouse/teclado recebidos."""
import argparse
import asyncio
import fractions
import json
import secrets
import time

import aiohttp
import mss
import numpy as np
import pyautogui
from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription, VideoStreamTrack)
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
FPS = 15
MAX_WIDTH = 1600

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
        self.sct = mss.mss()
        self.monitor = self.sct.monitors[1]

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        await asyncio.sleep(1 / FPS)
        shot = self.sct.grab(self.monitor)
        img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > MAX_WIDTH:
            img = img.resize((MAX_WIDTH, int(img.height * MAX_WIDTH / img.width)))
        frame = VideoFrame.from_ndarray(np.asarray(img), format="rgb24")
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


async def run(server, room_id, password, ice, log=print, approve=None):
    pc = None
    config = RTCConfiguration([RTCIceServer(u) for u in ice])
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{server}/ws/agent", heartbeat=20) as ws:
            await ws.send_json({"type": "register", "id": room_id, "password": password})
            log("waiting")
            async for msg in ws:
                data = json.loads(msg.data)
                t = data["type"]
                if t in ("viewer-joined", "viewer-left"):
                    if pc:
                        await pc.close()
                        pc = None
                    if t == "viewer-left":
                        log("waiting")
                        continue
                    if approve and not await approve():
                        await ws.send_json({"type": "denied"})
                        continue
                    log("connected")
                    pc = RTCPeerConnection(config)
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

                    @pc.on("icecandidate")
                    async def on_ice(c):
                        pass  # aiortc inclui candidatos no SDP da resposta
                elif t == "offer" and pc:
                    await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                    await pc.setLocalDescription(await pc.createAnswer())
                    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})


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
