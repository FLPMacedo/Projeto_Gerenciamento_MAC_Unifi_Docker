"""Aplicacao web (Flask) para gestao multi-site dos MACs da rede mobile UniFi.

Os dados sao coletados do controller e MESCLADOS num SQLite (unifi/db.py). Um
MAC so e considerado disponivel (liberavel) apos > AVAILABLE_DAYS (35) dias sem
logar -- regra a prova de ferias. Cada visita as telas grava uma coleta (no
maximo a cada 120s); para historico continuo, rode collect.py periodicamente.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from flask import (
    Flask, Response, flash, redirect, render_template, request, send_file,
    session, url_for,
)

from unifi import UnifiClient, UnifiError, db, secret
from unifi import config as unifi_config_mod
from unifi.inventory import snapshot_all, collect_unifi_audit

# Caminhos robustos para rodar tanto via "python app.py" quanto como .exe
# (PyInstaller): templates/static saem do bundle (_MEIPASS), enquanto .env e o
# banco ficam ao lado do executavel (graveis).
FROZEN = getattr(sys, "frozen", False)
APP_DIR = (os.path.dirname(sys.executable) if FROZEN
           else os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


load_dotenv(os.path.join(APP_DIR, ".env"))

app = Flask(__name__,
            template_folder=resource_path("templates"),
            static_folder=resource_path("static"))

APP_VERSION = "v4"
DEFAULT_DAYS = db.AVAILABLE_DAYS  # 35
NEVER_MODE = os.getenv("NEVER_MODE", "grace")  # grace | immediate
EDIT_LOCK_TTL = 180  # segundos: aviso de edicao simultanea
COLLECT_BASE = int(os.getenv("COLLECT_BASE", "60"))   # base da janela de coleta
SESSION_TTL = 90        # segundos sem ping -> sessao considerada encerrada

# ---------------------------------------------------------------- logs/
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log = logging.getLogger("gestaomac")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "gestaomac.log"), when="midnight",
        backupCount=60, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)

# Ponteiro local (ao lado do exe) que guarda ONDE fica o banco. Assim o usuario
# aponta a pasta de rede no proprio login, sem mexer no .env.
DBPOINTER = os.path.join(APP_DIR, "dbpath.cfg")


def _read_pointer():
    try:
        if os.path.exists(DBPOINTER):
            return (open(DBPOINTER, encoding="utf-8").read().strip() or None)
    except Exception:
        pass
    return None


DB_PATH = (os.getenv("DB_PATH") or _read_pointer()
           or os.path.join(APP_DIR, "data", "history.db"))
DATA_DIR = os.path.dirname(DB_PATH)

# A chave de cripto fica NA RAIZ (ao lado do exe) por padrao -- a MESMA chave
# deve ser distribuida para cada PC que usa o banco compartilhado. Migra a
# chave antiga (que ficava na pasta do banco) se existir e a da raiz nao.
KEY_PATH = os.getenv("KEY_PATH") or os.path.join(APP_DIR, "secret.key")
# Credenciais do UniFi: arquivo LOCAL por maquina (cada usuario sua conta).
CREDS_PATH = os.getenv("CREDS_PATH") or os.path.join(APP_DIR, "creds.enc")


def set_db_folder(folder: str) -> None:
    """Aponta o banco para uma PASTA (ex.: de rede). Persiste em dbpath.cfg.
    A secret.key NAO muda -- continua na raiz (mesma chave em todos os PCs)."""
    global DB_PATH, DATA_DIR
    folder = (folder or "").strip().rstrip("/\\")
    if not folder:
        return
    DB_PATH = os.path.join(folder, "history.db")
    DATA_DIR = folder
    os.makedirs(folder, exist_ok=True)
    with open(DBPOINTER, "w", encoding="utf-8") as fh:
        fh.write(DB_PATH)

# Multiusuario: instancias "visualizador" nao coletam (evita disputa de escrita
# no banco em rede). Deixe um coletor central (collect.py agendado) gravando.
# Defina COLLECT_ON_OPEN=0 nos PCs dos usuarios; =1 no coletor (ou uso single).
COLLECT_ON_OPEN = os.getenv("COLLECT_ON_OPEN", "1").strip().lower() \
    not in {"0", "false", "no", "off", "nao", "não"}

# Unidades disponiveis no checklist do cadastro (editavel via env UNITS).
UNITS = [u.strip() for u in os.getenv(
    "UNITS",
    "101,102,103,104,105,106,107,110,111,113,115,117").split(",") if u.strip()]

_client: UnifiClient | None = None
_client_sig = None
_lock = threading.Lock()
_sites_cache: tuple[float, list[dict]] | None = None


def db_conn():
    return db.connect(DB_PATH)


def _flask_secret() -> str:
    """Segredo de sessao persistente (gerado uma vez e guardado no banco)."""
    conn = db_conn()
    try:
        s = db.get_setting(conn, "flask_secret")
        if not s:
            s = secrets.token_hex(32)
            db.set_setting(conn, "flask_secret", s)
        return s
    finally:
        conn.close()


app.secret_key = _flask_secret()


# ----------------------------------------------------- credenciais UniFi
def get_unifi_config() -> dict | None:
    return unifi_config_mod.resolve(CREDS_PATH, KEY_PATH)


def unifi_configured() -> bool:
    c = get_unifi_config()
    return bool(c and c["host"] and c["username"] and c["password"])


def get_client() -> UnifiClient:
    global _client, _client_sig
    cfg = get_unifi_config()
    if not cfg or not cfg["username"] or not cfg["password"]:
        raise UnifiError("UniFi nao configurado. Acesse Configuracao.")
    sig = (cfg["host"], cfg["site"], cfg["username"], cfg["password"], cfg["verify"])
    if _client is None or _client_sig != sig:
        _client = UnifiClient(host=cfg["host"], username=cfg["username"],
                              password=cfg["password"], site=cfg["site"],
                              verify_ssl=cfg["verify"])
        _client.login()
        _client_sig = sig
    return _client


def invalidate_client() -> None:
    global _client, _client_sig, _sites_cache
    _client = None
    _client_sig = None
    _sites_cache = None


def get_sites() -> list[dict]:
    """Sites com cache de 5 min (a lista muda raramente)."""
    global _sites_cache
    now = time.time()
    if _sites_cache and now - _sites_cache[0] < 300:
        return _sites_cache[1]
    sites = get_client().get_sites()
    _sites_cache = (now, sites)
    return sites


def site_desc(site_id: str) -> str:
    for s in get_sites():
        if s["id"] == site_id:
            return s["desc"]
    return site_id


def _machine() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "?"


def maybe_collect(force: bool = False) -> bool:
    """Coleta distribuida com LEASE (turno unico): so UM terminal coleta por
    janela; os demais apenas re-leem. Janela = COLLECT_BASE + 30s x conectados.
    force (abrir/fechar/botao) usa janela curta de 15s (evita estouro)."""
    if not force and not COLLECT_ON_OPEN:
        return False  # instancia marcada como somente-visualizacao
    conn = db_conn()
    try:
        if force:
            interval = 15
        else:
            interval = COLLECT_BASE + 30 * db.active_count(conn, SESSION_TTL)
        if not db.claim_collection(conn, interval, _machine()):
            return False  # outro terminal ja coletou nesta janela
    finally:
        conn.close()

    with _lock:
        try:
            cli = get_client()
            rows, ts = snapshot_all(cli)
            novos = 0
            conn = db_conn()
            try:
                db.record_snapshot(conn, rows, ts)
                try:
                    novos = collect_unifi_audit(cli, conn, get_sites())
                except Exception as exc:
                    log.warning("espelho log UniFi falhou: %s", exc)
            finally:
                conn.close()
            log.info("coleta: %d linhas | %d eventos UniFi novos | por %s",
                     len(rows), novos, _machine())
        except Exception as exc:
            log.error("coleta falhou: %s", exc)
            return False
    return True


# ----------------------------------------------- presenca / heartbeat
_LAST_PING = {"t": time.time()}
_PINGED = {"v": False}


def seconds_since_ping() -> float:
    return time.time() - _LAST_PING["t"]


def was_pinged() -> bool:
    return _PINGED["v"]


def final_shutdown_collect() -> None:
    """Coleta final ao fechar (best-effort)."""
    try:
        log.info("encerrando: coleta final")
        maybe_collect(force=True)
    except Exception:
        pass


@app.context_processor
def _inject_logos():
    """Disponibiliza a logo da empresa (se o arquivo existir) e a marca/versao."""
    brand_logo = None
    for name in ("logo_brand.png", "logo_brand.jpg", "logo_brand.jpeg",
                 "logo_brand.webp"):
        if os.path.exists(resource_path(os.path.join("static", name))):
            brand_logo = name
            break
    return {"brand_logo": brand_logo, "app_version": APP_VERSION,
            "brand": os.getenv("BRAND", "")}


@app.context_processor
def _inject_alerts():
    """Banner de alerta (VIP fora da rede) + nº de usuários conectados."""
    if not session.get("user"):
        return {"vip_alert": [], "connected": 0}
    try:
        conn = db_conn()
        try:
            return {"vip_alert": db.vip_alerts(conn),
                    "connected": db.active_count(conn, SESSION_TTL)}
        finally:
            conn.close()
    except Exception:
        return {"vip_alert": [], "connected": 0}


@app.route("/api/ping", methods=["GET", "POST"])
def api_ping():
    _LAST_PING["t"] = time.time()
    _PINGED["v"] = True
    connected = 1
    sid = session.get("sid")
    if sid:
        conn = db_conn()
        try:
            db.ping_session(conn, sid, session.get("user", ""), _machine())
            connected = db.active_count(conn, SESSION_TTL)
        finally:
            conn.close()
    return {"connected": connected, "ts": int(time.time())}


@app.route("/api/close", methods=["GET", "POST"])
def api_close():
    # Apenas encerra a sessao de presenca. NAO derruba o app -- o encerramento
    # acontece por ausencia de heartbeat (o navegador parou de pingar de verdade).
    sid = session.get("sid")
    if sid:
        conn = db_conn()
        try:
            db.end_session(conn, sid)
        finally:
            conn.close()
    return {"ok": True}


# --------------------------------------------------------------------- rotas
PUBLIC_ENDPOINTS = {"login", "static", "api_ping", "api_close"}


@app.before_request
def _guard():
    ep = request.endpoint
    if ep in PUBLIC_ENDPOINTS:
        return
    if not session.get("user"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    # Permite apontar a PASTA do banco (rede) no proprio login.
    if request.method == "POST" and request.form.get("db_folder", "").strip():
        set_db_folder(request.form["db_folder"].strip())
        invalidate_client()

    # Acesso = credenciais do UniFi (validadas direto no controller).
    conn = db_conn()
    try:
        host = db.get_setting(conn, "unifi_host", "") or os.getenv("UNIFI_HOST", "")
        site = db.get_setting(conn, "unifi_site", "default")
        verify = db.get_setting(conn, "unifi_verify", "0") == "1"
        user_saved = db.get_setting(conn, "unifi_username", "")
    finally:
        conn.close()

    if request.method == "POST":
        host = (request.form.get("host", "").strip() or host)
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        try:
            test = UnifiClient(host=host, username=user, password=pw,
                               site=site, verify_ssl=verify)
            test.login()                 # valida no controller UniFi
            test.get_sites()             # garante que tem acesso de leitura
            # Cada usuario usa a PROPRIA conta UniFi: grava as credenciais
            # LOCALMENTE nesta maquina (criptografadas com a secret.key local).
            unifi_config_mod.save(CREDS_PATH, KEY_PATH, {
                "host": host, "site": site, "username": user,
                "password": pw, "verify": verify})
            invalidate_client()
            session["user"] = user
            session["sid"] = uuid.uuid4().hex
            conn = db_conn()
            try:
                db.ping_session(conn, session["sid"], user, _machine())
            finally:
                conn.close()
            log.info("login: %s de %s", user, _machine())
            return redirect(request.args.get("next") or url_for("overview"))
        except Exception:
            flash("Login recusado pelo UniFi: verifique usuário, senha e host.", "err")
            user_saved = user
    return render_template("login.html", host=host, user_saved=user_saved,
                           db_folder=DATA_DIR)


@app.route("/logout")
def logout():
    sid = session.get("sid")
    if sid:
        conn = db_conn()
        try:
            db.end_session(conn, sid)
        finally:
            conn.close()
    session.clear()
    flash("Sessão encerrada.", "ok")
    return redirect(url_for("login"))


@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        cur = get_unifi_config() or {}
        pw = request.form.get("password", "")
        cfg = {
            "host": request.form.get("host", "").strip(),
            "site": request.form.get("site", "").strip() or "default",
            "username": request.form.get("username", "").strip(),
            "verify": request.form.get("verify") == "on",
            # mantem a senha atual se o campo vier vazio
            "password": pw or cur.get("password", ""),
        }
        unifi_config_mod.save(CREDS_PATH, KEY_PATH, cfg)
        invalidate_client()
        try:
            n = len(get_client().get_sites())
            flash(f"Configuração salva e conectada: {n} site(s) encontrados.", "ok")
        except Exception as exc:
            flash(f"Salvo, mas não consegui conectar: {str(exc)[:140]}", "err")
        return redirect(url_for("config"))
    cfg = get_unifi_config() or {}
    return render_template("config.html", cfg=cfg, has_pw=bool(cfg.get("password")))


@app.route("/")
def index():
    return render_template("index.html", sites=get_sites())


@app.route("/overview")
def overview():
    days = int(request.args.get("days", DEFAULT_DAYS))
    maybe_collect(force=request.args.get("refresh") == "1")
    conn = db_conn()
    try:
        data = db.overview_summary(conn, days=days, never_mode=NEVER_MODE)
    finally:
        conn.close()
    return render_template("overview.html", data=data, days=days, sites=get_sites())


@app.route("/site/<site_id>")
def dashboard(site_id):
    days = int(request.args.get("days", DEFAULT_DAYS))
    filt = request.args.get("filter", "all")  # all | unused | online
    maybe_collect()
    conn = db_conn()
    try:
        mobiles = db.site_wlans(conn, site_id)
        if not mobiles:
            return render_template(
                "dashboard.html", site_id=site_id, site_desc=site_desc(site_id),
                sites=get_sites(), inv=None, mobiles=[], filt=filt)
        wlan_id = request.args.get("wlan") or mobiles[0]["_id"]
        chosen = next((w for w in mobiles if w["_id"] == wlan_id), mobiles[0])
        inv = db.site_inventory(conn, site_id, wlan_id, days=days,
                                never_mode=NEVER_MODE)
    finally:
        conn.close()

    rows = inv["rows"]
    if filt == "unused":
        rows = [r for r in rows if r["unused"]]
    elif filt == "online":
        rows = [r for r in rows if r["online"]]
    elif filt == "blocked":
        rows = [r for r in rows if r["blocked"]]
    elif filt == "d50":
        rows = [r for r in rows if not r["online"]
                and r["days_idle"] is not None and r["days_idle"] > 50]
    elif filt == "d100":
        rows = [r for r in rows if not r["online"]
                and r["days_idle"] is not None and r["days_idle"] > 100]

    return render_template(
        "dashboard.html", site_id=site_id, site_desc=site_desc(site_id),
        sites=get_sites(), inv=inv, rows=rows, mobiles=mobiles,
        wlan=chosen, filt=filt, stale_days=days)


@app.route("/refresh")
def refresh():
    """Botao 'Atualizar status': forca uma coleta (somente leitura) e volta."""
    maybe_collect(force=True)
    flash("Status atualizado: dados coletados do controller (somente leitura).", "ok")
    return redirect(request.args.get("next") or url_for("overview"))


@app.route("/backup.csv")
def backup_csv():
    """Backup completo (CSV) de TODOS os aparelhos ja vistos na mobile + cadastro.
    Tambem salva uma copia em backups/ ao lado do app."""
    maybe_collect()
    conn = db_conn()
    try:
        rows = db.backup_rows(conn)
    finally:
        conn.close()

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    header = ["mac", "site", "wlan", "na_lista", "bloqueado", "vip", "termo",
              "device_name", "hostname", "fabricante", "online", "ultimo_acesso",
              "primeiro_acesso", "nome", "setor", "unidade", "funcao", "lider",
              "gestor_autorizou", "chamado", "notes"]
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(k, "") for k in header])
    data = buf.getvalue().encode("utf-8-sig")

    # salva uma copia em disco (backup historico)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bkp_dir = os.path.join(APP_DIR, "backups")
    os.makedirs(bkp_dir, exist_ok=True)
    with open(os.path.join(bkp_dir, f"backup_mobile_{stamp}.csv"), "wb") as fh:
        fh.write(data)

    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="backup_mobile_{stamp}.csv"'})


def _bkp_dir() -> str:
    d = os.path.join(APP_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    return d


@app.route("/backup")
def backup_page():
    d = _bkp_dir()
    files = []
    for f in sorted(os.listdir(d), reverse=True):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            files.append({"name": f, "kb": round(os.path.getsize(p) / 1024, 1),
                          "mtime": os.path.getmtime(p)})
    return render_template("backup.html", files=files, sites=get_sites())


@app.route("/backup.db")
def backup_db():
    """Baixa o banco SQLite inteiro (cópia consistente via backup online) e
    guarda uma cópia em backups/."""
    import sqlite3
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(_bkp_dir(), f"history_{stamp}.db")
    src = db.connect(DB_PATH)
    try:
        dst = sqlite3.connect(out_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return send_file(out_path, as_attachment=True,
                     download_name=f"history_{stamp}.db")


def _mobile_sites():
    """Sites com WLAN mobile + ocupacao (para os modulos add/remover)."""
    conn = db_conn()
    try:
        return [s for s in db.overview_summary(
            conn, days=DEFAULT_DAYS, never_mode=NEVER_MODE)["sites"] if s["wlan_id"]]
    finally:
        conn.close()


def _wlan_lock(key) -> bool:
    conn = db_conn()
    try:
        return db.acquire_wlan_lock(conn, key, session.get("user", ""))
    finally:
        conn.close()


def _wlan_unlock(key) -> None:
    conn = db_conn()
    try:
        db.release_wlan_lock(conn, key)
    finally:
        conn.close()


_BUSY_MSG = "Este site está sendo editado por outra pessoa agora. Tente em alguns segundos."


@app.route("/adicionar", methods=["GET", "POST"])
def adicionar():
    if request.method == "POST":
        target = request.form.get("target", "")
        mac = request.form.get("mac", "").strip()
        nome = request.form.get("nome", "").strip()
        if ":" not in target:
            flash("Selecione o site.", "err")
            return render_template("adicionar.html", sites=_mobile_sites(), mac=mac, nome=nome)
        sid, wid = target.split(":", 1)
        client = get_client()
        key = f"{sid}:{wid}"
        if not _wlan_lock(key):
            flash(_BUSY_MSG, "warn")
            return render_template("adicionar.html", sites=_mobile_sites(), mac=mac, nome=nome)
        try:
            norm = client.normalize_mac(mac)
            with _lock:
                client.site = sid
                res = client.add_mac_to_wlan(wid, norm)
        except UnifiError as exc:
            flash(str(exc), "err")
            return render_template("adicionar.html", sites=_mobile_sites(), mac=mac, nome=nome)
        finally:
            _wlan_unlock(key)
        if res["changed"]:
            conn = db_conn()
            try:
                if nome:
                    db.upsert_client_info(conn, norm, {"nome": nome})
                db.add_event(conn, int(time.time()), sid, site_desc(sid), norm,
                             "add_manual", f"por {session.get('user','')}")
            finally:
                conn.close()
            log.info("add_manual: %s em %s por %s", norm, sid, session.get("user", ""))
            maybe_collect(force=True)
            flash(f"MAC {norm} adicionado em {site_desc(sid)} ({res['count']}/512).", "ok")
        else:
            flash(f"MAC {norm} já estava cadastrado nesse site.", "warn")
        return redirect(url_for("adicionar"))
    return render_template("adicionar.html", sites=_mobile_sites(), mac="", nome="")


@app.route("/remover", methods=["GET", "POST"])
def remover():
    if request.method == "POST":
        target = request.form.get("target", "")
        mac = request.form.get("mac", "").strip()
        confirm = request.form.get("confirm") == "1"
        if ":" not in target:
            flash("Selecione o site.", "err")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac)
        sid, wid = target.split(":", 1)
        client = get_client()
        try:
            norm = client.normalize_mac(mac)
        except UnifiError as exc:
            flash(str(exc), "err")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac)
        conn = db_conn()
        try:
            ci = db.get_client_info(conn, norm) or {}
        finally:
            conn.close()
        if ci.get("vip"):
            flash(f"MAC {norm} é VIP/Diretoria — desmarque o VIP na ficha antes de remover.", "err")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac)
        if not confirm:
            flash("Marque a confirmação para remover.", "warn")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac, target=target)
        key = f"{sid}:{wid}"
        if not _wlan_lock(key):
            flash(_BUSY_MSG, "warn")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac)
        try:
            with _lock:
                client.site = sid
                res = client.remove_mac_from_wlan(wid, norm)
        except UnifiError as exc:
            flash(str(exc), "err")
            return render_template("remover.html", sites=_mobile_sites(), mac=mac)
        finally:
            _wlan_unlock(key)
        if res["changed"]:
            conn = db_conn()
            try:
                db.add_event(conn, int(time.time()), sid, site_desc(sid), norm,
                             "remove_manual", f"por {session.get('user','')}")
            finally:
                conn.close()
            log.info("remove_manual: %s de %s por %s", norm, sid, session.get("user", ""))
            maybe_collect(force=True)
            flash(f"MAC {norm} removido de {site_desc(sid)} ({res['count']}/512).", "ok")
        else:
            flash(f"MAC {norm} não estava na lista desse site.", "warn")
        return redirect(url_for("remover"))
    return render_template("remover.html", sites=_mobile_sites(), mac="")


@app.route("/site/<site_id>/wlan/<wlan_id>/export.csv")
def export_csv(site_id, wlan_id):
    maybe_collect()
    conn = db_conn()
    try:
        inv = db.site_inventory(conn, site_id, wlan_id, days=DEFAULT_DAYS,
                                never_mode=NEVER_MODE)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["site", "wlan", "mac", "nome", "fabricante", "online",
                "bloqueado", "status", "dias_parado", "disponivel"])
    sd = site_desc(site_id)
    wn = inv["wlan"]["name"]
    for r in inv["rows"]:
        w.writerow([sd, wn, r["mac"], r["name"], r["oui"],
                    "sim" if r["online"] else "nao",
                    "sim" if r["blocked"] else "nao", r["status_label"],
                    r["days_idle"] if r["days_idle"] is not None else "",
                    "sim" if r["unused"] else "nao"])
    return Response(
        buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="mobile_{site_id}.csv"'})


def _by_unidade(rows, unidade):
    if not unidade:
        return rows
    out = []
    for r in rows:
        units = {x.strip() for x in (r.get("unidade") or "").replace(";", ",").split(",")
                 if x.strip()}
        if unidade in units:
            out.append(r)
    return out


@app.route("/clientes")
def clientes():
    q = request.args.get("q", "").strip()
    unidade = request.args.get("unidade", "").strip()
    only_vip = request.args.get("vip", "") == "1"
    maybe_collect()
    conn = db_conn()
    try:
        rows = db.list_clients(conn, status="active", search=q or None)
        removed_count = db.removed_macs_with_info(conn)
        ev_ts = db.last_event_ts(conn)
    finally:
        conn.close()
    rows = _by_unidade(rows, unidade)
    if only_vip:
        rows = [r for r in rows if r.get("vip")]
    if request.args.get("termo") == "sem":
        rows = [r for r in rows if r.get("has_info") and not r.get("termo")]
    return render_template("clientes.html", rows=rows, q=q, unidade=unidade,
                           only_vip=only_vip, sem_termo=request.args.get("termo") == "sem",
                           units=UNITS, view="active",
                           ev_ts=ev_ts, removed_count=removed_count, sites=get_sites())


@app.route("/clientes/removidos")
def clientes_removidos():
    q = request.args.get("q", "").strip()
    unidade = request.args.get("unidade", "").strip()
    only_vip = request.args.get("vip", "") == "1"
    conn = db_conn()
    try:
        rows = db.list_clients(conn, status="removed", search=q or None)
        ev_ts = db.last_event_ts(conn)
    finally:
        conn.close()
    rows = _by_unidade(rows, unidade)
    if only_vip:
        rows = [r for r in rows if r.get("vip")]
    if request.args.get("termo") == "sem":
        rows = [r for r in rows if r.get("has_info") and not r.get("termo")]
    return render_template("clientes.html", rows=rows, q=q, unidade=unidade,
                           only_vip=only_vip, sem_termo=request.args.get("termo") == "sem",
                           units=UNITS, view="removed",
                           ev_ts=ev_ts, removed_count=len(rows), sites=get_sites())


@app.route("/cliente/<mac>", methods=["GET", "POST"])
def cliente(mac):
    conn = db_conn()
    try:
        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "copy" and request.form.get("from_mac"):
                src = db.get_client_info(conn, request.form["from_mac"]) or {}
                db.upsert_client_info(conn, mac,
                                      {k: src.get(k, "") for k in db.CLIENT_FIELDS})
                flash("Dados copiados do usuário removido. Revise e salve.", "ok")
            elif action == "troca" and request.form.get("new_mac"):
                try:
                    new_norm = UnifiClient.normalize_mac(request.form["new_mac"])
                except UnifiError as exc:
                    flash(str(exc), "err")
                    return redirect(url_for("cliente", mac=mac))
                src = db.get_client_info(conn, mac) or {}
                db.upsert_client_info(conn, new_norm,
                                      {k: src.get(k, "") for k in db.CLIENT_FIELDS})
                db.set_vip(conn, new_norm, bool(src.get("vip")))
                netmsg = ""
                target = request.form.get("troca_target", "")
                if request.form.get("apply_net") == "on" and ":" in target:
                    sid, wid = target.split(":", 1)
                    key = f"{sid}:{wid}"
                    cli = get_client()
                    if not _wlan_lock(key):
                        netmsg = " (rede: site ocupado, tente a parte de rede de novo)"
                    else:
                        try:
                            with _lock:
                                cli.site = sid
                                try:
                                    cli.remove_mac_from_wlan(wid, mac)
                                except UnifiError:
                                    pass
                                try:
                                    cli.add_mac_to_wlan(wid, new_norm)
                                except UnifiError as exc:
                                    netmsg = f" (rede: {exc})"
                        finally:
                            _wlan_unlock(key)
                        maybe_collect(force=True)
                db.add_event(conn, int(time.time()), "", "", new_norm, "troca",
                             f"{mac} -> {new_norm} por {session.get('user','')}")
                log.info("troca: %s -> %s por %s", mac, new_norm, session.get("user", ""))
                flash(f"Troca registrada: {mac} → {new_norm}.{netmsg} "
                      "Confira o cadastro do novo MAC.", "ok")
                return redirect(url_for("cliente", mac=new_norm))
            else:
                fields = {k: request.form.get(k, "") for k in db.CLIENT_FIELDS}
                # unidade e multipla (checklist) -> guarda como "101, 105, 110"
                fields["unidade"] = ", ".join(request.form.getlist("unidade"))
                db.upsert_client_info(conn, mac, fields)
                db.set_vip(conn, mac, request.form.get("vip") == "on")
                db.set_termo(conn, mac, request.form.get("termo") == "on")
                flash("Cadastro do cliente salvo.", "ok")
            return redirect(url_for("cliente", mac=mac))
        dev = db.device_detail(conn, mac)
        info = db.get_client_info(conn, mac) or {}
        removed = db.list_clients(conn, status="removed")
        events = db.events_for_mac(conn, mac)
        # aviso de edicao simultanea: alguem mais editando este MAC?
        me = session.get("user", "")
        now = int(time.time())
        lock = db.get_lock(conn, mac)
        editing_by = None
        if lock and lock["who"] != me and (now - (lock["ts"] or 0) < EDIT_LOCK_TTL):
            editing_by = lock["who"]
        db.set_lock(conn, mac, me, now)   # registra/renova minha edicao
    finally:
        conn.close()
    selected_units = {x.strip() for x in
                      (info.get("unidade") or "").replace(";", ",").split(",")
                      if x.strip()}
    return render_template("cliente.html", mac=mac.lower(), dev=dev, info=info,
                           removed=removed, fields=db.CLIENT_FIELDS,
                           units=UNITS, selected_units=selected_units,
                           events=events, event_label=db.EVENT_LABEL,
                           editing_by=editing_by, mobile_sites=_mobile_sites(),
                           sites=get_sites())


@app.route("/auditoria")
def auditoria():
    event = request.args.get("event", "").strip()
    q = request.args.get("q", "").strip()
    fonte = request.args.get("fonte", "sistema")  # sistema | unifi
    conn = db_conn()
    try:
        if fonte == "unifi":
            rows = db.list_unifi_audit(conn, search=q or None, limit=500)
            total = db.unifi_audit_count(conn)
        else:
            rows = db.recent_events(conn, event=event or None, search=q or None, limit=500)
            total = db.events_count(conn)
    finally:
        conn.close()
    return render_template("auditoria.html", rows=rows, event=event, q=q, fonte=fonte,
                           total=total, labels=db.EVENT_LABEL, sites=get_sites())


@app.route("/auditoria.csv")
def auditoria_csv():
    event = request.args.get("event", "").strip()
    q = request.args.get("q", "").strip()
    conn = db_conn()
    try:
        rows = db.recent_events(conn, event=event or None, search=q or None,
                                limit=100000)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["data_hora", "evento", "site", "mac", "detalhe"])
    for e in rows:
        w.writerow([_ts(e["ts"]), db.EVENT_LABEL.get(e["event"], e["event"]),
                    e["site_desc"] or "", e["mac"], e["detail"] or ""])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="auditoria_{stamp}.csv"'})


@app.template_filter("ts")
def _ts(value):
    if not value:
        return "-"
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(value))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
