"""Persistencia em SQLite com merge historico das coletas.

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
import sqlite3
import time
from collections import defaultdict

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mac_state(
    site_id        TEXT NOT NULL,
    site_desc      TEXT,
    wlan_id        TEXT,
    wlan_name      TEXT,
    mac            TEXT NOT NULL,
    name           TEXT,
    hostname       TEXT,
    oui            TEXT,
    in_allow_list  INTEGER NOT NULL DEFAULT 1,
    blocked        INTEGER NOT NULL DEFAULT 0,
    last_seen      INTEGER NOT NULL DEFAULT 0,   -- 0 = nunca visto
    last_online    INTEGER NOT NULL DEFAULT 0,
    first_seen     INTEGER NOT NULL DEFAULT 0,
    first_collected INTEGER NOT NULL,
    last_collected  INTEGER NOT NULL,
    PRIMARY KEY (site_id, mac)
);
CREATE TABLE IF NOT EXISTS seen_history(
    collection_id INTEGER, site_id TEXT, mac TEXT, online INTEGER, last_seen INTEGER
);
-- Auditoria: cada transicao detectada entre coletas.
-- event: cadastrado | voltou | removido | bloqueado | desbloqueado
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    site_id TEXT, site_desc TEXT,
    mac TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_mac ON events(mac);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
-- Configuracoes (login do app, credenciais do UniFi criptografadas, etc.)
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Travas de edicao (aviso de uso simultaneo no cadastro do cliente)
CREATE TABLE IF NOT EXISTS edit_locks(
    mac TEXT PRIMARY KEY,
    who TEXT,
    ts  INTEGER
);
-- Espelho do log NATIVO de atividade da UniFi (preservado para sempre)
CREATE TABLE IF NOT EXISTS unifi_audit(
    uid       TEXT PRIMARY KEY,   -- id do registro na UniFi (dedup)
    ts        INTEGER,            -- timestamp do evento (epoch)
    site_id   TEXT, site_desc TEXT,
    key       TEXT, operation TEXT, actor TEXT,
    message   TEXT, raw TEXT,
    imported_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_uaudit_ts ON unifi_audit(ts);
-- Presenca: sessoes ativas do app (para "N conectados" e escalonar coleta)
CREATE TABLE IF NOT EXISTS active_sessions(
    sid   TEXT PRIMARY KEY,       -- id da sessao (cookie)
    who   TEXT, machine TEXT,
    last_ping INTEGER
);
-- Travas de escrita por WLAN (serializa add/remover/troca no mesmo site)
CREATE TABLE IF NOT EXISTS wlan_locks(
    key TEXT PRIMARY KEY,         -- site_id:wlan_id
    who TEXT, ts INTEGER
);
-- Cadastro do cliente (dados de RH/negocio por MAC). Persiste mesmo quando o
-- MAC sai da allow-list -> aparece em "Usuarios removidos" sem perder os dados.
CREATE TABLE IF NOT EXISTS client_info(
    mac        TEXT PRIMARY KEY,
    nome       TEXT, setor TEXT, unidade TEXT, funcao TEXT,
    lider      TEXT, chamado TEXT, notes TEXT,
    gestor_autorizou TEXT,
    termo      INTEGER NOT NULL DEFAULT 0,
    vip        INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_state_site ON mac_state(site_id, in_allow_list);
CREATE INDEX IF NOT EXISTS idx_state_mac ON mac_state(mac);
"""

CLIENT_FIELDS = ["nome", "setor", "unidade", "funcao", "lider",
                 "gestor_autorizou", "chamado", "notes"]


def _migrate(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mac_state)")}
    if "hostname" not in cols:
        conn.execute("ALTER TABLE mac_state ADD COLUMN hostname TEXT")
    if "first_seen" not in cols:
        conn.execute("ALTER TABLE mac_state ADD COLUMN first_seen INTEGER NOT NULL DEFAULT 0")
    if "blocked" not in cols:
        conn.execute("ALTER TABLE mac_state ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(client_info)")}
    if ccols and "vip" not in ccols:
        conn.execute("ALTER TABLE client_info ADD COLUMN vip INTEGER NOT NULL DEFAULT 0")
    if ccols and "gestor_autorizou" not in ccols:
        conn.execute("ALTER TABLE client_info ADD COLUMN gestor_autorizou TEXT")
    if ccols and "termo" not in ccols:
        conn.execute("ALTER TABLE client_info ADD COLUMN termo INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def default_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "history.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # timeout alto: em pasta de rede (SMB), espera o lock em vez de falhar com
    # "database is locked". NAO usamos WAL (nao funciona em compartilhamento).
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


_UPSERT = """
INSERT INTO mac_state
  (site_id, site_desc, wlan_id, wlan_name, mac, name, hostname, oui, blocked,
   in_allow_list, last_seen, last_online, first_seen, first_collected, last_collected)
VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
ON CONFLICT(site_id, mac) DO UPDATE SET
  site_desc   = excluded.site_desc,
  wlan_id     = excluded.wlan_id,
  wlan_name   = excluded.wlan_name,
  name        = CASE WHEN excluded.name     <> '' THEN excluded.name     ELSE mac_state.name     END,
  hostname    = CASE WHEN excluded.hostname <> '' THEN excluded.hostname ELSE mac_state.hostname END,
  oui         = CASE WHEN excluded.oui      <> '' THEN excluded.oui      ELSE mac_state.oui      END,
  blocked     = excluded.blocked,
  in_allow_list = 1,
  last_seen   = MAX(mac_state.last_seen,  excluded.last_seen),
  last_online = MAX(mac_state.last_online, excluded.last_online),
  first_seen  = CASE
                  WHEN mac_state.first_seen = 0 THEN excluded.first_seen
                  WHEN excluded.first_seen  = 0 THEN mac_state.first_seen
                  ELSE MIN(mac_state.first_seen, excluded.first_seen) END,
  last_collected = excluded.last_collected
"""


def record_snapshot(conn: sqlite3.Connection, rows: list[dict], ts: int) -> dict:
    """Grava uma coleta, mesclando com o historico. Marca como removido (
    in_allow_list=0) quem sumiu da lista de um site presente nesta coleta."""
    had_prior = _count_collections(conn) > 0
    # estado anterior por (site, mac) para detectar transicoes
    prev = {(r["site_id"], r["mac"]): (r["in_allow_list"], r["blocked"])
            for r in conn.execute(
                "SELECT site_id, mac, in_allow_list, blocked FROM mac_state")}

    cur = conn.execute("INSERT INTO collections(ts) VALUES (?)", (ts,))
    cid = cur.lastrowid
    events: list[tuple] = []

    current: dict[str, set] = defaultdict(set)
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

        conn.execute(_UPSERT, (
            r["site_id"], r["site_desc"], r["wlan_id"], r["wlan_name"], r["mac"],
            r.get("name") or "", r.get("hostname") or "", r.get("oui") or "",
            new_blk,
            eff_seen, last_online, r.get("first_seen") or 0, ts, ts,
        ))
        conn.execute(
            "INSERT INTO seen_history(collection_id, site_id, mac, online, last_seen)"
            " VALUES (?,?,?,?,?)",
            (cid, r["site_id"], r["mac"], 1 if r["online"] else 0, ls),
        )

    vips = vip_macs(conn)
    removed = 0
    for site_id, macs in current.items():
        existing = conn.execute(
            "SELECT mac, site_desc FROM mac_state "
            "WHERE site_id=? AND in_allow_list=1", (site_id,)).fetchall()
        for row in existing:
            if row["mac"] not in macs:
                conn.execute(
                    "UPDATE mac_state SET in_allow_list=0, last_collected=? "
                    "WHERE site_id=? AND mac=?", (ts, site_id, row["mac"]))
                is_vip = row["mac"] in vips
                events.append((ts, site_id, row["site_desc"], row["mac"],
                               "vip_removido" if is_vip else "removido",
                               "VIP/Diretoria removido!" if is_vip
                               else "saiu da allow-list"))
                removed += 1

    if events:
        conn.executemany(
            "INSERT INTO events(ts, site_id, site_desc, mac, event, detail) "
            "VALUES (?,?,?,?,?,?)", events)
    conn.commit()
    return {"collection_id": cid, "rows": len(rows),
            "marked_removed": removed, "events": len(events)}


# ---------------------------------------------------------------- leitura
def _latest_ts(conn) -> int:
    r = conn.execute("SELECT MAX(ts) AS t FROM collections").fetchone()
    return r["t"] or 0


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


def _row_view(r, now, days, never_mode):
    status, didle = _classify(r["last_seen"], r["first_collected"],
                              _is_online(r, now), now, days, never_mode)
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


_LATEST = {"ts": 0}


def _is_online(r, now):
    # online se a ultima coleta o viu online (last_online == ts da coleta mais recente)
    return r["last_online"] and r["last_online"] == _LATEST["ts"]


def site_inventory(conn, site_id, wlan_id, days=AVAILABLE_DAYS,
                   never_mode="grace", cap=MAC_FILTER_CAP):
    _LATEST["ts"] = _latest_ts(conn)
    now = int(time.time())
    q = ("SELECT * FROM mac_state WHERE site_id=? AND in_allow_list=1"
         + (" AND wlan_id=?" if wlan_id else ""))
    args = (site_id, wlan_id) if wlan_id else (site_id,)
    db_rows = conn.execute(q, args).fetchall()
    rows = [_row_view(r, now, days, never_mode) for r in db_rows]

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
    _LATEST["ts"] = _latest_ts(conn)
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
        v = _row_view(r, now, days, never_mode)
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
            "latest_ts": _LATEST["ts"], "collections": _count_collections(conn)}


def _count_collections(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM collections").fetchone()["c"]


def site_wlans(conn, site_id) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT wlan_id, wlan_name FROM mac_state "
        "WHERE site_id=? AND in_allow_list=1", (site_id,)).fetchall()
    return [{"_id": r["wlan_id"], "name": r["wlan_name"]}
            for r in rows if r["wlan_id"]]


def has_data(conn) -> bool:
    return _count_collections(conn) > 0


# ------------------------------------------------------------- settings
def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)))
    conn.commit()


# ----------------------------------------------- travas de edicao (locks)
def get_lock(conn, mac) -> dict | None:
    r = conn.execute("SELECT who, ts FROM edit_locks WHERE mac=?",
                     (mac.lower(),)).fetchone()
    return dict(r) if r else None


def set_lock(conn, mac, who, ts) -> None:
    conn.execute(
        "INSERT INTO edit_locks(mac, who, ts) VALUES(?,?,?) "
        "ON CONFLICT(mac) DO UPDATE SET who=excluded.who, ts=excluded.ts",
        (mac.lower(), who, ts))
    conn.commit()


# ----------------------------------------------------- cadastro de cliente
def get_client_info(conn, mac) -> dict | None:
    r = conn.execute("SELECT * FROM client_info WHERE mac=?", (mac.lower(),)).fetchone()
    return dict(r) if r else None


def upsert_client_info(conn, mac, fields: dict) -> None:
    mac = mac.lower()
    now = int(time.time())
    cols = ",".join(CLIENT_FIELDS)
    ph = ",".join(["?"] * len(CLIENT_FIELDS))
    sets = ",".join(f"{k}=excluded.{k}" for k in CLIENT_FIELDS)
    vals = [(fields.get(k) or "").strip() for k in CLIENT_FIELDS]
    conn.execute(
        f"INSERT INTO client_info(mac,{cols},created_at,updated_at) "
        f"VALUES(?,{ph},?,?) "
        f"ON CONFLICT(mac) DO UPDATE SET {sets}, updated_at=excluded.updated_at",
        [mac] + vals + [now, now])
    conn.commit()


def set_vip(conn, mac, vip: bool) -> None:
    """Marca/desmarca um MAC como prioritario (VIP/Diretoria)."""
    conn.execute("UPDATE client_info SET vip=?, updated_at=? WHERE mac=?",
                 (1 if vip else 0, int(time.time()), mac.lower()))
    conn.commit()


def set_termo(conn, mac, termo: bool) -> None:
    """Marca/desmarca se o termo do cliente foi assinado/entregue."""
    conn.execute("UPDATE client_info SET termo=?, updated_at=? WHERE mac=?",
                 (1 if termo else 0, int(time.time()), mac.lower()))
    conn.commit()


def vip_alerts(conn) -> list[dict]:
    """MACs marcados como VIP que NAO estao mais na allow-list (alerta!)."""
    active = {r["mac"] for r in conn.execute(
        "SELECT DISTINCT mac FROM mac_state WHERE in_allow_list=1")}
    out = []
    for r in conn.execute("SELECT mac, nome, setor FROM client_info WHERE vip=1"):
        if r["mac"] not in active:
            out.append({"mac": r["mac"], "nome": r["nome"] or "",
                        "setor": r["setor"] or ""})
    return out


def vip_macs(conn) -> set:
    return {r["mac"] for r in conn.execute(
        "SELECT mac FROM client_info WHERE vip=1")}


def device_detail(conn, mac) -> dict | None:
    """Tudo que sabemos do aparelho (infos da UniFi mescladas) + presenca por site."""
    mac = mac.lower()
    rows = conn.execute("SELECT * FROM mac_state WHERE mac=?", (mac,)).fetchall()
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


_AGG = """
SELECT mac,
  MAX(in_allow_list)            AS active,
  MAX(blocked)                  AS blocked,
  MAX(NULLIF(name,''))          AS name,
  MAX(NULLIF(hostname,''))      AS hostname,
  MAX(NULLIF(oui,''))           AS oui,
  MAX(COALESCE(last_seen,0))    AS last_seen,
  MAX(COALESCE(last_online,0))  AS last_online,
  GROUP_CONCAT(DISTINCT CASE WHEN in_allow_list=1 THEN site_desc END) AS sites
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


EVENT_LABEL = {
    "cadastrado": "Cadastrado", "voltou": "Voltou", "removido": "Removido",
    "bloqueado": "Bloqueado", "desbloqueado": "Desbloqueado",
    "vip_removido": "VIP REMOVIDO",
    "add_manual": "Adicionado (manual)", "remove_manual": "Removido (manual)",
    "troca": "Troca de aparelho",
}


def add_event(conn, ts, site_id, site_desc, mac, event, detail="") -> None:
    conn.execute(
        "INSERT INTO events(ts, site_id, site_desc, mac, event, detail) "
        "VALUES (?,?,?,?,?,?)",
        (ts, site_id, site_desc, mac.lower(), event, detail))
    conn.commit()


def events_for_mac(conn, mac, limit=200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE mac=? ORDER BY ts DESC, id DESC LIMIT ?",
        (mac.lower(), limit)).fetchall()
    return [dict(r) for r in rows]


def recent_events(conn, event=None, search=None, limit=500) -> list[dict]:
    q = "SELECT * FROM events"
    args, where = [], []
    if event:
        where.append("event=?"); args.append(event)
    if search:
        s = f"%{search.strip()}%"
        where.append("(mac LIKE ? OR site_desc LIKE ?)"); args += [s, s]
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"; args.append(limit)
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
    info = {r["mac"] for r in conn.execute("SELECT mac FROM client_info")}
    if not info:
        return 0
    active = {r["mac"] for r in conn.execute(
        "SELECT DISTINCT mac FROM mac_state WHERE in_allow_list=1")}
    return len(info - active)


# ============================ v4: log UniFi / presenca / leases ============
def upsert_unifi_audit(conn, rows) -> int:
    """Insere registros do log nativo da UniFi (dedup por uid). Retorna novos."""
    now = int(time.time())
    novos = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO unifi_audit"
            "(uid, ts, site_id, site_desc, key, operation, actor, message, raw, imported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["uid"], r.get("ts"), r.get("site_id"), r.get("site_desc"),
             r.get("key"), r.get("operation"), r.get("actor"),
             r.get("message"), r.get("raw"), now))
        novos += cur.rowcount
    conn.commit()
    return novos


def list_unifi_audit(conn, search=None, limit=500) -> list[dict]:
    q = "SELECT * FROM unifi_audit"
    args = []
    if search:
        s = f"%{search.strip()}%"
        q += " WHERE (actor LIKE ? OR message LIKE ? OR site_desc LIKE ? OR key LIKE ?)"
        args = [s, s, s, s]
    q += " ORDER BY ts DESC, uid DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def unifi_audit_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM unifi_audit").fetchone()["c"]


# ----------------------------------------------------------- presenca
def ping_session(conn, sid, who, machine) -> None:
    conn.execute(
        "INSERT INTO active_sessions(sid, who, machine, last_ping) VALUES (?,?,?,?) "
        "ON CONFLICT(sid) DO UPDATE SET who=excluded.who, machine=excluded.machine, "
        "last_ping=excluded.last_ping",
        (sid, who or "", machine or "", int(time.time())))
    conn.commit()


def end_session(conn, sid) -> None:
    conn.execute("DELETE FROM active_sessions WHERE sid=?", (sid,))
    conn.commit()


def active_sessions(conn, ttl=90) -> list[dict]:
    cut = int(time.time()) - ttl
    conn.execute("DELETE FROM active_sessions WHERE last_ping < ?", (cut,))
    conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT who, machine, last_ping FROM active_sessions ORDER BY who")]


def active_count(conn, ttl=90) -> int:
    return len(active_sessions(conn, ttl))


# --------------------------------------------- lease de coleta (turno unico)
def claim_collection(conn, interval, who="") -> bool:
    """Assume a coleta da janela atual de forma atomica. True = deve coletar."""
    now = int(time.time())
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('last_collect_ts','0')")
    cur = conn.execute(
        "UPDATE settings SET value=? WHERE key='last_collect_ts' "
        "AND CAST(value AS INTEGER) <= ?", (str(now), now - interval))
    conn.commit()
    if cur.rowcount == 1:
        set_setting(conn, "last_collect_by", who)
        return True
    return False


# ------------------------------------------- trava de escrita por WLAN
def acquire_wlan_lock(conn, key, who, ttl=20) -> bool:
    now = int(time.time())
    conn.execute("DELETE FROM wlan_locks WHERE ts < ?", (now - ttl,))
    cur = conn.execute("INSERT OR IGNORE INTO wlan_locks(key, who, ts) VALUES (?,?,?)",
                       (key, who or "", now))
    conn.commit()
    return cur.rowcount == 1


def release_wlan_lock(conn, key) -> None:
    conn.execute("DELETE FROM wlan_locks WHERE key=?", (key,))
    conn.commit()
