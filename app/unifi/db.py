"""Persistencia em PostgreSQL com merge historico das coletas.

Migrado do SQLite preservando a API publica: todas as funcoes mantem o mesmo
nome e a mesma assinatura da versao desktop, de modo que app.py e os templates
seguem praticamente inalterados.

A cada coleta guardamos, por (site, mac), o MAIOR last_seen ja observado
(controller + nossas coletas). Um MAC so e considerado DISPONIVEL (liberavel)
se ficar mais de AVAILABLE_DAYS (35) dias sem logar -- assim quem esta de
ferias nao e marcado por engano.

Tratamento de "nunca conectou":
  - never_mode="grace"     -> so vira disponivel 35 dias apos a 1a vez que o
                              vimos na lista (protege cadastro novo que ainda
                              nao conectou).
  - never_mode="immediate" -> nunca logou ja conta como disponivel.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

AVAILABLE_DAYS = 35
MAC_FILTER_CAP = 512
DAY = 86400

STATUS_LABEL = {
    "online": "Online agora",
    "recent": "Ativo (<=7d)",
    "idle": "Ocioso (8-35d)",
    "stale": "Parado (36-90d)",
    "abandoned": "Abandonado (>90d)",
    "never": "Nunca conectou",
    "pending": "Novo (sem conexão ainda)",
}
UNUSED_STATUSES = {"stale", "abandoned", "never"}

CLIENT_FIELDS = ["nome", "setor", "unidade", "funcao", "lider",
                 "gestor_autorizou", "chamado", "notes"]


# ============================================================ conexao / pool
_pool: ConnectionPool | None = None


def dsn_from_env() -> str:
    """DSN do PostgreSQL. Aceita DATABASE_URL inteiro ou as partes PG*."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("PGHOST", "db")
    port = os.getenv("PGPORT", "5432")
    name = os.getenv("PGDATABASE", "gestaomac")
    user = os.getenv("PGUSER", "gestaomac")
    pw = os.getenv("PGPASSWORD", "")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


def init_pool(dsn: str | None = None, min_size: int = 1,
              max_size: int = 10) -> ConnectionPool:
    """Cria o pool (idempotente).

    Substitui o connect()/close() por request da versao SQLite: abrir conexao
    no PostgreSQL custa caro demais para se fazer a cada tela.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn or dsn_from_env(),
            min_size=min_size, max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Conexao do pool, devolvida automaticamente ao sair do bloco.

        with db.connection() as conn:
            ...
    """
    with init_pool().connection() as conn:
        yield conn


def wait_ready(timeout: float = 60.0) -> None:
    """Espera o banco aceitar conexao (o container do app sobe antes do db)."""
    end = time.time() + timeout
    last: Exception | None = None
    while time.time() < end:
        try:
            with connection() as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:      # noqa: BLE001 -- qualquer falha = ainda subindo
            last = exc
            close_pool()              # pool nasce quebrado se o db recusou
            time.sleep(1.0)
    raise RuntimeError(f"PostgreSQL nao respondeu em {timeout:.0f}s: {last}")


def _dir_db() -> str:
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, "db")


def apply_schema(schema_path: str | None = None) -> None:
    """Aplica db/schema.sql (idempotente: tudo e CREATE ... IF NOT EXISTS).

    Este arquivo e a FOTOGRAFIA do schema atual, usada para criar um banco do
    zero. Ele NAO altera tabela que ja existe -- para isso existem as migracoes
    versionadas (apply_migrations).
    """
    if schema_path is None:
        schema_path = os.getenv("SCHEMA_PATH") or os.path.join(
            _dir_db(), "schema.sql")
    with open(schema_path, encoding="utf-8") as fh:
        sql = fh.read()
    with connection() as conn:
        conn.execute(sql)
        conn.commit()


# ------------------------------------------------------------- migracoes
_MIGR_TABELA = """
CREATE TABLE IF NOT EXISTS schema_migrations(
    versao     TEXT PRIMARY KEY,
    nome       TEXT,
    checksum   TEXT,
    aplicada_em BIGINT,
    baseline   SMALLINT NOT NULL DEFAULT 0
)
"""


def banco_vazio(conn) -> bool:
    """True se ainda nao existe nenhuma tabela da aplicacao."""
    r = conn.execute("""
        SELECT COUNT(*) AS c FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'mac_state'
    """).fetchone()
    return r["c"] == 0


def _migracoes_disponiveis(pasta: str | None = None) -> list[tuple[str, str, str]]:
    """[(versao, nome, sql)] ordenadas pelo prefixo numerico do arquivo.

    Convencao: db/migrations/0001_descricao_curta.sql
    """
    pasta = pasta or os.path.join(_dir_db(), "migrations")
    if not os.path.isdir(pasta):
        return []
    out = []
    for nome in sorted(os.listdir(pasta)):
        if not nome.endswith(".sql"):
            continue
        versao = nome.split("_", 1)[0]
        with open(os.path.join(pasta, nome), encoding="utf-8") as fh:
            out.append((versao, nome, fh.read()))
    return out


def _checksum(sql: str) -> str:
    import hashlib
    # normaliza fim de linha: o repo e editado no Windows e roda no Linux,
    # senao o mesmo arquivo teria checksum diferente em cada lado
    return hashlib.sha256(sql.replace("\r\n", "\n").encode()).hexdigest()[:16]


def apply_migrations(conn, baseline: bool = False, pasta: str | None = None) -> list[str]:
    """Aplica as migracoes pendentes. Devolve as versoes aplicadas agora.

    `baseline=True` (banco recem-criado): o schema.sql ja trouxe tudo, entao as
    migracoes existentes sao apenas MARCADAS como aplicadas, sem executar. Sem
    isso, um banco novo tentaria rodar um ALTER numa coluna que ja nasceu certa.

    Cada migracao roda na sua propria transacao: se a de numero 3 falhar, as
    1 e 2 permanecem aplicadas e o erro aponta exatamente onde parou.
    """
    conn.execute(_MIGR_TABELA)
    conn.commit()

    ja = {r["versao"]: r for r in conn.execute(
        "SELECT versao, checksum FROM schema_migrations")}
    agora = int(time.time())
    aplicadas: list[str] = []

    for versao, nome, sql in _migracoes_disponiveis(pasta):
        chk = _checksum(sql)
        if versao in ja:
            # Migracao ja aplicada nao pode ter mudado: alterar o arquivo depois
            # significa que os bancos ficaram diferentes entre si sem ninguem
            # perceber. Avisa alto em vez de reaplicar por conta propria.
            if ja[versao]["checksum"] and ja[versao]["checksum"] != chk:
                raise RuntimeError(
                    f"A migracao {nome} foi ALTERADA depois de aplicada "
                    f"(checksum {ja[versao]['checksum']} -> {chk}). "
                    "Crie uma migracao nova em vez de editar a antiga.")
            continue

        if not baseline:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations(versao, nome, checksum, aplicada_em,"
            " baseline) VALUES (%s,%s,%s,%s,%s)",
            (versao, nome, chk, agora, 1 if baseline else 0))
        conn.commit()
        aplicadas.append(versao)

    return aplicadas


def migracoes_status(conn) -> list[dict]:
    conn.execute(_MIGR_TABELA)
    conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT versao, nome, aplicada_em, baseline FROM schema_migrations "
        "ORDER BY versao")]


def prepare_database() -> dict:
    """Deixa o banco pronto. Unico ponto que decide schema.sql x migracoes.

    Banco NOVO   -> aplica o schema.sql (fotografia do estado atual) e MARCA as
                    migracoes como aplicadas, sem executa-las: o schema ja
                    nasceu com tudo que elas fariam.
    Banco EXISTENTE -> NAO toca no schema.sql, so aplica as migracoes pendentes.

    Por que nao rodar o schema.sql sempre: ele descreve o estado FINAL. Um
    indice novo sobre uma coluna que so a migracao acrescenta faria o
    `CREATE INDEX` falhar com "column ... does not exist" num banco antigo --
    exatamente o que aconteceu ao publicar a migracao 0001.

    Consequencia pratica: **tabela nova tambem precisa de migracao**, nao basta
    acrescentar ao schema.sql.
    """
    with connection() as conn:
        novo = banco_vazio(conn)

    if novo:
        apply_schema()

    with connection() as conn:
        aplicadas = apply_migrations(conn, baseline=novo)
    return {"novo": novo, "migracoes": aplicadas}


# ================================================================== escrita
_UPSERT = """
INSERT INTO mac_state
  (site_id, site_desc, wlan_id, wlan_name, mac, name, hostname, oui, blocked,
   in_allow_list, last_seen, last_online, first_seen, first_collected, last_collected)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s)
ON CONFLICT(site_id, mac) DO UPDATE SET
  site_desc   = excluded.site_desc,
  wlan_id     = excluded.wlan_id,
  wlan_name   = excluded.wlan_name,
  name        = CASE WHEN excluded.name     <> '' THEN excluded.name     ELSE mac_state.name     END,
  hostname    = CASE WHEN excluded.hostname <> '' THEN excluded.hostname ELSE mac_state.hostname END,
  oui         = CASE WHEN excluded.oui      <> '' THEN excluded.oui      ELSE mac_state.oui      END,
  blocked     = excluded.blocked,
  in_allow_list = 1,
  last_seen   = GREATEST(mac_state.last_seen,   excluded.last_seen),
  last_online = GREATEST(mac_state.last_online, excluded.last_online),
  first_seen  = CASE
                  WHEN mac_state.first_seen = 0 THEN excluded.first_seen
                  WHEN excluded.first_seen  = 0 THEN mac_state.first_seen
                  ELSE LEAST(mac_state.first_seen, excluded.first_seen) END,
  last_collected = excluded.last_collected
"""


def record_snapshot(conn, rows: list[dict], ts: int) -> dict:
    """Grava uma coleta, mesclando com o historico. Marca como removido (
    in_allow_list=0) quem sumiu da lista de um site presente nesta coleta."""
    had_prior = _count_collections(conn) > 0
    # estado anterior por (site, mac) para detectar transicoes
    prev = {(r["site_id"], r["mac"]): (r["in_allow_list"], r["blocked"])
            for r in conn.execute(
                "SELECT site_id, mac, in_allow_list, blocked FROM mac_state")}

    # SQLite: cur.lastrowid -> PostgreSQL: RETURNING id
    cid = conn.execute(
        "INSERT INTO collections(ts) VALUES (%s) RETURNING id", (ts,)
    ).fetchone()["id"]
    events: list[tuple] = []

    current: dict[str, set] = defaultdict(set)
    upserts: list[tuple] = []
    seen_rows: list[tuple] = []
    for r in rows:
        current[r["site_id"]].add(r["mac"])
        ls = r.get("last_seen") or 0
        eff_seen = ts if r["online"] else ls
        last_online = ts if r["online"] else 0
        new_blk = 1 if r.get("blocked") else 0
        key = (r["site_id"], r["mac"])
        p = prev.get(key)
        if p is None:
            # MAC novo no banco. So vira "evento" se ja havia historico antes
            # (na 1a coleta apenas semeamos o estado, sem poluir a auditoria).
            if had_prior:
                events.append((ts, r["site_id"], r["site_desc"], r["mac"],
                               "cadastrado", ""))
                if new_blk:
                    events.append((ts, r["site_id"], r["site_desc"], r["mac"],
                                   "bloqueado", "ja entrou bloqueado"))
        else:
            prev_in, prev_blk = p
            if prev_in == 0:
                events.append((ts, r["site_id"], r["site_desc"], r["mac"],
                               "voltou", "reapareceu na allow-list"))
            if prev_blk != new_blk:
                events.append((ts, r["site_id"], r["site_desc"], r["mac"],
                               "bloqueado" if new_blk else "desbloqueado", ""))

        upserts.append((
            r["site_id"], r["site_desc"], r["wlan_id"], r["wlan_name"], r["mac"],
            r.get("name") or "", r.get("hostname") or "", r.get("oui") or "",
            new_blk,
            eff_seen, last_online, r.get("first_seen") or 0, ts, ts,
        ))
        seen_rows.append(
            (cid, r["site_id"], r["mac"], 1 if r["online"] else 0, ls))

    # Em lote: ~1.500 MACs por coleta dariam 3.000 idas e voltas uma a uma.
    with conn.cursor() as cur:
        if upserts:
            cur.executemany(_UPSERT, upserts)
        if seen_rows:
            cur.executemany(
                "INSERT INTO seen_history(collection_id, site_id, mac, online, last_seen)"
                " VALUES (%s,%s,%s,%s,%s)", seen_rows)

    vips = vip_macs(conn)
    removed = 0
    for site_id, macs in current.items():
        existing = conn.execute(
            "SELECT mac, site_desc FROM mac_state "
            "WHERE site_id=%s AND in_allow_list=1", (site_id,)).fetchall()
        gone = [row for row in existing if row["mac"] not in macs]
        if gone:
            conn.execute(
                "UPDATE mac_state SET in_allow_list=0, last_collected=%s "
                "WHERE site_id=%s AND mac = ANY(%s)",
                (ts, site_id, [row["mac"] for row in gone]))
            for row in gone:
                is_vip = row["mac"] in vips
                events.append((ts, site_id, row["site_desc"], row["mac"],
                               "vip_removido" if is_vip else "removido",
                               "VIP/Diretoria removido!" if is_vip
                               else "saiu da allow-list"))
                removed += 1

    if events:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO events(ts, site_id, site_desc, mac, event, detail) "
                "VALUES (%s,%s,%s,%s,%s,%s)", events)
    conn.commit()
    return {"collection_id": cid, "rows": len(rows),
            "marked_removed": removed, "events": len(events)}


# ================================================================== leitura
def _latest_ts(conn) -> int:
    r = conn.execute("SELECT MAX(ts) AS t FROM collections").fetchone()
    return (r["t"] if r else 0) or 0


def _classify(last_seen, first_collected, online, now, days, never_mode):
    """Retorna (status, days_idle). days_idle = dias desde a ultima conexao."""
    if online:
        return "online", 0.0
    if last_seen and last_seen > 0:
        d = (now - last_seen) / DAY
        if d <= 7:
            return "recent", round(d, 1)
        if d <= days:
            return "idle", round(d, 1)
        if d <= 90:
            return "stale", round(d, 1)
        return "abandoned", round(d, 1)
    # nunca conectou
    if never_mode == "immediate":
        return "never", None
    age = (now - first_collected) / DAY
    return ("never", None) if age > days else ("pending", None)


def _is_online(r, latest: int) -> bool:
    """Online se a coleta mais recente o viu online.

    `latest` chega por parametro. Na versao SQLite vinha de um dict global de
    modulo (_LATEST) que site_inventory/overview_summary escreviam e esta
    funcao lia -- com gunicorn multi-thread, duas requisicoes concorrentes se
    sobrescreviam e o status "online" saia errado.
    """
    return bool(r["last_online"] and r["last_online"] == latest)


def _row_view(r, now, days, never_mode, latest: int):
    status, didle = _classify(r["last_seen"], r["first_collected"],
                              _is_online(r, latest), now, days, never_mode)
    return {
        "mac": r["mac"], "name": r["name"] or "", "oui": r["oui"] or "",
        "online": status == "online",
        "last_seen": r["last_seen"] or None,
        "days_idle": didle,
        "status": status, "status_label": STATUS_LABEL[status],
        "unused": status in UNUSED_STATUSES,
        "blocked": bool(r["blocked"]),
        "first_collected": r["first_collected"],
    }


def site_inventory(conn, site_id, wlan_id, days=AVAILABLE_DAYS,
                   never_mode="grace", cap=MAC_FILTER_CAP):
    latest = _latest_ts(conn)
    now = int(time.time())
    q = ("SELECT * FROM mac_state WHERE site_id=%s AND in_allow_list=1"
         + (" AND wlan_id=%s" if wlan_id else ""))
    args = (site_id, wlan_id) if wlan_id else (site_id,)
    db_rows = conn.execute(q, args).fetchall()
    rows = [_row_view(r, now, days, never_mode, latest) for r in db_rows]

    vips = vip_macs(conn)
    for r in rows:
        r["vip"] = r["mac"] in vips

    counts = {k: 0 for k in STATUS_LABEL}
    for r in rows:
        counts[r["status"]] += 1

    def sort_key(r):
        d = r["days_idle"] if r["days_idle"] is not None else 10**9
        return (0 if r["unused"] else 1, -d if r["unused"] else d)
    rows.sort(key=sort_key)

    total = len(rows)
    unused = sum(1 for r in rows if r["unused"])
    blocked = sum(1 for r in rows if r["blocked"])

    def _parado(r, n):
        return (not r["online"]) and r["days_idle"] is not None and r["days_idle"] > n
    d50 = sum(1 for r in rows if _parado(r, 50))
    d100 = sum(1 for r in rows if _parado(r, 100))
    summary = {
        "total": total, "cap": cap, "free_slots": cap - total,
        "is_full": total >= cap, "unused": unused, "in_use": total - unused,
        "blocked": blocked, "d50": d50, "d100": d100,
        "counts": counts, "stale_days": days,
    }
    wlan_name = db_rows[0]["wlan_name"] if db_rows else None
    return {"rows": rows, "summary": summary,
            "wlan": {"_id": wlan_id, "name": wlan_name}}


def overview_summary(conn, days=AVAILABLE_DAYS, never_mode="grace",
                     cap=MAC_FILTER_CAP):
    latest = _latest_ts(conn)
    now = int(time.time())
    db_rows = conn.execute(
        "SELECT * FROM mac_state WHERE in_allow_list=1").fetchall()

    per = {}  # (site_id) -> agregado
    tot = {"total": 0, "online": 0, "over_x": 0, "never": 0,
           "reclaimable": 0, "in_use": 0, "free": 0, "blocked": 0,
           "d50": 0, "d100": 0}
    # bandas mutuamente exclusivas para o grafico de distribuicao
    dist = {"online": 0, "ate30": 0, "31-50": 0, "51-100": 0, ">100": 0, "never": 0}

    for r in db_rows:
        v = _row_view(r, now, days, never_mode, latest)
        sid = r["site_id"]
        p = per.setdefault(sid, {
            "site_id": sid, "site_desc": r["site_desc"],
            "wlan_id": r["wlan_id"], "wlan_name": r["wlan_name"],
            "total": 0, "online": 0, "over_x": 0, "never": 0,
            "reclaimable": 0, "blocked": 0, "d50": 0, "d100": 0, "cap": cap,
        })
        p["total"] += 1
        tot["total"] += 1
        if v["blocked"]:
            p["blocked"] += 1; tot["blocked"] += 1
        if v["status"] == "online":
            p["online"] += 1; tot["online"] += 1; dist["online"] += 1
        if v["unused"]:
            p["reclaimable"] += 1; tot["reclaimable"] += 1
            if v["status"] == "never":
                p["never"] += 1; tot["never"] += 1
            else:
                p["over_x"] += 1; tot["over_x"] += 1
        # faixas por dias parado (usa last_seen real); d50/d100 sao cumulativos
        if v["status"] != "online":
            ls = r["last_seen"]
            if not ls:
                dist["never"] += 1
            else:
                d = (now - ls) / DAY
                if d > 100:
                    dist[">100"] += 1
                elif d > 50:
                    dist["51-100"] += 1
                elif d > 30:
                    dist["31-50"] += 1
                else:
                    dist["ate30"] += 1
                if d > 50:
                    p["d50"] += 1; tot["d50"] += 1
                if d > 100:
                    p["d100"] += 1; tot["d100"] += 1

    sites = []
    for p in per.values():
        p["free"] = cap - p["total"]
        p["is_full"] = p["total"] >= cap
        p["in_use"] = p["total"] - p["reclaimable"]
        p["pct"] = round(p["total"] / cap * 100) if cap else 0
        sites.append(p)
    sites.sort(key=lambda r: r["reclaimable"], reverse=True)
    tot["in_use"] = tot["total"] - tot["reclaimable"]
    tot["free"] = sum(s["free"] for s in sites)

    return {"totals": tot, "sites": sites, "dist": dist, "days": days,
            "latest_ts": latest, "collections": _count_collections(conn)}


def _count_collections(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM collections").fetchone()["c"]


def site_wlans(conn, site_id) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT wlan_id, wlan_name FROM mac_state "
        "WHERE site_id=%s AND in_allow_list=1", (site_id,)).fetchall()
    return [{"_id": r["wlan_id"], "name": r["wlan_name"]}
            for r in rows if r["wlan_id"]]


def has_data(conn) -> bool:
    return _count_collections(conn) > 0


# ================================================================= settings
def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)))
    conn.commit()


def get_or_create_setting(conn, key, factory) -> str:
    """Le a chave; se nao existir, grava o valor de factory() e devolve o que
    de fato ficou gravado.

    DO NOTHING + releitura (em vez de set_setting) porque varios workers sobem
    ao mesmo tempo: com DO UPDATE, dois deles gerariam segredos diferentes e o
    ultimo sobrescreveria o primeiro, invalidando as sessoes ja emitidas.
    Aqui o primeiro a gravar vence e todos leem o mesmo valor.
    """
    r = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    if r and r["value"]:
        return r["value"]
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING",
        (key, factory()))
    conn.commit()
    return conn.execute(
        "SELECT value FROM settings WHERE key=%s", (key,)).fetchone()["value"]


# ================================================ credenciais por usuario
# Modelo hibrido: cada pessoa entra com a propria conta UniFi (como no desktop).
# As telas e as acoes de escrita usam a conta de quem esta logado -- assim o log
# nativo da UniFi registra o autor real. O coletor, que roda sem ninguem logado,
# usa a credencial mais recente que comprovadamente funcionou.
def save_user_creds(conn, username, host, site, verify, password) -> None:
    from . import secret
    enc = secret.encrypt(conn, password)
    now = int(time.time())
    conn.execute(
        "INSERT INTO user_creds(username, host, site, verify, password_enc, "
        "updated_at, last_ok) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(username) DO UPDATE SET host=excluded.host, "
        "site=excluded.site, verify=excluded.verify, "
        "password_enc=excluded.password_enc, updated_at=excluded.updated_at, "
        "last_ok=excluded.last_ok",
        (username, host, site or "default", 1 if verify else 0, enc, now, now))
    conn.commit()


def _creds_view(conn, r) -> dict | None:
    from . import secret
    pw = secret.decrypt(conn, r["password_enc"])
    if not pw:
        return None          # chave trocada ou dado corrompido: inutilizavel
    return {"username": r["username"], "host": r["host"], "site": r["site"],
            "verify": bool(r["verify"]), "password": pw,
            "updated_at": r["updated_at"], "last_ok": r["last_ok"]}


def get_user_creds(conn, username) -> dict | None:
    r = conn.execute("SELECT * FROM user_creds WHERE username=%s",
                     (username,)).fetchone()
    return _creds_view(conn, r) if r else None


def mark_creds_ok(conn, username) -> None:
    conn.execute("UPDATE user_creds SET last_ok=%s WHERE username=%s",
                 (int(time.time()), username))
    conn.commit()


def collector_creds(conn, limit: int = 5) -> list[dict]:
    """Credenciais candidatas para o coletor, da mais confiavel para a menos.

    Ordena por last_ok: a que autenticou mais recentemente vem primeiro. Devolve
    varias porque a primeira pode ter deixado de valer (a pessoa trocou a senha
    no dominio, a conta foi desativada) -- nesse caso o coletor tenta a proxima
    em vez de simplesmente parar de coletar.
    """
    rows = conn.execute(
        "SELECT * FROM user_creds ORDER BY last_ok DESC NULLS LAST, "
        "updated_at DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        c = _creds_view(conn, r)
        if c:
            out.append(c)
    return out


def list_user_creds(conn) -> list[dict]:
    """Para a tela de configuracao. NUNCA devolve a senha."""
    return [{"username": r["username"], "host": r["host"], "site": r["site"],
             "verify": bool(r["verify"]), "updated_at": r["updated_at"],
             "last_ok": r["last_ok"]}
            for r in conn.execute(
                "SELECT username, host, site, verify, updated_at, last_ok "
                "FROM user_creds ORDER BY last_ok DESC NULLS LAST")]


def delete_user_creds(conn, username) -> None:
    conn.execute("DELETE FROM user_creds WHERE username=%s", (username,))
    conn.commit()


# ================================================== portal: usuarios locais
# Autenticacao SEPARADA do login administrativo. O admin entra com a conta do
# UniFi; quem so retira voucher (portaria, recepcao, lider) nao tem conta no
# controller e recebe credencial local criada pela TI.
PORTAL_MAX_TENTATIVAS = 5
PORTAL_BLOQUEIO_SEG = 900          # 15 min


def create_portal_user(conn, username, senha, nome="", setor="", unidade="",
                       criado_por="") -> int:
    from . import secret
    now = int(time.time())
    r = conn.execute(
        "INSERT INTO portal_users(username, nome, setor, unidade, password_hash,"
        " ativo, must_change, criado_por, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,1,1,%s,%s,%s) RETURNING id",
        (username.strip().lower(), nome, setor, unidade,
         secret.hash_password(senha), criado_por, now, now)).fetchone()
    conn.commit()
    return r["id"]


def get_portal_user(conn, username=None, user_id=None) -> dict | None:
    if user_id is not None:
        r = conn.execute("SELECT * FROM portal_users WHERE id=%s",
                         (user_id,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM portal_users WHERE username=%s",
                         ((username or "").strip().lower(),)).fetchone()
    return dict(r) if r else None


def list_portal_users(conn) -> list[dict]:
    """Sem o hash da senha: esta lista vai para a tela."""
    return [dict(r) for r in conn.execute(
        "SELECT id, username, nome, setor, unidade, ativo, must_change, "
        "criado_por, created_at, updated_at, last_login, locked_until "
        "FROM portal_users ORDER BY nome NULLS LAST, username")]


def set_portal_password(conn, user_id, senha, must_change=False) -> None:
    from . import secret
    conn.execute(
        "UPDATE portal_users SET password_hash=%s, must_change=%s, "
        "updated_at=%s, failed_count=0, locked_until=NULL WHERE id=%s",
        (secret.hash_password(senha), 1 if must_change else 0,
         int(time.time()), user_id))
    conn.commit()


def update_portal_user(conn, user_id, nome, setor, unidade, ativo) -> None:
    conn.execute(
        "UPDATE portal_users SET nome=%s, setor=%s, unidade=%s, ativo=%s, "
        "updated_at=%s WHERE id=%s",
        (nome, setor, unidade, 1 if ativo else 0, int(time.time()), user_id))
    conn.commit()


def delete_portal_user(conn, user_id) -> None:
    conn.execute("DELETE FROM portal_users WHERE id=%s", (user_id,))
    conn.commit()


def check_portal_login(conn, username, senha) -> tuple[dict | None, str]:
    """Valida o acesso ao portal. Devolve (usuario, motivo_da_recusa).

    Trava a conta por PORTAL_BLOQUEIO_SEG apos PORTAL_MAX_TENTATIVAS erros.
    O portal usa senha simples -- bem mais fraca que a validacao contra o
    controller do lado administrativo -- entao o freio contra forca bruta e
    parte do desenho, nao um extra.
    """
    from . import secret
    u = get_portal_user(conn, username=username)
    if not u:
        # custo de hash mesmo sem usuario, para nao vazar quem existe pelo tempo
        secret.verify_password(
            "scrypt:32768:8:1$x$0" * 1, senha or "")
        return None, "Usuário ou senha inválidos."
    if not u["ativo"]:
        return None, "Usuário desativado. Procure a TI."

    agora = int(time.time())
    if u["locked_until"] and u["locked_until"] > agora:
        faltam = (u["locked_until"] - agora + 59) // 60
        return None, f"Conta bloqueada por tentativas seguidas. Tente em {faltam} min."

    if not secret.verify_password(u["password_hash"], senha):
        falhas = (u["failed_count"] or 0) + 1
        trava = agora + PORTAL_BLOQUEIO_SEG if falhas >= PORTAL_MAX_TENTATIVAS else None
        conn.execute(
            "UPDATE portal_users SET failed_count=%s, locked_until=%s WHERE id=%s",
            (falhas, trava, u["id"]))
        conn.commit()
        if trava:
            return None, ("Conta bloqueada por 15 minutos após "
                          f"{PORTAL_MAX_TENTATIVAS} tentativas.")
        return None, "Usuário ou senha inválidos."

    conn.execute(
        "UPDATE portal_users SET failed_count=0, locked_until=NULL, "
        "last_login=%s WHERE id=%s", (agora, u["id"]))
    conn.commit()
    u["failed_count"] = 0
    u["locked_until"] = None
    u["last_login"] = agora
    return u, ""


# ==================================================== vouchers de hotspot
VOUCHER_CAMPOS = ("code", "voucher_id", "site_id", "site_desc", "note", "quota",
                  "duration_min", "data_limit_mb", "down_kbps", "up_kbps",
                  "portal_user_id", "criado_por", "create_time", "created_at")


def record_voucher_grants(conn, linhas: list[dict]) -> int:
    """Guarda os vouchers recem-criados. ON CONFLICT protege contra reenvio."""
    if not linhas:
        return 0
    agora = int(time.time())
    payload = [
        tuple(r.get(c) if c != "created_at" else (r.get("created_at") or agora)
              for c in VOUCHER_CAMPOS)
        for r in linhas
    ]
    cols = ", ".join(VOUCHER_CAMPOS)
    ph = ", ".join(["%s"] * len(VOUCHER_CAMPOS))
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO voucher_grants({cols}) VALUES ({ph}) "
            f"ON CONFLICT (site_id, code) DO NOTHING", payload)
        n = cur.rowcount
    conn.commit()
    return max(n, 0)


_VG_SELECT = """
SELECT g.*, p.username AS portal_username, p.nome AS portal_nome
FROM voucher_grants g
LEFT JOIN portal_users p ON p.id = g.portal_user_id
"""


def list_voucher_grants(conn, site_id=None, portal_user_id=None,
                        somente_ativos=False, somente_disponiveis=False,
                        search=None, create_time=None, ids=None,
                        limit=500) -> list[dict]:
    q = _VG_SELECT
    where, args = [], []
    if site_id:
        where.append("g.site_id=%s"); args.append(site_id)
    if portal_user_id is not None:
        where.append("g.portal_user_id=%s"); args.append(portal_user_id)
    if create_time:
        # identifica um LOTE: a UniFi carimba o mesmo create_time em todos os
        # vouchers gerados na mesma operacao
        where.append("g.create_time=%s"); args.append(create_time)
    if ids:
        # impressao parcial: so os itens marcados na tela
        where.append("g.id = ANY(%s)"); args.append(list(ids))
    if somente_ativos:
        where.append("g.revogado_em IS NULL")
    if somente_disponiveis:
        # o que ainda funciona: nao revogado, nao usado e ainda no controller.
        # status NULL = nunca sincronizado, entao entra por precaucao.
        where.append(
            "g.revogado_em IS NULL AND g.used = 0 "
            "AND (g.status IS NULL OR g.status <> ALL(%s))")
        args.append(list(VOUCHER_STATUS_MORTOS))
    if search:
        s = f"%{search.strip()}%"
        where.append("(g.code LIKE %s OR g.note LIKE %s OR p.nome LIKE %s "
                     "OR p.username LIKE %s)")
        args += [s, s, s, s]
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY g.created_at DESC, g.id DESC LIMIT %s"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


# Como a UniFi classifica o voucher, mais dois status nossos (USADO, EXPIRADO),
# deduzidos do desaparecimento -- ver sync_voucher_status.
VOUCHER_STATUS_LABEL = {
    "VALID_ONE": "Disponível",
    "VALID_MULTI": "Disponível (multi)",
    "USED_UPDATED": "Já usado",
    "EXPIRED": "Expirado",
    "USADO": "Usado",
    "EXPIRADO": "Expirado",
    # gravado por uma versao anterior, antes de sabermos distinguir os dois
    "AUSENTE": "Fora do controller",
}
# Status que tiram o voucher da lista de entrega.
#
# AUSENTE continua aqui por compatibilidade: bancos que sincronizaram com a
# versao anterior tem linhas assim. Sem isso elas reapareceriam como
# disponiveis logo apos a atualizacao, ate a proxima sincronizacao reclassifica-
# las -- uma janela curta, mas em que alguem poderia imprimir codigo morto.
VOUCHER_STATUS_MORTOS = ("USED_UPDATED", "EXPIRED", "USADO", "EXPIRADO",
                         "AUSENTE")


def sync_voucher_status(conn, site_id: str, vouchers: list[dict]) -> dict:
    """Atualiza a situacao a partir do que o controller devolve.

    Como a UniFi sinaliza o uso
    ---------------------------
    Ela NAO marca o voucher como usado: **remove o registro da lista**. Um
    voucher de uso unico consumido simplesmente deixa de aparecer em
    stat/voucher. Foi o que se observou em producao -- o codigo usado sumiu de
    todos os sites, e o campo `used` continuou 0 em todos os que restaram.

    Entao a deteccao e o DESAPARECIMENTO, nao o campo `used`.

    Sumiu por uso ou por vencimento?
    --------------------------------
    Dedu-se pela validade: se o voucher sumiu ANTES de vencer, foi usado; se
    depois, expirou. Nao e informacao que a UniFi entregue, mas a distincao
    importa para quem administra -- "usado" e consumo normal, "expirado" e
    voucher entregue e desperdicado.

    Em ambos os casos ele sai da lista de entrega: um codigo que nao existe
    mais no controller nao funciona para ninguem.
    """
    agora = int(time.time())
    do_controller = {v.get("code"): v for v in vouchers if v.get("code")}

    nossos = conn.execute(
        "SELECT id, code, created_at, duration_min FROM voucher_grants "
        "WHERE site_id=%s AND revogado_em IS NULL", (site_id,)).fetchall()
    if not nossos:
        return {"atualizados": 0, "usados": 0, "expirados": 0}

    presentes, sumidos = [], []
    for r in nossos:
        v = do_controller.get(r["code"])
        if v is not None:
            presentes.append((int(v.get("used") or 0),
                              v.get("status") or "", agora, r["id"]))
            continue
        vence_em = (r["created_at"] or 0) + (r["duration_min"] or 0) * 60
        # margem de 5 min: a coleta e periodica, entao o instante exato do
        # sumico nao e conhecido -- perto do vencimento, assume vencimento
        usado = bool(vence_em) and agora < vence_em - 300
        sumidos.append(("USADO" if usado else "EXPIRADO",
                        1 if usado else 0, agora, r["id"]))

    with conn.cursor() as cur:
        if presentes:
            cur.executemany(
                "UPDATE voucher_grants SET used=%s, status=%s, synced_at=%s "
                "WHERE id=%s", presentes)
        if sumidos:
            cur.executemany(
                "UPDATE voucher_grants SET status=%s, used=%s, synced_at=%s "
                "WHERE id=%s", sumidos)
    conn.commit()
    return {"atualizados": len(presentes),
            "usados": sum(1 for s in sumidos if s[0] == "USADO"),
            "expirados": sum(1 for s in sumidos if s[0] == "EXPIRADO")}


def get_voucher_grant(conn, grant_id) -> dict | None:
    r = conn.execute(_VG_SELECT + " WHERE g.id=%s", (grant_id,)).fetchone()
    return dict(r) if r else None


def mark_voucher_retirado(conn, grant_id) -> None:
    """Primeira vez que a pessoa viu o codigo no portal (nao sobrescreve)."""
    conn.execute(
        "UPDATE voucher_grants SET retirado_em=%s "
        "WHERE id=%s AND retirado_em IS NULL", (int(time.time()), grant_id))
    conn.commit()


def mark_voucher_revogado(conn, grant_id, quem) -> None:
    conn.execute(
        "UPDATE voucher_grants SET revogado_em=%s, revogado_por=%s WHERE id=%s",
        (int(time.time()), quem, grant_id))
    conn.commit()


def voucher_alerts(conn, dias: int = 7) -> list[dict]:
    """Vouchers MULTI-USO ou ILIMITADOS gerados recentemente.

    Voucher de uso unico se esgota sozinho; multi-uso e ilimitado circulam --
    um codigo repassado adiante libera acesso indefinidamente. Por isso toda
    geracao desse tipo fica visivel num alerta, com o nome de quem gerou, em vez
    de ficar so no historico. Mesma logica do alerta de VIP fora da allow-list.
    """
    corte = int(time.time()) - dias * DAY
    rows = conn.execute("""
        SELECT g.id, g.code, g.note, g.quota, g.site_desc, g.criado_por,
               g.created_at, p.nome AS portal_nome
        FROM voucher_grants g
        LEFT JOIN portal_users p ON p.id = g.portal_user_id
        WHERE g.quota <> 1 AND g.revogado_em IS NULL AND g.created_at >= %s
        ORDER BY g.created_at DESC
    """, (corte,)).fetchall()
    return [dict(r) for r in rows]


def voucher_stats(conn) -> dict:
    r = conn.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE revogado_em IS NULL) AS ativos,
               COUNT(*) FILTER (WHERE retirado_em IS NOT NULL) AS retirados,
               COUNT(*) FILTER (WHERE portal_user_id IS NOT NULL) AS atribuidos,
               COUNT(*) FILTER (WHERE used > 0) AS usados,
               COUNT(*) FILTER (WHERE status IN ('EXPIRADO','EXPIRED','AUSENTE')) AS expirados,
               COUNT(*) FILTER (
                   WHERE revogado_em IS NULL AND used = 0
                     AND (status IS NULL OR status NOT IN
                          ('USED_UPDATED','EXPIRED','USADO','EXPIRADO','AUSENTE'))
               ) AS disponiveis,
               MAX(synced_at) AS ultima_sync
        FROM voucher_grants
    """).fetchone()
    return dict(r)


# ==================================================== travas de edicao (locks)
def get_lock(conn, mac) -> dict | None:
    r = conn.execute("SELECT who, ts FROM edit_locks WHERE mac=%s",
                     (mac.lower(),)).fetchone()
    return dict(r) if r else None


def set_lock(conn, mac, who, ts) -> None:
    conn.execute(
        "INSERT INTO edit_locks(mac, who, ts) VALUES(%s,%s,%s) "
        "ON CONFLICT(mac) DO UPDATE SET who=excluded.who, ts=excluded.ts",
        (mac.lower(), who, ts))
    conn.commit()


# =========================================================== cadastro cliente
def get_client_info(conn, mac) -> dict | None:
    r = conn.execute("SELECT * FROM client_info WHERE mac=%s",
                     (mac.lower(),)).fetchone()
    return dict(r) if r else None


def upsert_client_info(conn, mac, fields: dict) -> None:
    mac = mac.lower()
    now = int(time.time())
    cols = ",".join(CLIENT_FIELDS)
    ph = ",".join(["%s"] * len(CLIENT_FIELDS))
    sets = ",".join(f"{k}=excluded.{k}" for k in CLIENT_FIELDS)
    vals = [(fields.get(k) or "").strip() for k in CLIENT_FIELDS]
    conn.execute(
        f"INSERT INTO client_info(mac,{cols},created_at,updated_at) "
        f"VALUES(%s,{ph},%s,%s) "
        f"ON CONFLICT(mac) DO UPDATE SET {sets}, updated_at=excluded.updated_at",
        [mac] + vals + [now, now])
    conn.commit()


def set_vip(conn, mac, vip: bool) -> None:
    """Marca/desmarca um MAC como prioritario (VIP/Diretoria)."""
    conn.execute("UPDATE client_info SET vip=%s, updated_at=%s WHERE mac=%s",
                 (1 if vip else 0, int(time.time()), mac.lower()))
    conn.commit()


def set_termo(conn, mac, termo: bool) -> None:
    """Marca/desmarca se o termo do cliente foi assinado/entregue."""
    conn.execute("UPDATE client_info SET termo=%s, updated_at=%s WHERE mac=%s",
                 (1 if termo else 0, int(time.time()), mac.lower()))
    conn.commit()


def vip_alerts(conn) -> list[dict]:
    """MACs marcados como VIP que NAO estao mais na allow-list (alerta!)."""
    rows = conn.execute("""
        SELECT c.mac, c.nome, c.setor FROM client_info c
        WHERE c.vip = 1 AND NOT EXISTS (
            SELECT 1 FROM mac_state m
            WHERE m.mac = c.mac AND m.in_allow_list = 1)
    """).fetchall()
    return [{"mac": r["mac"], "nome": r["nome"] or "", "setor": r["setor"] or ""}
            for r in rows]


def vip_macs(conn) -> set:
    return {r["mac"] for r in conn.execute(
        "SELECT mac FROM client_info WHERE vip=1")}


def device_detail(conn, mac) -> dict | None:
    """Tudo que sabemos do aparelho (infos da UniFi mescladas) + presenca por site."""
    mac = mac.lower()
    rows = conn.execute("SELECT * FROM mac_state WHERE mac=%s", (mac,)).fetchall()
    if not rows:
        return None
    latest = _latest_ts(conn)

    def first(attr):
        return next((r[attr] for r in rows if r[attr]), "")

    last_online = max((r["last_online"] or 0) for r in rows)
    fseen = [r["first_seen"] for r in rows if r["first_seen"]]
    sites = [{
        "site_id": r["site_id"], "site_desc": r["site_desc"],
        "wlan_name": r["wlan_name"], "in_list": bool(r["in_allow_list"]),
        "blocked": bool(r["blocked"]), "last_seen": r["last_seen"] or None,
    } for r in rows]
    return {
        "mac": mac,
        "device_name": first("name"),
        "hostname": first("hostname"),
        "oui": first("oui"),
        "last_seen": max((r["last_seen"] or 0) for r in rows) or None,
        "first_seen": min(fseen) if fseen else None,
        "online": bool(last_online and last_online == latest),
        "active": any(r["in_allow_list"] for r in rows),
        "blocked": any(r["blocked"] for r in rows),
        "sites": sorted(sites, key=lambda s: (not s["in_list"], s["site_desc"] or "")),
    }


# GROUP_CONCAT(DISTINCT x) do SQLite -> string_agg(DISTINCT x, ', ') no PostgreSQL.
_AGG = """
SELECT mac,
  MAX(in_allow_list)            AS active,
  MAX(blocked)                  AS blocked,
  MAX(NULLIF(name,''))          AS name,
  MAX(NULLIF(hostname,''))      AS hostname,
  MAX(NULLIF(oui,''))           AS oui,
  MAX(COALESCE(last_seen,0))    AS last_seen,
  MAX(COALESCE(last_online,0))  AS last_online,
  string_agg(DISTINCT CASE WHEN in_allow_list=1 THEN site_desc END, ', ') AS sites
FROM mac_state GROUP BY mac
"""


def list_clients(conn, status="active", search=None) -> list[dict]:
    """status: active | removed | all. Junta info do aparelho + cadastro."""
    latest = _latest_ts(conn)
    info = {r["mac"]: dict(r) for r in conn.execute("SELECT * FROM client_info")}
    q = (search or "").strip().lower()

    def _match(row):
        if not q:
            return True
        blob = " ".join(str(row.get(k, "")) for k in
                        ("mac", "device_name", "hostname", "sites", *CLIENT_FIELDS)).lower()
        return q in blob

    out = []
    seen = set()
    for r in conn.execute(_AGG):
        mac = r["mac"]
        seen.add(mac)
        active = bool(r["active"])
        ci = info.get(mac)
        if status == "active" and not active:
            continue
        if status == "removed" and (active or not ci):
            continue
        row = {
            "mac": mac, "active": active, "blocked": bool(r["blocked"]),
            "device_name": r["name"] or "", "hostname": r["hostname"] or "",
            "oui": r["oui"] or "", "last_seen": r["last_seen"] or None,
            "online": bool(r["last_online"] and r["last_online"] == latest),
            "sites": r["sites"] or "", "vip": bool((ci or {}).get("vip")),
            "termo": bool((ci or {}).get("termo")), "has_info": bool(ci),
        }
        for k in CLIENT_FIELDS:
            row[k] = (ci or {}).get(k, "")
        if _match(row):
            out.append(row)

    # MACs que existem so no cadastro (importados, nunca vistos na rede) -> removidos
    if status in ("removed", "all"):
        for mac, ci in info.items():
            if mac in seen:
                continue
            row = {
                "mac": mac, "active": False, "blocked": False,
                "device_name": "", "hostname": "", "oui": "",
                "last_seen": None, "online": False, "sites": "",
                "vip": bool(ci.get("vip")),
                "termo": bool(ci.get("termo")), "has_info": True,
            }
            for k in CLIENT_FIELDS:
                row[k] = ci.get(k, "")
            if _match(row):
                out.append(row)

    out.sort(key=lambda x: (x["nome"] or x["device_name"] or x["mac"]).lower())
    return out


def _fmt(ts) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else ""


def backup_rows(conn) -> list[dict]:
    """Uma linha por (mac, site) de TODOS os aparelhos ja vistos na mobile
    (inclui os que sairam da lista) + cadastro do cliente. Base do backup."""
    info = {r["mac"]: dict(r) for r in conn.execute("SELECT * FROM client_info")}
    latest = _latest_ts(conn)
    out = []
    for r in conn.execute("SELECT * FROM mac_state ORDER BY mac, site_desc"):
        ci = info.get(r["mac"], {})
        out.append({
            "mac": r["mac"],
            "site": r["site_desc"] or r["site_id"],
            "wlan": r["wlan_name"] or "",
            "na_lista": "sim" if r["in_allow_list"] else "nao",
            "bloqueado": "sim" if r["blocked"] else "nao",
            "device_name": r["name"] or "",
            "hostname": r["hostname"] or "",
            "fabricante": r["oui"] or "",
            "online": "sim" if (r["last_online"] and r["last_online"] == latest) else "nao",
            "ultimo_acesso": _fmt(r["last_seen"]),
            "primeiro_acesso": _fmt(r["first_seen"]),
            "vip": "sim" if ci.get("vip") else "nao",
            "termo": "sim" if ci.get("termo") else "nao",
            "nome": ci.get("nome", ""), "setor": ci.get("setor", ""),
            "unidade": ci.get("unidade", ""), "funcao": ci.get("funcao", ""),
            "lider": ci.get("lider", ""),
            "gestor_autorizou": ci.get("gestor_autorizou", ""),
            "chamado": ci.get("chamado", ""), "notes": ci.get("notes", ""),
        })
    return out


# ================================================================ auditoria
EVENT_LABEL = {
    "cadastrado": "Cadastrado", "voltou": "Voltou", "removido": "Removido",
    "bloqueado": "Bloqueado", "desbloqueado": "Desbloqueado",
    "vip_removido": "VIP REMOVIDO",
    "add_manual": "Adicionado (manual)", "remove_manual": "Removido (manual)",
    "troca": "Troca de aparelho",
    "voucher_criado": "Voucher gerado",
    "voucher_multi": "Voucher MULTI-USO gerado",
    "voucher_revogado": "Voucher revogado",
}


def add_event(conn, ts, site_id, site_desc, mac, event, detail="") -> None:
    conn.execute(
        "INSERT INTO events(ts, site_id, site_desc, mac, event, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (ts, site_id, site_desc, mac.lower(), event, detail))
    conn.commit()


def events_for_mac(conn, mac, limit=200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE mac=%s ORDER BY ts DESC, id DESC LIMIT %s",
        (mac.lower(), limit)).fetchall()
    return [dict(r) for r in rows]


def recent_events(conn, event=None, search=None, limit=500) -> list[dict]:
    q = "SELECT * FROM events"
    args, where = [], []
    if event:
        where.append("event=%s"); args.append(event)
    if search:
        s = f"%{search.strip()}%"
        where.append("(mac LIKE %s OR site_desc LIKE %s)"); args += [s, s]
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC, id DESC LIMIT %s"; args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def events_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]


def last_event_ts(conn) -> dict:
    """{mac: {'removido': ts, 'voltou': ts}} com o ts mais recente de cada tipo."""
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT mac, event, MAX(ts) AS ts FROM events "
        "WHERE event IN ('removido','voltou') GROUP BY mac, event"):
        out.setdefault(r["mac"], {})[r["event"]] = r["ts"]
    return out


def removed_macs_with_info(conn) -> int:
    """Quantos cadastros existem cujo MAC nao esta mais em nenhuma allow-list."""
    r = conn.execute("""
        SELECT COUNT(*) AS c FROM client_info c
        WHERE NOT EXISTS (
            SELECT 1 FROM mac_state m
            WHERE m.mac = c.mac AND m.in_allow_list = 1)
    """).fetchone()
    return r["c"]


# ==================================== log nativo da UniFi / presenca / leases
def upsert_unifi_audit(conn, rows) -> int:
    """Insere registros do log nativo da UniFi (dedup por uid). Retorna novos.

    INSERT OR IGNORE (SQLite) -> ON CONFLICT DO NOTHING (PostgreSQL).
    """
    if not rows:
        return 0
    now = int(time.time())
    payload = [
        (r["uid"], r.get("ts"), r.get("site_id"), r.get("site_desc"),
         r.get("key"), r.get("operation"), r.get("actor"),
         r.get("message"), r.get("raw"), now)
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO unifi_audit"
            "(uid, ts, site_id, site_desc, key, operation, actor, message, raw, imported_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT(uid) DO NOTHING",
            payload)
        # Apos executemany, rowcount e o total afetado por TODAS as execucoes.
        # Como o conflito nao afeta linha nenhuma, isso ja e a contagem de novos.
        novos = cur.rowcount
    conn.commit()
    return max(novos, 0)


def list_unifi_audit(conn, search=None, limit=500) -> list[dict]:
    q = "SELECT * FROM unifi_audit"
    args = []
    if search:
        s = f"%{search.strip()}%"
        q += " WHERE (actor LIKE %s OR message LIKE %s OR site_desc LIKE %s OR key LIKE %s)"
        args = [s, s, s, s]
    q += " ORDER BY ts DESC, uid DESC LIMIT %s"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def unifi_audit_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM unifi_audit").fetchone()["c"]


# ------------------------------------------------------------------ presenca
def ping_session(conn, sid, who, machine) -> None:
    conn.execute(
        "INSERT INTO active_sessions(sid, who, machine, last_ping) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT(sid) DO UPDATE SET who=excluded.who, machine=excluded.machine, "
        "last_ping=excluded.last_ping",
        (sid, who or "", machine or "", int(time.time())))
    conn.commit()


def end_session(conn, sid) -> None:
    conn.execute("DELETE FROM active_sessions WHERE sid=%s", (sid,))
    conn.commit()


def active_sessions(conn, ttl=90) -> list[dict]:
    cut = int(time.time()) - ttl
    conn.execute("DELETE FROM active_sessions WHERE last_ping < %s", (cut,))
    conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT who, machine, last_ping FROM active_sessions ORDER BY who")]


def active_count(conn, ttl=90) -> int:
    return len(active_sessions(conn, ttl))


# --------------------------------------------- lease de coleta (turno unico)
def claim_collection(conn, interval, who="") -> bool:
    """Assume a coleta da janela atual de forma atomica. True = deve coletar.

    O UPDATE condicional so acerta a linha para UM chamador: quem obtiver
    rowcount==1 ganhou a janela. Mesma semantica da versao SQLite.

    Mudanca: usa a tabela collect_lease (coluna BIGINT) em vez de
    settings['last_collect_ts'] (TEXT). O CAST(value AS INTEGER) sobre TEXT
    devolvia 0 silenciosamente no SQLite, mas LANCA EXCECAO no PostgreSQL
    quando o valor esta vazio ou nao e numerico.
    """
    now = int(time.time())
    cur = conn.execute(
        "UPDATE collect_lease SET last_ts=%s, last_by=%s "
        "WHERE id=1 AND last_ts <= %s", (now, who, now - interval))
    conn.commit()
    return cur.rowcount == 1


def last_collect_info(conn) -> dict:
    r = conn.execute(
        "SELECT last_ts, last_by FROM collect_lease WHERE id=1").fetchone()
    return dict(r) if r else {"last_ts": 0, "last_by": ""}


# ------------------------------------------- trava de escrita por WLAN
# TTL 60s (era 20s no SQLite). O caminho de escrita faz get_wlan + PUT contra o
# controller, com timeout de 15s por chamada e a possibilidade de refazer login
# em caso de 401 -- ou seja, pode passar de 30s. Com TTL de 20s a trava expirava
# no meio da operacao e um segundo worker entrava junto, arriscando estourar o
# cap de 512.
WLAN_LOCK_TTL = 60


def acquire_wlan_lock(conn, key, who, ttl=WLAN_LOCK_TTL) -> bool:
    """Trava exclusiva por WLAN.

    O INSERT ... ON CONFLICT DO NOTHING e atomico no PostgreSQL, entao a trava
    vale entre PROCESSOS e entre CONTAINERES -- diferente do threading.Lock da
    versao desktop, que so valia dentro de um processo e deixava de proteger
    assim que o app passasse a rodar com varios workers.
    """
    now = int(time.time())
    conn.execute("DELETE FROM wlan_locks WHERE ts < %s", (now - ttl,))
    cur = conn.execute(
        "INSERT INTO wlan_locks(key, who, ts) VALUES (%s,%s,%s) "
        "ON CONFLICT(key) DO NOTHING", (key, who or "", now))
    conn.commit()
    return cur.rowcount == 1


def release_wlan_lock(conn, key) -> None:
    conn.execute("DELETE FROM wlan_locks WHERE key=%s", (key,))
    conn.commit()
