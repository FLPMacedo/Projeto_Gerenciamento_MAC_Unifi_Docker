"""Credenciais do UniFi guardadas LOCALMENTE por maquina (cada usuario usa a
PROPRIA conta UniFi). Ficam num arquivo `creds.enc` ao lado do exe, criptografado
com a `secret.key` LOCAL (gerada na 1a vez em cada maquina, nunca distribuida).

Nada de credencial vai para o banco compartilhado. Na 1a vez, se houver um .env
com credenciais, elas sao usadas como semente (util em dev).
"""
from __future__ import annotations

import json
import os

from . import secret


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def save(creds_file: str, key_path: str, cfg: dict) -> None:
    """Grava as credenciais (criptografadas) localmente."""
    blob = json.dumps({
        "host": cfg.get("host", ""), "site": cfg.get("site", "default"),
        "username": cfg.get("username", ""), "password": cfg.get("password", ""),
        "verify": bool(cfg.get("verify")),
    }, ensure_ascii=False)
    with open(creds_file, "w", encoding="utf-8") as fh:
        fh.write(secret.encrypt(key_path, blob))


def resolve(creds_file: str, key_path: str) -> dict | None:
    # 1) arquivo local de credenciais (por maquina)
    if os.path.exists(creds_file):
        try:
            with open(creds_file, encoding="utf-8") as fh:
                data = json.loads(secret.decrypt(key_path, fh.read()) or "{}")
            if data.get("host"):
                data.setdefault("site", "default")
                data["verify"] = bool(data.get("verify"))
                return data
        except Exception:
            pass
    # 2) semente via .env (dev / primeiro arranque)
    if os.getenv("UNIFI_HOST"):
        cfg = {
            "host": os.environ["UNIFI_HOST"],
            "site": os.getenv("UNIFI_SITE", "default"),
            "username": os.getenv("UNIFI_USERNAME", ""),
            "password": os.getenv("UNIFI_PASSWORD", ""),
            "verify": _truthy(os.getenv("UNIFI_VERIFY_SSL", "")),
        }
        try:
            save(creds_file, key_path, cfg)
        except Exception:
            pass
        return cfg
    return None
