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

    with db.connection() as conn:
        # Precisa ser avaliado ANTES do schema.sql: depois dele o banco sempre
        # pareceria "ja existente" e as migracoes seriam executadas num banco
        # que ja nasceu com o schema mais novo.
        novo = db.banco_vazio(conn)

    log.info("banco %s", "novo (sera criado do zero)" if novo
             else "existente (so o que faltar)")
    db.apply_schema()
    log.info("schema.sql aplicado")

    with db.connection() as conn:
        aplicadas = db.apply_migrations(conn, baseline=novo)
        if aplicadas and novo:
            log.info("%d migracao(oes) marcada(s) como aplicada(s) sem executar "
                     "(banco novo ja nasceu com o schema atual): %s",
                     len(aplicadas), ", ".join(aplicadas))
        elif aplicadas:
            log.info("%d migracao(oes) aplicada(s): %s",
                     len(aplicadas), ", ".join(aplicadas))
        else:
            log.info("nenhuma migracao pendente")

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
