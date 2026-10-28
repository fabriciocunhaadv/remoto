"""Endereço do servidor. Edite config.json (ao lado do programa) ou o padrão abaixo."""
import json
import sys
from pathlib import Path

DEFAULT = "https://remoto.assessor.net.br"


def server_url():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    try:
        return json.loads((base / "config.json").read_text())["server"].rstrip("/")
    except Exception:
        return DEFAULT
