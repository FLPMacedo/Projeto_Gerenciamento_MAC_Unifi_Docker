r"""Coleta um snapshot de todos os sites e grava/mescla no SQLite.

Rode periodicamente (ex.: a cada 6h) no Agendador de Tarefas do Windows:
    .\.venv\Scripts\python.exe collect.py

Quanto mais coletas, mais confiavel fica o historico de last_seen.
"""
from __future__ import annotations

import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

from unifi import UnifiClient, db
from unifi import config as unifi_config_mod
from unifi.inventory import snapshot_all

load_dotenv()


def main() -> None:
    t0 = time.time()
    base = os.path.dirname(os.path.abspath(__file__))
    key_path = os.getenv("KEY_PATH") or os.path.join(base, "secret.key")
    creds_path = os.getenv("CREDS_PATH") or os.path.join(base, "creds.enc")
    cfg = unifi_config_mod.resolve(creds_path, key_path)
    if not cfg or not cfg.get("username") or not cfg.get("password"):
        sys.exit("UniFi nao configurado nesta maquina. Abra o app e faca login uma vez.")
    client = UnifiClient(
        host=cfg["host"], username=cfg["username"], password=cfg["password"],
        site=cfg["site"], verify_ssl=cfg["verify"],
    )
    client.login()
    rows, ts = snapshot_all(client)
    client.logout()

    conn = db.connect(os.getenv("DB_PATH"))
    res = db.record_snapshot(conn, rows, ts)
    summ = db.overview_summary(conn)
    t = summ["totals"]

    when = time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))
    print(f"[{when}] coleta #{res['collection_id']} gravada em {time.time()-t0:.1f}s")
    print(f"  linhas: {res['rows']} | marcados como removidos: {res['marked_removed']}")
    print(f"  cadastrados: {t['total']} | em uso: {t['in_use']} | "
          f"disponiveis (>{summ['days']}d): {t['reclaimable']} "
          f"(parados {t['over_x']} + nunca {t['never']})")
    print(f"  total de coletas no historico: {summ['collections']}")


if __name__ == "__main__":
    main()
