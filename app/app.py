"""Aplicacao web (Flask) para gestao multi-site dos MACs da rede mobile UniFi.

Versao servidor/Docker. Os dados sao coletados do controller e MESCLADOS num
PostgreSQL (unifi/db.py). Um MAC so e considerado disponivel (liberavel) apos
> AVAILABLE_DAYS (35) dias sem logar -- regra a prova de ferias.

Diferencas em relacao a versao desktop:
  - Servido por gunicorn (`app:app`), nao por app.run/pywebview. O entrypoint
    NAO e mais iniciar.py: o watchdog de heartbeat de la encerra o processo
    apos 90s sem navegador, o que num container viraria restart loop.
  - A coleta e responsabilidade do servico `collector`; o web so le. Ver
    COLLECT_ON_OPEN.
  - Comunicacao com a UniFi via CONTA DE SERVICO (unifi/config.py).
  - Logs em stdout (padrao Docker/Portainer), nao em arquivo rotativo.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, flash, redirect, render_template, request,
    send_file, send_from_directory, session, url_for,
)

from unifi import UnifiClient, UnifiError, db, secret
from unifi import config as unifi_config_mod
from unifi.inventory import snapshot_all, collect_unifi_audit

APP_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(APP_DIR, ".env"))

app = Flask(__name__,
            template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))

APP_VERSION = os.getenv("APP_VERSION", "v5-docker")

# Quais areas este container publica:
#   completo -> painel administrativo + portal (padrao, deploy unico)
#   admin    -> so o painel; /portal nao existe
#   portal   -> so o portal de retirada; nenhuma rota administrativa e
#               registrada, entao nao ha como alcanca-las por essa porta
APP_MODE = os.getenv("APP_MODE", "completo").strip().lower()
if APP_MODE not in {"completo", "admin", "portal"}:
    raise SystemExit(f"APP_MODE invalido: {APP_MODE!r} "
                     "(use completo, admin ou portal)")
MODO_ADMIN = APP_MODE in {"completo", "admin"}
MODO_PORTAL = APP_MODE in {"completo", "portal"}

# Teto de vouchers por lote. A UniFi nao documenta um limite rigido e ele varia
# por versao; 200 e um valor conservador que cobre o uso real e evita que um
# erro de digitacao (um zero a mais) gere milhares de codigos de uma vez.
VOUCHER_MAX_QTD = int(os.getenv("VOUCHER_MAX_QTD", "200"))
# Unidades da tela da UniFi -> minutos (campo `expire` da API)
VOUCHER_UNIDADES = {"minutos": 1, "horas": 60, "dias": 1440}
DEFAULT_DAYS = db.AVAILABLE_DAYS  # 35
NEVER_MODE = os.getenv("NEVER_MODE", "grace")  # grace | immediate
EDIT_LOCK_TTL = 180  # segundos: aviso de edicao simultanea
COLLECT_BASE = int(os.getenv("COLLECT_BASE", "60"))   # base da janela de coleta
SESSION_TTL = 90        # segundos sem ping -> sessao considerada encerrada

# Pasta gravavel (volume). Guarda os backups CSV/dump gerados pela tela Backup.
DATA_DIR = os.getenv("DATA_DIR", "/data")
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(DATA_DIR, "backups"))

# ---------------------------------------------------------------- logging
# Em container os logs vao para stdout: o Docker/Portainer coleta, rotaciona e
# exibe. Escrever em arquivo dentro do container so perderia o log no restart.
logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gestaomac")

# Coleta na requisicao: DESLIGADA por padrao no servidor. Quem coleta e o
# servico `collector`. Ligar isto faz cada visita de tela disputar o lease e
# chamar o controller, deixando a navegacao lenta sem necessidade.
COLLECT_ON_OPEN = os.getenv("COLLECT_ON_OPEN", "0").strip().lower() \
    in {"1", "true", "yes", "on", "sim"}

# Unidades disponiveis no checklist do cadastro (editavel via env UNITS).
UNITS = [u.strip() for u in os.getenv(
    "UNITS",
    "101,102,103,104,105,106,107,110,111,113,115,117").split(",") if u.strip()]

# Um UnifiClient POR USUARIO: cada pessoa fala com o controller usando a conta
# dela, de modo que o log nativo da UniFi registre o autor real de cada acao.
# Chaveado por username -> (assinatura das credenciais, client).
_clients: dict[str, tuple[tuple, UnifiClient]] = {}
# Serializa o uso dos clients DENTRO deste processo: o codigo muda
# `client.site` antes de cada chamada, o que e estado mutavel compartilhado.
# A exclusao entre PROCESSOS/CONTAINERES e feita por db.acquire_wlan_lock.
_lock = threading.Lock()
# Cache de sites por usuario (permissoes podem diferir entre contas).
_sites_cache: dict[str, tuple[float, list[dict]]] = {}


def _flask_secret() -> str:
    """Segredo de sessao persistente: fica no banco, entao todas as replicas
    compartilham e as sessoes sobrevivem a restart/redeploy."""
    override = os.getenv("FLASK_SECRET_KEY")
    if override:
        return override
    with db.connection() as conn:
        return db.get_or_create_setting(
            conn, "flask_secret", lambda: secrets.token_hex(32))


def bootstrap() -> None:
    """Espera o banco, aplica o schema e resolve o segredo de sessao."""
    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "60")))
    if os.getenv("APPLY_SCHEMA", "1").lower() in {"1", "true", "yes", "on"}:
        db.apply_schema()
    app.secret_key = _flask_secret()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with db.connection() as conn:
        log.info("UniFi: %s", unifi_config_mod.describe(conn))
    log.info("coleta na requisicao (COLLECT_ON_OPEN): %s",
             "ligada" if COLLECT_ON_OPEN else "desligada")


# ----------------------------------------------------- credenciais UniFi
def get_client() -> UnifiClient:
    """Cliente UniFi da conta de QUEM ESTA LOGADO.

    Cada usuario fala com o controller com a propria conta, entao as alteracoes
    aparecem no log nativo da UniFi com o nome real do autor.
    """
    user = session.get("user")
    if not user:
        raise UnifiError("Sessão expirada. Entre novamente.")
    with db.connection() as conn:
        cfg = unifi_config_mod.user_credentials(conn, user)
    if not cfg or not cfg.get("password"):
        raise UnifiError(
            "Suas credenciais do UniFi não estão disponíveis. "
            "Saia e entre novamente para revalidá-las.")

    sig = (cfg["host"], cfg["site"], user, cfg["password"], cfg["verify"])
    cached = _clients.get(user)
    if cached and cached[0] == sig:
        return cached[1]

    client = UnifiClient(host=cfg["host"], username=user,
                         password=cfg["password"], site=cfg["site"],
                         verify_ssl=cfg["verify"])
    client.login()
    _clients[user] = (sig, client)
    return client


def invalidate_client(user: str | None = None) -> None:
    """Descarta o client em cache (do usuario informado, ou de todos)."""
    if user:
        _clients.pop(user, None)
        _sites_cache.pop(user, None)
    else:
        _clients.clear()
        _sites_cache.clear()


def get_sites() -> list[dict]:
    """Sites com cache de 5 min por usuario (a lista muda raramente)."""
    user = session.get("user") or "?"
    now = time.time()
    hit = _sites_cache.get(user)
    if hit and now - hit[0] < 300:
        return hit[1]
    sites = get_client().get_sites()
    _sites_cache[user] = (now, sites)
    return sites


def site_desc(site_id: str) -> str:
    for s in get_sites():
        if s["id"] == site_id:
            return s["desc"]
    return site_id


def _machine() -> str:
    """Identifica a instancia. Em container e o id curto do container."""
    try:
        return os.getenv("INSTANCE_NAME") or socket.gethostname()
    except Exception:
        return "?"


def maybe_collect(force: bool = False) -> bool:
    """Coleta com LEASE (turno unico): so UMA instancia coleta por janela.
    Janela = COLLECT_BASE + 30s x conectados. force usa janela curta de 15s."""
    if not force and not COLLECT_ON_OPEN:
        return False
    with db.connection() as conn:
        if force:
            interval = 15
        else:
            interval = COLLECT_BASE + 30 * db.active_count(conn, SESSION_TTL)
        if not db.claim_collection(conn, interval, _machine()):
            return False  # outra instancia ja coletou nesta janela

    with _lock:
        try:
            cli = get_client()
            rows, ts = snapshot_all(cli)
            novos = 0
            with db.connection() as conn:
                db.record_snapshot(conn, rows, ts)
                try:
                    novos = collect_unifi_audit(cli, conn, get_sites())
                except Exception as exc:
                    log.warning("espelho log UniFi falhou: %s", exc)
            log.info("coleta: %d linhas | %d eventos UniFi novos | por %s",
                     len(rows), novos, _machine())
        except Exception as exc:
            log.error("coleta falhou: %s", exc)
            # a sessao no controller pode ter expirado: forca novo login
            invalidate_client(session.get("user"))
            return False
    return True


# ----------------------------------------------- presenca / heartbeat
_LOGO_NOMES = ("logo_brand.png", "logo_brand.jpg", "logo_brand.jpeg",
               "logo_brand.webp")
# Alternativa ao static/: a logo da empresa NAO e versionada (identidade da
# empresa fica fora do repositorio publico), entao um build feito pelo Portainer
# a partir do Git nao teria o arquivo. Basta deixa-la nesta pasta do volume de
# dados que o app a encontra igual.
BRAND_DIR = os.getenv("BRAND_DIR", os.path.join(DATA_DIR, "branding"))


def _logo_empresa() -> str | None:
    """URL da logo da empresa, do static/ ou do volume. None se nao houver."""
    for nome in _LOGO_NOMES:
        if os.path.exists(os.path.join(app.static_folder, nome)):
            return url_for("static", filename=nome)
    for nome in _LOGO_NOMES:
        if os.path.exists(os.path.join(BRAND_DIR, nome)):
            return url_for("branding", nome=nome)
    return None


@app.route("/branding/<nome>")
def branding(nome):
    if nome not in _LOGO_NOMES:
        abort(404)
    return send_from_directory(BRAND_DIR, nome, max_age=3600)


@app.context_processor
def _inject_logos():
    """Disponibiliza a logo da empresa (se houver) e a marca/versao."""
    return {"brand_logo_url": _logo_empresa(), "app_version": APP_VERSION,
            "brand": os.getenv("BRAND", "")}


@app.context_processor
def _inject_alerts():
    """Banners de alerta + nº de usuários conectados."""
    vazio = {"vip_alert": [], "voucher_alert": [], "connected": 0}
    if not session.get("user"):
        return vazio
    try:
        with db.connection() as conn:
            return {"vip_alert": db.vip_alerts(conn),
                    "voucher_alert": db.voucher_alerts(conn),
                    "connected": db.active_count(conn, SESSION_TTL)}
    except Exception:
        return vazio


@app.route("/healthz")
def healthz():
    """Healthcheck do container: valida que o banco responde."""
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "version": APP_VERSION}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}, 503


@app.route("/api/ping", methods=["GET", "POST"])
def api_ping():
    connected = 1
    sid = session.get("sid")
    if sid:
        with db.connection() as conn:
            db.ping_session(conn, sid, session.get("user", ""), _machine())
            connected = db.active_count(conn, SESSION_TTL)
    return {"connected": connected, "ts": int(time.time())}


@app.route("/api/close", methods=["GET", "POST"])
def api_close():
    # Encerra apenas a sessao de presenca. Ao contrario do desktop, nao existe
    # watchdog: o processo do servidor nunca se encerra por falta de heartbeat.
    sid = session.get("sid")
    if sid:
        with db.connection() as conn:
            db.end_session(conn, sid)
    return {"ok": True}


# --------------------------------------------------------------------- rotas
# Publicas em QUALQUER modo: servem arquivos estaticos e o healthcheck do
# container, que precisa responder antes de existir sessao.
SEMPRE_PUBLICOS = {"static", "healthz", "branding"}
# Publicas so no painel de gestao (dispensam sessao, mas nao o modo admin).
PUBLIC_ENDPOINTS = {"login", "api_ping", "api_close"}


@app.before_request
def _guard():
    # O portal tem autenticacao propria (blueprint portal.py) e nao deve cair
    # na exigencia de sessao administrativa.
    if request.blueprint == "portal":
        return
    ep = request.endpoint
    # Rota inexistente: deixa o Flask devolver 404 em vez de redirecionar para
    # o login, o que fazia toda URL errada parecer uma tela protegida.
    if ep is None:
        return
    if ep in SEMPRE_PUBLICOS:
        return
    # Container em APP_MODE=portal: NENHUMA rota administrativa responde --
    # nem mesmo a tela de login, que antes vazava por estar na lista de
    # publicas e ser avaliada antes desta checagem. Devolve 404, e nao 403,
    # para nao confirmar que a rota existe naquela porta.
    if not MODO_ADMIN:
        abort(404)
    if ep in PUBLIC_ENDPOINTS:
        return
    if not session.get("user"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Entrada com a conta PESSOAL do UniFi, validada ao vivo no controller.

    A credencial e guardada cifrada (`user_creds`) para dois fins: as telas e as
    acoes de escrita passarem a usar a conta desta pessoa -- assim o log nativo
    da UniFi registra o autor real -- e o coletor, que roda sem ninguem logado,
    ter uma credencial valida para trabalhar.
    """
    with db.connection() as conn:
        cfg = unifi_config_mod.get_host(conn)
    host = cfg["host"]
    user_saved = ""

    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        # O host so pode ser informado aqui enquanto ninguem o configurou ainda
        # (primeiro arranque). Depois disso muda-se pela tela de Configuração.
        if not host:
            host = request.form.get("host", "").strip().rstrip("/")
        if not host:
            flash("Informe o endereço do controller para o primeiro acesso.", "err")
            return render_template("login.html", host="", user_saved=user,
                                   precisa_host=True)
        try:
            test = UnifiClient(host=host, username=user, password=pw,
                               site=cfg["site"], verify_ssl=cfg["verify"])
            test.login()                 # valida no controller UniFi
            test.get_sites()             # garante que tem acesso de leitura
            try:
                test.logout()
            except Exception:
                pass

            session["user"] = user
            session["sid"] = uuid.uuid4().hex
            with db.connection() as conn:
                if not unifi_config_mod.is_configured(conn):
                    # primeiro login: fixa o controller para todo mundo
                    unifi_config_mod.set_host(conn, host, cfg["site"], cfg["verify"])
                db.save_user_creds(conn, user, host, cfg["site"], cfg["verify"], pw)
                db.ping_session(conn, session["sid"], user, _machine())
            invalidate_client(user)      # descarta client antigo desta conta
            log.info("login: %s em %s", user, _machine())
            return redirect(request.args.get("next") or url_for("overview"))
        except Exception:
            flash("Login recusado pelo UniFi: verifique usuário e senha.", "err")
            user_saved = user
    return render_template("login.html", host=host, user_saved=user_saved,
                           precisa_host=not host)


@app.route("/logout")
def logout():
    sid = session.get("sid")
    if sid:
        with db.connection() as conn:
            db.end_session(conn, sid)
    session.clear()
    flash("Sessão encerrada.", "ok")
    return redirect(url_for("login"))


@app.route("/config", methods=["GET", "POST"])
def config():
    """Endereco do controller (editavel) + credenciais guardadas.

    A SENHA nao se define aqui: ela vem do login de cada pessoa. Esta tela trata
    do que e comum a todos (host/site/TLS) e mostra quais contas estao gravadas.
    """
    with db.connection() as conn:
        if request.method == "POST":
            acao = request.form.get("acao", "salvar")
            if acao == "remover" and request.form.get("username"):
                alvo = request.form["username"]
                db.delete_user_creds(conn, alvo)
                invalidate_client(alvo)
                log.info("credencial removida: %s por %s", alvo,
                         session.get("user", ""))
                flash(f"Credencial de {alvo} removida. Ela será regravada no "
                      "próximo login dessa pessoa.", "ok")
            else:
                unifi_config_mod.set_host(
                    conn,
                    request.form.get("host", ""),
                    request.form.get("site", "default"),
                    request.form.get("verify") == "on")
                invalidate_client()   # o endereco mudou para todo mundo
                log.info("controller reconfigurado por %s", session.get("user", ""))
                flash("Endereço do controller salvo.", "ok")
            return redirect(url_for("config"))

        cfg = unifi_config_mod.get_host(conn)
        creds = db.list_user_creds(conn)
        resumo = unifi_config_mod.describe(conn)

    status, detail = "ok", ""
    try:
        detail = f"{len(get_sites())} site(s) acessíveis com a sua conta."
    except Exception as exc:
        status, detail = "err", str(exc)[:200]

    return render_template("config.html", cfg=cfg, creds=creds,
                           status=status, detail=detail, resumo=resumo,
                           tem_servico=bool(unifi_config_mod.service_account()),
                           me=session.get("user", ""))


@app.route("/")
def index():
    return render_template("index.html", sites=get_sites())


@app.route("/overview")
def overview():
    days = int(request.args.get("days", DEFAULT_DAYS))
    maybe_collect(force=request.args.get("refresh") == "1")
    with db.connection() as conn:
        data = db.overview_summary(conn, days=days, never_mode=NEVER_MODE)
    return render_template("overview.html", data=data, days=days, sites=get_sites())


@app.route("/site/<site_id>")
def dashboard(site_id):
    days = int(request.args.get("days", DEFAULT_DAYS))
    filt = request.args.get("filter", "all")  # all | unused | online | blocked | d50 | d100
    maybe_collect()
    with db.connection() as conn:
        mobiles = db.site_wlans(conn, site_id)
        if not mobiles:
            return render_template(
                "dashboard.html", site_id=site_id, site_desc=site_desc(site_id),
                sites=get_sites(), inv=None, mobiles=[], filt=filt)
        wlan_id = request.args.get("wlan") or mobiles[0]["_id"]
        chosen = next((w for w in mobiles if w["_id"] == wlan_id), mobiles[0])
        inv = db.site_inventory(conn, site_id, wlan_id, days=days,
                                never_mode=NEVER_MODE)

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
    if maybe_collect(force=True):
        flash("Status atualizado: dados coletados do controller.", "ok")
    else:
        flash("Uma coleta recente ja estava em andamento; dados na tela são "
              "os mais atuais.", "warn")
    return redirect(request.args.get("next") or url_for("overview"))


@app.route("/backup.csv")
def backup_csv():
    """Backup completo (CSV) de TODOS os aparelhos ja vistos na mobile + cadastro.
    Tambem salva uma copia no volume de dados."""
    maybe_collect()
    with db.connection() as conn:
        rows = db.backup_rows(conn)

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

    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(os.path.join(BACKUP_DIR, f"backup_mobile_{stamp}.csv"), "wb") as fh:
        fh.write(data)

    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="backup_mobile_{stamp}.csv"'})


@app.route("/backup")
def backup_page():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        p = os.path.join(BACKUP_DIR, f)
        if os.path.isfile(p):
            files.append({"name": f, "kb": round(os.path.getsize(p) / 1024, 1),
                          "mtime": os.path.getmtime(p)})
    return render_template("backup.html", files=files, sites=get_sites())


@app.route("/backup.db")
def backup_db():
    """Dump completo do banco.

    Na versao SQLite isto usava sqlite3.Connection.backup para copiar o arquivo.
    Com PostgreSQL o equivalente e o pg_dump em formato custom (-Fc), que ja sai
    comprimido e e restauravel com pg_restore.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fname = f"gestaomac_{stamp}.dump"
    out_path = os.path.join(BACKUP_DIR, fname)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("PGPASSWORD", os.getenv("PGPASSWORD", ""))
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl",
           "--file", out_path, db.dsn_from_env()]
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, timeout=600)
    except FileNotFoundError:
        log.error("pg_dump ausente na imagem")
        flash("pg_dump não está disponível no servidor. Avise a TI.", "err")
        return redirect(url_for("backup_page"))
    except subprocess.TimeoutExpired:
        flash("Backup demorou demais e foi cancelado.", "err")
        return redirect(url_for("backup_page"))
    if res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace")[:300]
        log.error("pg_dump falhou: %s", err)
        flash(f"Falha ao gerar o backup: {err}", "err")
        return redirect(url_for("backup_page"))

    log.info("backup do banco gerado por %s: %s", session.get("user", ""), fname)
    return send_file(out_path, as_attachment=True, download_name=fname)


def _mobile_sites():
    """Sites com WLAN mobile + ocupacao (para os modulos add/remover)."""
    with db.connection() as conn:
        return [s for s in db.overview_summary(
            conn, days=DEFAULT_DAYS, never_mode=NEVER_MODE)["sites"] if s["wlan_id"]]


def _wlan_lock(key) -> bool:
    with db.connection() as conn:
        return db.acquire_wlan_lock(conn, key, session.get("user", ""))


def _wlan_unlock(key) -> None:
    with db.connection() as conn:
        db.release_wlan_lock(conn, key)


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
            with db.connection() as conn:
                if nome:
                    db.upsert_client_info(conn, norm, {"nome": nome})
                db.add_event(conn, int(time.time()), sid, site_desc(sid), norm,
                             "add_manual", f"por {session.get('user','')}")
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
        with db.connection() as conn:
            ci = db.get_client_info(conn, norm) or {}
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
            with db.connection() as conn:
                db.add_event(conn, int(time.time()), sid, site_desc(sid), norm,
                             "remove_manual", f"por {session.get('user','')}")
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
    with db.connection() as conn:
        inv = db.site_inventory(conn, site_id, wlan_id, days=DEFAULT_DAYS,
                                never_mode=NEVER_MODE)
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
    with db.connection() as conn:
        rows = db.list_clients(conn, status="active", search=q or None)
        removed_count = db.removed_macs_with_info(conn)
        ev_ts = db.last_event_ts(conn)
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
    with db.connection() as conn:
        rows = db.list_clients(conn, status="removed", search=q or None)
        ev_ts = db.last_event_ts(conn)
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
    with db.connection() as conn:
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
    with db.connection() as conn:
        if fonte == "unifi":
            rows = db.list_unifi_audit(conn, search=q or None, limit=500)
            total = db.unifi_audit_count(conn)
        else:
            rows = db.recent_events(conn, event=event or None, search=q or None, limit=500)
            total = db.events_count(conn)
    return render_template("auditoria.html", rows=rows, event=event, q=q, fonte=fonte,
                           total=total, labels=db.EVENT_LABEL, sites=get_sites())


@app.route("/auditoria.csv")
def auditoria_csv():
    event = request.args.get("event", "").strip()
    q = request.args.get("q", "").strip()
    with db.connection() as conn:
        rows = db.recent_events(conn, event=event or None, search=q or None,
                                limit=100000)
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


# ======================================================= vouchers (admin)
def _quota_label(q) -> str:
    if q == 0:
        return "Ilimitado"
    if q == 1:
        return "Uso único"
    return f"{q} usos"


@app.route("/vouchers")
def vouchers():
    site_id = request.args.get("site", "").strip()
    q = request.args.get("q", "").strip()
    with db.connection() as conn:
        rows = db.list_voucher_grants(conn, site_id=site_id or None,
                                      search=q or None, limit=500)
        stats = db.voucher_stats(conn)
    return render_template("vouchers.html", rows=rows, stats=stats,
                           site_id=site_id, q=q, sites=get_sites(),
                           quota_label=_quota_label)


@app.route("/vouchers/novo", methods=["GET", "POST"])
def voucher_novo():
    with db.connection() as conn:
        pessoas = [p for p in db.list_portal_users(conn) if p["ativo"]]

    if request.method == "POST":
        f = request.form
        target = f.get("site", "")
        try:
            qtd = int(f.get("quantidade", "1"))
            expira = int(f.get("expiracao", "24"))
        except ValueError:
            flash("Quantidade e expiração precisam ser números.", "err")
            return redirect(url_for("voucher_novo"))

        unidade = f.get("unidade", "horas")
        tipo = f.get("tipo", "single")       # single | multi | unlimited

        if not target:
            flash("Selecione o site.", "err")
            return redirect(url_for("voucher_novo"))
        if not 1 <= qtd <= VOUCHER_MAX_QTD:
            flash(f"Quantidade deve estar entre 1 e {VOUCHER_MAX_QTD}.", "err")
            return redirect(url_for("voucher_novo"))
        if expira < 1:
            flash("A expiração precisa ser de pelo menos 1.", "err")
            return redirect(url_for("voucher_novo"))
        if unidade not in VOUCHER_UNIDADES:
            flash("Unidade de expiração inválida.", "err")
            return redirect(url_for("voucher_novo"))

        if tipo == "single":
            quota = 1
        elif tipo == "unlimited":
            quota = 0
        else:
            try:
                quota = int(f.get("usos", "2"))
            except ValueError:
                quota = 2
            if quota < 2:
                flash("Multi-uso precisa de pelo menos 2 usos.", "err")
                return redirect(url_for("voucher_novo"))

        expire_min = expira * VOUCHER_UNIDADES[unidade]

        def _num(campo, mult=1):
            v = (f.get(campo) or "").strip()
            try:
                return int(float(v) * mult) if v else None
            except ValueError:
                return None

        # a tela pede Mbps; a API trabalha em kbps
        down = _num("download", 1000)
        up = _num("upload", 1000)
        dados = _num("data_limit")

        pid = f.get("portal_user_id") or ""
        portal_user_id = int(pid) if pid.isdigit() else None
        note = f.get("nome", "").strip()

        cli = get_client()
        try:
            with _lock:
                cli.site = target
                criados = cli.create_vouchers(
                    quantidade=qtd, quota=quota, expire_min=expire_min,
                    note=note, down_kbps=down, up_kbps=up, data_mb=dados)
        except UnifiError as exc:
            flash(f"Falha ao gerar: {exc}", "err")
            return redirect(url_for("voucher_novo"))

        sd = site_desc(target)
        agora = int(time.time())
        quem = session.get("user", "")
        linhas = [{
            "code": v.get("code"), "voucher_id": v.get("_id"),
            "site_id": target, "site_desc": sd, "note": note, "quota": quota,
            "duration_min": expire_min, "data_limit_mb": dados,
            "down_kbps": down, "up_kbps": up,
            "portal_user_id": portal_user_id, "criado_por": quem,
            "create_time": v.get("create_time"), "created_at": agora,
        } for v in criados]

        with db.connection() as conn:
            db.record_voucher_grants(conn, linhas)
            destino = ""
            if portal_user_id:
                p = db.get_portal_user(conn, user_id=portal_user_id)
                destino = f" para {p['nome'] or p['username']}" if p else ""
            # Multi-uso e ilimitado ficam marcados a parte: circulam e nao se
            # esgotam sozinhos, entao a geracao precisa ser visivel.
            evento = "voucher_criado" if quota == 1 else "voucher_multi"
            db.add_event(
                conn, agora, target, sd, "-", evento,
                f"{len(criados)}x {_quota_label(quota)}"
                f"{' | ' + note if note else ''}{destino} | por {quem}")
        log.info("vouchers: %d x %s em %s por %s", len(criados),
                 _quota_label(quota), target, quem)

        if quota != 1:
            flash(f"ATENÇÃO: {len(criados)} voucher(s) {_quota_label(quota)} "
                  f"gerado(s) por {quem}. Esse tipo circula — acompanhe.", "warn")
        codigos = [v.get("code") for v in criados]
        lote = criados[0].get("create_time") if criados else None
        return render_template(
            "voucher_gerado.html", criados=criados, site_desc=sd,
            note=note, quota=quota, quota_label=_quota_label(quota),
            expire_min=expire_min, sites=get_sites(), codigos=codigos,
            lote=lote)

    return render_template("voucher_novo.html", sites=_mobile_sites_todos(),
                           pessoas=pessoas, max_qtd=VOUCHER_MAX_QTD,
                           unidades=list(VOUCHER_UNIDADES))


def _mobile_sites_todos():
    """Todos os sites do controller (voucher e por site, nao por WLAN)."""
    return get_sites()


@app.route("/vouchers/<int:grant_id>/revogar", methods=["POST"])
def voucher_revogar(grant_id):
    with db.connection() as conn:
        g = db.get_voucher_grant(conn, grant_id)
    if not g:
        flash("Voucher não encontrado.", "err")
        return redirect(url_for("vouchers"))
    if g["revogado_em"]:
        flash("Esse voucher já estava revogado.", "warn")
        return redirect(url_for("vouchers"))
    if request.form.get("confirm") != "1":
        flash("Marque a confirmação para revogar.", "warn")
        return redirect(url_for("vouchers"))

    quem = session.get("user", "")
    cli = get_client()
    try:
        with _lock:
            cli.site = g["site_id"]
            if g["voucher_id"]:
                cli.delete_voucher(g["voucher_id"])
    except UnifiError as exc:
        flash(f"Falha ao revogar no controller: {exc}", "err")
        return redirect(url_for("vouchers"))

    with db.connection() as conn:
        db.mark_voucher_revogado(conn, grant_id, quem)
        db.add_event(conn, int(time.time()), g["site_id"], g["site_desc"], "-",
                     "voucher_revogado", f"{g['code']} | por {quem}")
    log.info("voucher revogado: %s por %s", g["code"], quem)
    flash(f"Voucher {g['code']} revogado.", "ok")
    return redirect(url_for("vouchers"))


@app.route("/vouchers/imprimir")
def vouchers_imprimir():
    """Pagina otimizada para impressao. O PDF sai pelo Ctrl+P do navegador
    ('Salvar como PDF'), o que evita trazer uma biblioteca de PDF so para
    isso e mantem a impressao direta funcionando igual.

    Aceita `lote` (o create_time carimbado pela UniFi em todos os vouchers da
    mesma geracao) ou `site`, para reimprimir o que ja existe.
    """
    lote = request.args.get("lote", "")
    with db.connection() as conn:
        rows = db.list_voucher_grants(
            conn, site_id=request.args.get("site") or None,
            create_time=int(lote) if lote.isdigit() else None,
            somente_ativos=True, limit=500)
    return render_template("vouchers_imprimir.html", rows=rows,
                           quota_label=_quota_label, agora=int(time.time()))


@app.route("/vouchers.csv")
def vouchers_csv():
    site_id = request.args.get("site", "").strip()
    with db.connection() as conn:
        rows = db.list_voucher_grants(conn, site_id=site_id or None, limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["codigo", "site", "nome", "tipo", "validade_min", "dados_mb",
                "download_kbps", "upload_kbps", "atribuido_a", "gerado_por",
                "gerado_em", "retirado_em", "revogado_em", "revogado_por"])
    for r in rows:
        w.writerow([
            _voucher_fmt(r["code"]), r["site_desc"] or r["site_id"], r["note"] or "",
            _quota_label(r["quota"]), r["duration_min"] or "",
            r["data_limit_mb"] or "", r["down_kbps"] or "", r["up_kbps"] or "",
            r["portal_nome"] or r["portal_username"] or "",
            r["criado_por"] or "", _ts(r["created_at"]), _ts(r["retirado_em"]),
            _ts(r["revogado_em"]), r["revogado_por"] or ""])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="vouchers_{stamp}.csv"'})


# ============================================ usuarios do portal (admin)
@app.route("/portal-users", methods=["GET", "POST"])
def portal_users():
    with db.connection() as conn:
        if request.method == "POST":
            acao = request.form.get("acao", "criar")
            quem = session.get("user", "")
            if acao == "criar":
                usuario = request.form.get("username", "").strip().lower()
                senha = request.form.get("senha", "")
                if not usuario:
                    flash("Informe o usuário.", "err")
                    return redirect(url_for("portal_users"))
                erro = secret.validar_senha(senha)
                if erro:
                    flash(erro, "err")
                    return redirect(url_for("portal_users"))
                if db.get_portal_user(conn, username=usuario):
                    flash(f"Já existe um usuário {usuario}.", "err")
                    return redirect(url_for("portal_users"))
                db.create_portal_user(
                    conn, usuario, senha,
                    nome=request.form.get("nome", "").strip(),
                    setor=request.form.get("setor", "").strip(),
                    unidade=request.form.get("unidade", "").strip(),
                    criado_por=quem)
                log.info("portal_user criado: %s por %s", usuario, quem)
                flash(f"Usuário {usuario} criado. Ele troca a senha no "
                      "primeiro acesso.", "ok")
            elif acao == "senha":
                uid = int(request.form["id"])
                senha = request.form.get("senha", "")
                erro = secret.validar_senha(senha)
                if erro:
                    flash(erro, "err")
                    return redirect(url_for("portal_users"))
                db.set_portal_password(conn, uid, senha, must_change=True)
                log.info("portal_user senha redefinida: id=%s por %s", uid, quem)
                flash("Senha redefinida. A pessoa troca no próximo acesso.", "ok")
            elif acao == "ativar":
                uid = int(request.form["id"])
                u = db.get_portal_user(conn, user_id=uid)
                db.update_portal_user(conn, uid, u["nome"], u["setor"],
                                      u["unidade"], not u["ativo"])
                flash(f"Usuário {u['username']} "
                      f"{'ativado' if not u['ativo'] else 'desativado'}.", "ok")
            elif acao == "remover":
                uid = int(request.form["id"])
                u = db.get_portal_user(conn, user_id=uid)
                db.delete_portal_user(conn, uid)
                log.info("portal_user removido: %s por %s", u["username"], quem)
                flash(f"Usuário {u['username']} removido. Os vouchers dele "
                      "continuam no histórico, sem dono.", "ok")
            return redirect(url_for("portal_users"))

        pessoas = db.list_portal_users(conn)
    return render_template("portal_users.html", pessoas=pessoas,
                           sites=get_sites(), units=UNITS)


@app.template_filter("ts")
def _ts(value):
    if not value:
        return "-"
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(value))


@app.template_filter("voucher")
def _voucher_fmt(code):
    """Formata o codigo como a UniFi exibe e imprime: 02341-26485.

    A API guarda e devolve 10 digitos corridos; o hifen e so apresentacao. O
    banco continua com o valor cru, para casar com o que vem do controller --
    formatar na gravacao faria a comparacao por codigo falhar.
    """
    digitos = re.sub(r"\D", "", str(code or ""))
    if len(digitos) == 10:
        return f"{digitos[:5]}-{digitos[5:]}"
    return code or ""


if MODO_PORTAL:
    from portal import bp as portal_bp
    app.register_blueprint(portal_bp)

# Executado tanto sob gunicorn (import do modulo) quanto em `python app.py`.
bootstrap()
log.info("APP_MODE=%s (painel=%s, portal=%s)", APP_MODE, MODO_ADMIN, MODO_PORTAL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("FLASK_DEBUG", "0") == "1")
