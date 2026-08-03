"""Migra o history.db (SQLite, versao desktop) para o PostgreSQL.

Migra as 11 tabelas integralmente, preservando as chaves primarias.

    python scripts/migrate_sqlite_to_pg.py --sqlite /caminho/history.db --dry-run
    python scripts/migrate_sqlite_to_pg.py --sqlite /caminho/history.db --reset

Pontos que exigiram cuidado
---------------------------
1. IDs preservados. `collections.id` e `events.id` sao IDENTITY no PostgreSQL,
   que normalmente ignora o valor fornecido. Como `seen_history.collection_id`
   aponta para `collections.id`, gerar ids novos quebraria o vinculo de 550 mil
   linhas. Usamos OVERRIDING SYSTEM VALUE e, ao final, reposicionamos a
   sequencia -- se ela ficasse em 1, a proxima coleta colidiria com id existente.

2. COPY em vez de INSERT. seen_history tem ~550 mil linhas; uma a uma levaria
   dezenas de minutos.

3. NULL em coluna NOT NULL. O SQLite aceitava NULL onde o schema novo exige
   NOT NULL (ex.: mac_state.last_seen, client_info.vip). Normalizamos para 0.

4. Idempotencia. Por padrao o script se recusa a rodar sobre um banco que ja
   tem dados. Use --reset para limpar as tabelas antes de carregar.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

# `unifi.db` puxa psycopg, que so existe dentro do container. O import fica
# dentro de main(), depois do --dry-run, para que a conferencia da origem possa
# ser feita de qualquer maquina (inclusive a estacao Windows) sem instalar nada.

# Ordem importa: collections antes de seen_history (vinculo por collection_id).
TABELAS = [
    "collections",
    "mac_state",
    "seen_history",
    "client_info",
    "events",
    "settings",
    "unifi_audit",
    "edit_locks",
    "active_sessions",
    "wlan_locks",
]

# Colunas de cada tabela no destino, na ordem em que serao gravadas.
COLUNAS = {
    "collections": ["id", "ts"],
    "mac_state": [
        "site_id", "site_desc", "wlan_id", "wlan_name", "mac", "name",
        "hostname", "oui", "in_allow_list", "blocked", "last_seen",
        "last_online", "first_seen", "first_collected", "last_collected"],
    "seen_history": ["collection_id", "site_id", "mac", "online", "last_seen"],
    "client_info": [
        "mac", "nome", "setor", "unidade", "funcao", "lider", "chamado",
        "notes", "gestor_autorizou", "termo", "vip", "created_at", "updated_at"],
    "events": ["id", "ts", "site_id", "site_desc", "mac", "event", "detail"],
    "settings": ["key", "value"],
    "unifi_audit": [
        "uid", "ts", "site_id", "site_desc", "key", "operation", "actor",
        "message", "raw", "imported_at"],
    "edit_locks": ["mac", "who", "ts"],
    "active_sessions": ["sid", "who", "machine", "last_ping"],
    "wlan_locks": ["key", "who", "ts"],
}

# Colunas NOT NULL no destino que podem vir NULL do SQLite -> viram 0.
ZERO_SE_NULO = {
    "collections": {"ts"},
    "mac_state": {"in_allow_list", "blocked", "last_seen", "last_online",
                  "first_seen", "first_collected", "last_collected"},
    "client_info": {"termo", "vip"},
    "events": {"ts"},
}

# Tabelas cujo id e IDENTITY e precisa da sequencia reposicionada no fim.
IDENTITY = {"collections": "id", "events": "id"}

# Estado efemero: expira sozinho por TTL (20s a 180s). Migrado por fidelidade.
EFEMERAS = {"edit_locks", "active_sessions", "wlan_locks"}

# Chaves de `settings` que NAO sao migradas por padrao.
#
# Nao e economia de espaco, e higiene: sao material de credencial que ficaria
# parado num banco agora compartilhado, sem nenhuma utilidade.
#   unifi_password_enc -> senha cifrada com a secret.key LOCAL de cada maquina.
#                         Como a secret.key nao vai para o servidor (o modelo
#                         agora e conta de servico via env/secret), isso seria
#                         texto cifrado que ninguem consegue abrir.
#   admin_hash         -> hash scrypt do admin da v1, documentado como obsoleto
#                         desde que o login passou a ser validado no controller.
#
# As demais chaves vao normalmente -- inclusive unifi_host/unifi_site, que
# servem de referencia para preencher as variaveis do stack.
SEGREDOS_MORTOS = {"unifi_password_enc", "admin_hash"}


def contar_sqlite(sq: sqlite3.Connection) -> dict[str, int]:
    out = {}
    existentes = {r[0] for r in sq.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in TABELAS:
        out[t] = (sq.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  if t in existentes else -1)
    return out


def contar_pg(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in TABELAS}


def _linhas(sq: sqlite3.Connection, tabela: str, incluir_segredos: bool = False):
    """Le a tabela do SQLite normalizando NULL em coluna NOT NULL."""
    cols = COLUNAS[tabela]
    zeros = ZERO_SE_NULO.get(tabela, set())
    disponiveis = {r[1] for r in sq.execute(f"PRAGMA table_info({tabela})")}
    faltando = [c for c in cols if c not in disponiveis]
    if faltando:
        print(f"    aviso: colunas ausentes no SQLite (serao NULL/0): {faltando}")

    sel = ", ".join(c if c in disponiveis else "NULL" for c in cols)
    for row in sq.execute(f"SELECT {sel} FROM {tabela}"):
        if (tabela == "settings" and not incluir_segredos
                and row[0] in SEGREDOS_MORTOS):
            continue
        yield tuple(
            (0 if (v is None and c in zeros) else v)
            for c, v in zip(cols, row))


def migrar_tabela(sq, conn, tabela: str, incluir_segredos: bool = False) -> int:
    cols = COLUNAS[tabela]
    lista = ", ".join(f'"{c}"' for c in cols)
    override = " OVERRIDING SYSTEM VALUE" if tabela in IDENTITY else ""
    total = 0
    with conn.cursor() as cur:
        with cur.copy(
            f'COPY {tabela} ({lista}){override} FROM STDIN'
        ) as copy:
            for linha in _linhas(sq, tabela, incluir_segredos):
                copy.write_row(linha)
                total += 1
    return total


def migrar_lease(sq, conn) -> None:
    """Leva settings['last_collect_ts'] para a tabela collect_lease.

    O lease trocou de lugar na migracao (era uma linha TEXT em settings, virou
    coluna BIGINT). Sem este passo o destino comecaria com last_ts=0 e a
    primeira coleta dispararia na hora, ignorando a janela que ja estava em
    curso na instalacao antiga.
    """
    linhas = dict(sq.execute(
        "SELECT key, value FROM settings "
        "WHERE key IN ('last_collect_ts','last_collect_by')").fetchall())
    bruto = (linhas.get("last_collect_ts") or "0").strip()
    ts = int(bruto) if bruto.isdigit() else 0
    quem = linhas.get("last_collect_by") or ""
    conn.execute(
        "INSERT INTO collect_lease(id, last_ts, last_by) VALUES (1,%s,%s) "
        "ON CONFLICT(id) DO UPDATE SET last_ts=excluded.last_ts, "
        "last_by=excluded.last_by", (ts, quem))
    conn.commit()
    quando = (time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "nunca")
    print(f"  collect_lease -> ultima coleta {quando} por {quem or '?'}")


def reposicionar_sequencias(conn) -> None:
    """Coloca a IDENTITY acima do maior id migrado.

    Sem isto a sequencia continuaria em 1 e o proximo INSERT falharia com
    violacao de chave primaria contra os registros que acabamos de trazer.
    """
    for tabela, col in IDENTITY.items():
        r = conn.execute(f"SELECT COALESCE(MAX({col}), 0) AS m FROM {tabela}").fetchone()
        prox = r["m"] + 1
        conn.execute(
            f"ALTER TABLE {tabela} ALTER COLUMN {col} RESTART WITH {prox}")
        print(f"  sequencia {tabela}.{col} -> proximo id {prox}")
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite -> PostgreSQL")
    ap.add_argument("--sqlite", required=True, help="caminho do history.db")
    ap.add_argument("--reset", action="store_true",
                    help="LIMPA as tabelas no PostgreSQL antes de carregar")
    ap.add_argument("--dry-run", action="store_true",
                    help="so mostra o que seria migrado")
    ap.add_argument("--incluir-segredos", action="store_true",
                    help="migra tambem unifi_password_enc e admin_hash "
                         "(credenciais mortas; por padrao ficam de fora)")
    args = ap.parse_args()

    if not os.path.exists(args.sqlite):
        sys.exit(f"arquivo nao encontrado: {args.sqlite}")

    sq = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    origem = contar_sqlite(sq)

    print(f"\nORIGEM: {args.sqlite} "
          f"({os.path.getsize(args.sqlite) / 1048576:.1f} MB)")
    print(f"{'tabela':<18} {'linhas':>10}")
    print("-" * 30)
    for t, n in origem.items():
        marca = "  (ausente)" if n < 0 else ("  (efemera)" if t in EFEMERAS else "")
        print(f"{t:<18} {max(n, 0):>10}{marca}")
    print(f"{'TOTAL':<18} {sum(max(n, 0) for n in origem.values()):>10}")

    if args.dry_run:
        print("\n--dry-run: nada foi gravado.")
        return

    from unifi import db  # noqa: PLC0415 -- ver nota no topo do arquivo

    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "60")))
    db.apply_schema()

    with db.connection() as conn:
        destino = contar_pg(conn)
        ocupadas = {t: n for t, n in destino.items() if n > 0}
        if ocupadas and not args.reset:
            print("\nO PostgreSQL JA TEM DADOS:")
            for t, n in ocupadas.items():
                print(f"  {t}: {n} linha(s)")
            sys.exit("\nAbortado. Use --reset para limpar antes de carregar "
                     "(ou aponte para um banco vazio).")

        if args.reset and ocupadas:
            print("\n--reset: limpando as tabelas de destino...")
            conn.execute("TRUNCATE " + ", ".join(TABELAS) + " RESTART IDENTITY")
            conn.commit()

        print("\nMIGRANDO")
        t0 = time.time()
        migrado = {}
        for t in TABELAS:
            if origem[t] < 0:
                print(f"  {t:<18} ausente na origem, pulando")
                migrado[t] = 0
                continue
            ti = time.time()
            n = migrar_tabela(sq, conn, t, args.incluir_segredos)
            migrado[t] = n
            extra = ""
            if t == "settings" and n < origem[t]:
                extra = f"  ({origem[t] - n} credencial(is) morta(s) omitida(s))"
            print(f"  {t:<18} {n:>10} linha(s)  {time.time() - ti:5.1f}s{extra}")
        conn.commit()

        reposicionar_sequencias(conn)
        migrar_lease(sq, conn)

        print("\nCONFERENCIA")
        final = contar_pg(conn)
        print(f"{'tabela':<18} {'origem':>10} {'destino':>10}  ok")
        print("-" * 46)
        divergencias = []
        for t in TABELAS:
            orig = max(origem[t], 0)
            dest = final[t]
            # a unica diferenca aceitavel e settings menor pelas credenciais
            # mortas que optamos por nao trazer
            esperado = migrado[t] if origem[t] >= 0 else 0
            ok = dest == esperado
            nota = ""
            if t == "settings" and dest < orig:
                nota = "  (omissao intencional)"
            if not ok:
                divergencias.append(t)
            print(f"{t:<18} {orig:>10} {dest:>10}  "
                  f"{'sim' if ok else 'NAO'}{nota}")

        print(f"\nconcluido em {time.time() - t0:.1f}s")
        if divergencias:
            sys.exit(f"DIVERGENCIA em: {', '.join(divergencias)}")
        print("Todas as contagens conferem com o que foi enviado.")

    sq.close()


if __name__ == "__main__":
    main()
