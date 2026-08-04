"""Portal de retirada de vouchers.

Publico diferente do painel administrativo: portaria, recepcao, lideres de
unidade. Essas pessoas nao tem conta no controller UniFi, entao entram com
credencial LOCAL criada pela TI (tabela `portal_users`, senha em hash scrypt).

Isolamento
----------
Sao dois espacos de sessao distintos: o painel usa `session["user"]` (conta
UniFi) e o portal usa `session["portal_uid"]`. Um nao serve para o outro. Alem
disso, quando o container sobe com APP_MODE=portal as rotas administrativas
sequer sao registradas -- quem acessa aquela porta nao tem como alcanca-las.

O portal e SOMENTE LEITURA sobre os vouchers: mostra os codigos atribuidos a
quem esta logado e marca a data da primeira visualizacao. Nao cria, nao revoga
e nao fala com o controller.
"""
from __future__ import annotations

import logging
import time

from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for,
)

from unifi import db
from unifi import secret

log = logging.getLogger("portal")

bp = Blueprint("portal", __name__, url_prefix="/portal")

PUBLICOS = {"portal.login", "portal.healthz"}


def usuario_atual(conn) -> dict | None:
    uid = session.get("portal_uid")
    if not uid:
        return None
    u = db.get_portal_user(conn, user_id=uid)
    # conta desativada pela TI derruba a sessao na proxima navegacao
    return u if (u and u["ativo"]) else None


@bp.before_request
def _guard():
    if request.endpoint in PUBLICOS:
        return
    with db.connection() as conn:
        u = usuario_atual(conn)
    if not u:
        session.pop("portal_uid", None)
        return redirect(url_for("portal.login", next=request.path))
    # senha provisoria: obriga a trocar antes de ver qualquer coisa
    if u["must_change"] and request.endpoint != "portal.trocar_senha":
        return redirect(url_for("portal.trocar_senha"))


@bp.route("/healthz")
def healthz():
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "area": "portal"}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}, 503


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("username", "").strip()
        senha = request.form.get("password", "")
        with db.connection() as conn:
            u, motivo = db.check_portal_login(conn, usuario, senha)
        if not u:
            # mensagem generica de proposito: nao revela se o usuario existe
            flash(motivo, "err")
            log.info("portal: login recusado para %r (%s)", usuario, motivo[:40])
            return render_template("portal_login.html", user_saved=usuario)
        session["portal_uid"] = u["id"]
        session["portal_nome"] = u["nome"] or u["username"]
        log.info("portal: login de %s", u["username"])
        if u["must_change"]:
            return redirect(url_for("portal.trocar_senha"))
        return redirect(request.args.get("next") or url_for("portal.meus"))
    return render_template("portal_login.html", user_saved="")


@bp.route("/logout")
def logout():
    session.pop("portal_uid", None)
    session.pop("portal_nome", None)
    flash("Sessão encerrada.", "ok")
    return redirect(url_for("portal.login"))


@bp.route("/senha", methods=["GET", "POST"])
def trocar_senha():
    with db.connection() as conn:
        u = usuario_atual(conn)
        if request.method == "POST":
            atual = request.form.get("atual", "")
            nova = request.form.get("nova", "")
            conf = request.form.get("confirma", "")
            # na troca obrigatoria do primeiro acesso nao pedimos a atual de
            # novo: a pessoa acabou de digita-la no login
            if not u["must_change"] and not secret.verify_password(
                    u["password_hash"], atual):
                flash("Senha atual incorreta.", "err")
                return render_template("portal_senha.html", u=u)
            if nova != conf:
                flash("A confirmação não confere com a nova senha.", "err")
                return render_template("portal_senha.html", u=u)
            erro = secret.validar_senha(nova)
            if erro:
                flash(erro, "err")
                return render_template("portal_senha.html", u=u)
            db.set_portal_password(conn, u["id"], nova, must_change=False)
            log.info("portal: %s trocou a senha", u["username"])
            flash("Senha alterada.", "ok")
            return redirect(url_for("portal.meus"))
    return render_template("portal_senha.html", u=u)


def _meus_vouchers(conn, u, marcar_retirada=True):
    """So os que ainda funcionam: entregar codigo ja usado nao serve para nada."""
    vouchers = db.list_voucher_grants(
        conn, portal_user_id=u["id"], somente_disponiveis=True, limit=300)
    if marcar_retirada:
        for v in vouchers:
            if not v["retirado_em"]:
                db.mark_voucher_retirado(conn, v["id"])
                v["retirado_em"] = int(time.time())
    return vouchers


@bp.route("/")
def meus():
    """Vouchers atribuidos a quem esta logado."""
    with db.connection() as conn:
        u = usuario_atual(conn)
        vouchers = _meus_vouchers(conn, u)
    return render_template("portal_meus.html", u=u, vouchers=vouchers)


@bp.route("/imprimir")
def imprimir():
    """Folha de cartoes, a MESMA do painel de gestao.

    O botao daqui antes chamava window.print() na propria tela, o que mandava
    para o papel o menu, a caixa de copiar e a tabela inteira. Reaproveitar o
    template de impressao entrega a folha recortavel, com um cartao por
    voucher.
    """
    with db.connection() as conn:
        u = usuario_atual(conn)
        # nao remarca a retirada: ela ja foi registrada ao abrir "Meus vouchers"
        vouchers = _meus_vouchers(conn, u, marcar_retirada=False)
        stats = db.voucher_stats(conn)
    return render_template(
        "vouchers_imprimir.html", rows=vouchers, agora=int(time.time()),
        quota_label=lambda q: ("Ilimitado" if q == 0 else
                               "Uso único" if q == 1 else f"{q} usos"),
        status_label=db.VOUCHER_STATUS_LABEL,
        todos=False, parcial=False, ultima_sync=stats["ultima_sync"],
        titular=u["nome"] or u["username"])
