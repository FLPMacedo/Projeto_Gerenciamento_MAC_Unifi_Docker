"""Classificacao de uso/desuso dos MACs cadastrados na allow-list de uma WLAN.

Status possiveis (do mais ativo ao mais inutil):
  online     -> conectado agora
  recent     -> visto nos ultimos 7 dias
  idle       -> visto entre 8 e 30 dias
  stale      -> visto entre 31 e 90 dias        }
  abandoned  -> visto ha mais de 90 dias         } => "sem uso" (candidato a liberar)
  never      -> esta na lista mas nunca foi visto }
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from . import db as _db
from .client import MAC_FILTER_CAP, UnifiClient

# Status considerados "sem uso" por padrao (limiar 30 dias).
UNUSED_STATUSES = {"stale", "abandoned", "never"}

STATUS_LABEL = {
    "online": "Online agora",
    "recent": "Ativo (<=7d)",
    "idle": "Ocioso (8-30d)",
    "stale": "Parado (31-90d)",
    "abandoned": "Abandonado (>90d)",
    "never": "Nunca visto",
}


def _classify(days: Optional[float], online: bool, stale_days: int) -> str:
    if online:
        return "online"
    if days is None:
        return "never"
    if days <= 7:
        return "recent"
    if days <= stale_days:
        return "idle"
    if days <= 90:
        return "stale"
    return "abandoned"


def build_inventory(
    client: UnifiClient, wlan: dict, stale_days: int = 30
) -> dict:
    """Monta o inventario de uma WLAN mobile com status de uso de cada MAC.

    Retorna {'rows': [...], 'summary': {...}, 'wlan': {...}}.
    """
    allow = [m.lower() for m in (wlan.get("mac_filter_list") or [])]

    # historico: mac -> registro com last_seen/nome
    hist: dict[str, dict] = {}
    for c in client.get_all_users():
        m = (c.get("mac") or "").lower()
        if m:
            hist[m] = c

    online = {(c.get("mac") or "").lower() for c in client.get_clients()}
    now = int(time.time())
    DAY = 86400

    rows = []
    counts = {k: 0 for k in STATUS_LABEL}
    for mac in allow:
        c = hist.get(mac, {})
        is_online = mac in online
        last_seen = c.get("last_seen")
        days = None if not last_seen else (now - int(last_seen)) / DAY
        status = _classify(days, is_online, stale_days)
        counts[status] += 1
        rows.append(
            {
                "mac": mac,
                "name": c.get("name") or c.get("hostname") or "",
                "hostname": c.get("hostname") or "",
                "oui": c.get("oui") or "",
                "online": is_online,
                "is_wired": bool(c.get("is_wired")),
                "last_seen": int(last_seen) if last_seen else None,
                "days_idle": None if days is None else round(days, 1),
                "status": status,
                "status_label": STATUS_LABEL[status],
                "unused": status in UNUSED_STATUSES,
            }
        )

    # ordena: sem uso primeiro (mais tempo parado no topo), depois ativos
    def sort_key(r):
        d = r["days_idle"] if r["days_idle"] is not None else 10**9
        return (0 if r["unused"] else 1, -d if r["unused"] else d)

    rows.sort(key=sort_key)

    used = len(allow)  # noqa: F841 (mantido por clareza)
    unused = sum(counts[s] for s in UNUSED_STATUSES)
    summary = {
        "total": used,
        "cap": MAC_FILTER_CAP,
        "free_slots": MAC_FILTER_CAP - used,
        "is_full": used >= MAC_FILTER_CAP,
        "unused": unused,
        "in_use": used - unused,
        "counts": counts,
        "stale_days": stale_days,
        "reclaimable": unused,  # vagas que dariam pra liberar removendo os sem uso
    }
    return {"rows": rows, "summary": summary, "wlan": wlan}


# ------------------------------------------------- coletores detalhados (DB)
def _hist_index(client: UnifiClient) -> dict[str, dict]:
    idx = {}
    for c in client.get_all_users():
        m = (c.get("mac") or "").lower()
        if m:
            idx[m] = c
    return idx


def snapshot_site(client, wlan, site_id, site_desc, hist=None, online=None, ts=None):
    """Linhas detalhadas (com last_seen epoch) de uma WLAN, para gravar no banco."""
    ts = ts or int(time.time())
    if hist is None:
        hist = _hist_index(client)
    if online is None:
        online = {(c.get("mac") or "").lower() for c in client.get_clients()}
    rows = []
    for m in (wlan.get("mac_filter_list") or []):
        m = m.lower()
        c = hist.get(m, {})
        ls = c.get("last_seen")
        fs = c.get("first_seen")
        rows.append({
            "site_id": site_id, "site_desc": site_desc,
            "wlan_id": wlan["_id"], "wlan_name": wlan.get("name"),
            "mac": m, "name": c.get("name") or c.get("hostname") or "",
            "hostname": c.get("hostname") or "",
            "oui": c.get("oui") or "", "online": m in online,
            "last_seen": int(ls) if ls else None,
            "first_seen": int(fs) if fs else None,
            "blocked": bool(c.get("blocked")),
        })
    return rows, ts


def snapshot_all(client: UnifiClient):
    """Linhas detalhadas de todas as WLANs mobile de todos os sites."""
    ts = int(time.time())
    rows = []
    for s in client.get_sites():
        client.site = s["id"]
        try:
            mobiles = client.get_mobile_wlans()
        except Exception:
            continue
        if not mobiles:
            continue
        hist = _hist_index(client)
        online = {(c.get("mac") or "").lower() for c in client.get_clients()}
        for w in mobiles:
            r, _ = snapshot_site(client, w, s["id"], s["desc"], hist, online, ts)
            rows.extend(r)
    return rows, ts



# ------------------------------------ espelho do log nativo da UniFi (v4)
def render_admin_message(it: dict) -> str:
    """Mensagem legivel substituindo {ADMIN}/{OBJECT}/{SECTION}/{IP} pelo meta."""
    msg = it.get("message") or ""
    meta = it.get("meta") or {}
    repl = {
        "{ADMIN}": meta.get("actor") or "?",
        "{IP}": meta.get("ip") or meta.get("source_ip") or "",
        "{SECTION}": meta.get("section") or "",
        "{OBJECT}": meta.get("display_property_value") or meta.get("collection") or "",
        "{OBJECTS}": meta.get("display_property_value") or meta.get("collection") or "",
    }
    for k, v in repl.items():
        msg = msg.replace(k, str(v))
    return re.sub(r"\{[A-Z_]+\}", "", msg).strip()


def collect_unifi_audit(client: UnifiClient, conn, sites, page_size: int = 200) -> int:
    """Le o log nativo de atividade de cada site e espelha no banco (dedup)."""
    novos = 0
    for s in sites:
        try:
            items, _ = client.get_admin_activity(s["id"], 0, page_size)
        except Exception:
            continue
        rows = []
        for it in items:
            uid = it.get("id")
            if not uid:
                continue
            ts = it.get("timestamp") or 0
            if ts and ts > 10_000_000_000:   # ms -> s
                ts = ts // 1000
            rows.append({
                "uid": uid, "ts": ts, "site_id": s["id"], "site_desc": s["desc"],
                "key": it.get("key"), "operation": it.get("operation"),
                "actor": (it.get("meta") or {}).get("actor") or "",
                "message": render_admin_message(it),
                "raw": json.dumps(it, ensure_ascii=False)[:4000],
            })
        novos += _db.upsert_unifi_audit(conn, rows)
    return novos
