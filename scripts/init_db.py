"""Prepara o banco e sai. Roda como servico one-shot no compose.

Faz duas coisas, nesta ordem:
  1. schema.sql  -> cria o que falta (fotografia do schema atual)
  2. migracoes   -> aplica as alteracoes versionadas em db/migrations/

Por que um servico separado: se cada worker do gunicorn fizesse isso no import,
varios CREATE TABLE IF NOT EXISTS simultaneos poderiam colidir no catalogo do
PostgreSQL ("duplicate key value violates unique constraint
pg_type_type_name_nsp_index") -- falha rara, intermitente e chata de
diagnosticar. Aqui e um processo so, que termina antes de web e collector
subirem.
"""
from __future__ import annotations

import logging
import os
import sys


def _achar_pacote() -> None:
    """O layout do repositorio (`<raiz>/app/unifi`) e o do container
    (`/app/unifi`) sao diferentes; procura onde o pacote de fato esta."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    raiz = os.path.dirname(aqui)
    for cand in (os.path.join(raiz, "app"), raiz, "/app"):
        if os.path.isdir(os.path.join(cand, "unifi")):
            sys.path.insert(0, cand)
            return
    sys.exit("nao encontrei o pacote `unifi` a partir de " + aqui)


_achar_pacote()

from unifi import db  # noqa: E402

logging.basicConfig(
    stream=sys.stdout, level="INFO",
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("init_db")


def main() -> None:
    log.info("aguardando o PostgreSQL...")
    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "120")))

    r = db.prepare_database()
    novo, aplicadas = r["novo"], r["migracoes"]

    if novo:
        log.info("banco novo: schema.sql aplicado")
        if aplicadas:
            log.info("%d migracao(oes) marcada(s) sem executar (o schema ja "
                     "nasceu com elas): %s", len(aplicadas), ", ".join(aplicadas))
    else:
        log.info("banco existente: schema.sql NAO e reaplicado, so as migracoes")
        if aplicadas:
            log.info("%d migracao(oes) aplicada(s): %s",
                     len(aplicadas), ", ".join(aplicadas))
        else:
            log.info("nenhuma migracao pendente")

    with db.connection() as conn:
        tabelas = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """).fetchall()
        historico = db.migracoes_status(conn)

    log.info("%d tabelas: %s", len(tabelas),
             ", ".join(t["table_name"] for t in tabelas))
    log.info("migracoes registradas: %d", len(historico))
    log.info("banco pronto")


if __name__ == "__main__":
    main()
