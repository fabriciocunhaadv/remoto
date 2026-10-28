"""Servidor de sinalização: serve o visualizador web e repassa mensagens
SDP/ICE entre o agente (PC remoto) e o visualizador (navegador)."""
import argparse
import hmac
from pathlib import Path

from aiohttp import web, WSMsgType

STATIC = Path(__file__).parent / "static"
rooms = {}  # id -> {"password": str, "agent": ws, "viewer": ws|None}


async def agent_ws(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    room_id = None
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        data = msg.json()
        if data.get("type") == "register" and room_id is None:
            room_id = str(data["id"])
            rooms[room_id] = {"password": data["password"], "agent": ws, "viewer": None}
        elif room_id and rooms.get(room_id, {}).get("viewer") is not None:
            await rooms[room_id]["viewer"].send_json(data)
    if room_id and rooms.get(room_id, {}).get("agent") is ws:
        viewer = rooms[room_id]["viewer"]
        if viewer is not None:
            await viewer.close()
        rooms.pop(room_id, None)
    return ws


async def viewer_ws(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    room = None
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        data = msg.json()
        if data.get("type") == "join" and room is None:
            r = rooms.get(str(data.get("id")))
            ok = r and hmac.compare_digest(r["password"], str(data.get("password", "")))
            if not ok:
                await ws.send_json({"type": "error", "message": "ID ou senha inválidos"})
                await ws.close()
                break
            if r["viewer"] is not None:
                await ws.send_json({"type": "error", "message": "Já existe uma sessão ativa"})
                await ws.close()
                break
            room = r
            room["viewer"] = ws
            await room["agent"].send_json({"type": "viewer-joined"})
        elif room is not None:
            await room["agent"].send_json(data)
    if room is not None and room["viewer"] is ws:
        room["viewer"] = None
        if not room["agent"].closed:
            await room["agent"].send_json({"type": "viewer-left"})
    return ws


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    app = web.Application()
    app.add_routes([
        web.get("/ws/agent", agent_ws),
        web.get("/ws/viewer", viewer_ws),
        web.get("/", lambda r: web.FileResponse(STATIC / "index.html")),
    ])
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
