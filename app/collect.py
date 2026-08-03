"""Servico de coleta: le o controller UniFi e mescla no PostgreSQL.

Na versao desktop este script era disparado pelo Agendador de Tarefas do
Windows e rodava uma vez. Aqui ele e o processo principal do container
`collector`: fica em laco, coletando a cada COLLECT_INTERVAL segundos.

    python collect.py             # laco continuo (modo container)
    python collect.py --once      # uma coleta e sai (util para depurar)

O lease em `collect_lease` continua valendo: mesmo que alguem suba duas
replicas do collector por engano, so uma coleta por janela.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv

from unifi import UnifiClient, UnifiError, db
from unifi import config as unifi_config_mod
from unifi.inventory import snapshot_all, collect_unifi_audit

load_dotenv()

logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("collector")

COLLECT_INTERVAL = int(os.getenv("COLLECT_INTERVAL", "600"))  # 10 min
INSTANCE = os.getenv("INSTANCE_NAME") or os.uname().nodename

_stop = False


def _handle_stop(signum, _frame):
    """SIGTERM do `docker stop`: termina a coleta atual e sai limpo."""
    global _stop
    _stop = True
    log.info("sinal %s recebido: encerrando apos a coleta atual", signum)


def build_client() -> UnifiClient:
    cfg = unifi_config_mod.resolve()
    if not cfg or not cfg["username"] or not cfg["password"]:
        sys.exit("Conta de servico do UniFi nao configurada: defina UNIFI_HOST, "
                 "UNIFI_SERVICE_USERNAME e UNIFI_SERVICE_PASSWORD.")
    return UnifiClient(
        host=cfg["host"], username=cfg["username"], password=cfg["password"],
        site=cfg["site"], verify_ssl=cfg["verify"],
    )


def collect_once(client: UnifiClient, respect_lease: bool = True) -> bool:
    """Uma coleta. Retorna True se de fato coletou."""
    t0 = time.time()
    with db.connection() as conn:
        if respect_lease and not db.claim_collection(conn, COLLECT_INTERVAL, INSTANCE):
            log.debug("janela ja coletada por outra instancia")
            return False

    rows, ts = snapshot_all(client)
    novos = 0
    with db.connection() as conn:
        res = db.record_snapshot(conn, rows, ts)
        try:
            novos = collect_unifi_audit(client, conn, client.get_sites())
        except Exception as exc:                     # noqa: BLE001
            log.warning("espelho do log da UniFi falhou: %s", exc)

    log.info("coleta #%s: %d linhas | %d removidos | %d eventos | "
             "%d novos no log UniFi | %.1fs",
             res["collection_id"], res["rows"], res["marked_removed"],
             res["events"], novos, time.time() - t0)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Coletor UniFi -> PostgreSQL")
    ap.add_argument("--once", action="store_true",
                    help="coleta uma vez e sai (ignora o lease)")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "60")))
    if os.getenv("APPLY_SCHEMA", "1").lower() in {"1", "true", "yes", "on"}:
        db.apply_schema()

    log.info("UniFi: %s", unifi_config_mod.describe())
    client = build_client()
    client.login()

    if args.once:
        collect_once(client, respect_lease=False)
        return

    log.info("collector ativo em %s: intervalo de %ds", INSTANCE, COLLECT_INTERVAL)
    while not _stop:
        try:
            collect_once(client)
        except UnifiError as exc:
            log.error("erro na UniFi: %s -- refazendo login", exc)
            try:
                client = build_client()
                client.login()
            except Exception as exc2:                # noqa: BLE001
                log.error("falha ao reconectar: %s", exc2)
        except Exception as exc:                     # noqa: BLE001
            log.exception("coleta falhou: %s", exc)

        # sono fatiado para o SIGTERM ser atendido em ate 1s
        for _ in range(COLLECT_INTERVAL):
            if _stop:
                break
            time.sleep(1)

    log.info("collector encerrado")


if __name__ == "__main__":
    main()
