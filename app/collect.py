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


def build_client() -> UnifiClient | None:
    """Resolve uma credencial utilizavel e devolve o cliente ja logado.

    O coletor roda sem ninguem logado, entao usa as credenciais gravadas nos
    logins (modelo hibrido), da que autenticou mais recentemente para a mais
    antiga -- e a conta de servico primeiro, se estiver configurada.

    Tentar mais de uma importa: a credencial do topo pode ter deixado de valer
    (a pessoa trocou a senha, a conta foi desativada). Sem esse encadeamento a
    coleta simplesmente pararia ate alguem logar de novo.
    """
    with db.connection() as conn:
        candidatos = unifi_config_mod.collector_candidates(conn)

    if not candidatos:
        log.warning("nenhuma credencial disponivel ainda. Basta alguem fazer "
                    "login na interface web uma vez para a coleta comecar.")
        return None

    for c in candidatos:
        try:
            cli = UnifiClient(
                host=c["host"], username=c["username"], password=c["password"],
                site=c["site"], verify_ssl=c["verify"])
            cli.login()
            cli.get_sites()          # confirma que tem acesso de leitura
        except Exception as exc:     # noqa: BLE001
            log.warning("credencial de %s nao serve (%s): %s",
                        c["username"], c["origem"], str(exc)[:120])
            continue
        log.info("coletando com a credencial de %s (%s)", c["username"], c["origem"])
        if c["origem"].startswith("login de"):
            with db.connection() as conn:
                db.mark_creds_ok(conn, c["username"])
        return cli

    log.error("nenhuma das %d credencial(is) autenticou. Peca para alguem "
              "fazer login na interface web.", len(candidatos))
    return None


def collect_once(client: UnifiClient, respect_lease: bool = True) -> bool:
    """Uma coleta. Retorna True se de fato coletou."""
    t0 = time.time()
    with db.connection() as conn:
        if respect_lease and not db.claim_collection(conn, COLLECT_INTERVAL, INSTANCE):
            log.debug("janela ja coletada por outra instancia")
            return False

    rows, ts = snapshot_all(client)
    novos = 0
    vsync = {"atualizados": 0, "ausentes": 0}
    with db.connection() as conn:
        res = db.record_snapshot(conn, rows, ts)
        try:
            novos = collect_unifi_audit(client, conn, client.get_sites())
        except Exception as exc:                     # noqa: BLE001
            log.warning("espelho do log da UniFi falhou: %s", exc)
        try:
            vsync = sincroniza_vouchers(client, conn)
        except Exception as exc:                     # noqa: BLE001
            log.warning("sincronizacao de vouchers falhou: %s", exc)

    log.info("coleta #%s: %d linhas | %d removidos | %d eventos | "
             "%d novos no log UniFi | vouchers %d conferidos/%d ausentes | %.1fs",
             res["collection_id"], res["rows"], res["marked_removed"],
             res["events"], novos, vsync["atualizados"], vsync["ausentes"],
             time.time() - t0)
    return True


def sincroniza_vouchers(client: UnifiClient, conn) -> dict:
    """Confere no controller quais vouchers ja foram usados.

    So consulta os sites onde de fato geramos algo -- varrer os 14 sites a cada
    10 minutos seria desperdicio, ja que a maioria nunca recebeu voucher nosso.
    """
    sites = conn.execute(
        "SELECT DISTINCT site_id FROM voucher_grants "
        "WHERE revogado_em IS NULL").fetchall()
    total = {"atualizados": 0, "ausentes": 0}
    for s in sites:
        try:
            vs = client.get_vouchers(s["site_id"])
        except Exception as exc:                     # noqa: BLE001
            log.warning("vouchers do site %s: %s", s["site_id"], str(exc)[:100])
            continue
        r = db.sync_voucher_status(conn, s["site_id"], vs)
        total["atualizados"] += r["atualizados"]
        total["ausentes"] += r["ausentes"]
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Coletor UniFi -> PostgreSQL")
    ap.add_argument("--once", action="store_true",
                    help="coleta uma vez e sai (ignora o lease)")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "60")))
    if os.getenv("APPLY_SCHEMA", "1").lower() in {"1", "true", "yes", "on"}:
        # No compose isto vem "0": quem prepara o banco e o servico `init`.
        # Serve para rodar fora do stack (dev, ou um container avulso).
        db.prepare_database()

    with db.connection() as conn:
        log.info("UniFi: %s", unifi_config_mod.describe(conn))

    if args.once:
        client = build_client()
        if client is None:
            sys.exit("sem credencial utilizavel: faca login na interface web.")
        collect_once(client, respect_lease=False)
        return

    log.info("collector ativo em %s: intervalo de %ds", INSTANCE, COLLECT_INTERVAL)
    client: UnifiClient | None = None
    while not _stop:
        try:
            if client is None:
                client = build_client()
            if client is not None:
                collect_once(client)
        except UnifiError as exc:
            log.error("erro na UniFi: %s -- vai reescolher a credencial", exc)
            client = None
        except Exception as exc:                     # noqa: BLE001
            log.exception("coleta falhou: %s", exc)
            client = None

        # sono fatiado para o SIGTERM ser atendido em ate 1s
        for _ in range(COLLECT_INTERVAL):
            if _stop:
                break
            time.sleep(1)

    log.info("collector encerrado")


if __name__ == "__main__":
    main()
