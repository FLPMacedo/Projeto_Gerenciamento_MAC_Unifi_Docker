"""Aplica o schema no PostgreSQL e sai. Roda como servico one-shot no compose.

Existe para que o schema seja aplicado UMA vez, por um unico processo, antes de
web e collector subirem. Se cada worker do gunicorn aplicasse o schema no
import, varios CREATE TABLE IF NOT EXISTS simultaneos poderiam colidir no
catalogo do PostgreSQL ("duplicate key value violates unique constraint
pg_type_type_name_nsp_index") -- uma falha rara, intermitente e chata de
diagnosticar.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, "/app")

from unifi import db  # noqa: E402

logging.basicConfig(
    stream=sys.stdout, level="INFO",
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("init_db")


def main() -> None:
    log.info("aguardando o PostgreSQL...")
    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "120")))
    log.info("aplicando o schema...")
    db.apply_schema()
    with db.connection() as conn:
        tabelas = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """).fetchall()
    log.info("schema aplicado. %d tabelas: %s", len(tabelas),
             ", ".join(t["table_name"] for t in tabelas))


if __name__ == "__main__":
    main()
