"""Revoga vouchers em lote (apaga no controller e registra na auditoria).

Existe porque revogar de um em um pela tela nao escala: um lote de teste com
100 codigos exigiria 100 confirmacoes.

    # ver o que seria revogado (nao altera nada)
    python scripts/revogar_lote.py --lote 1785500000
    python scripts/revogar_lote.py --site vpueege8 --nota TI

    # revogar de fato
    python scripts/revogar_lote.py --lote 1785500000 --aplicar

Revogar apaga o codigo no controller e NAO tem volta. O registro em
voucher_grants permanece, marcado como revogado, para o historico de quem
recebeu o que continuar existindo.
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def _achar_pacote() -> None:
    aqui = os.path.dirname(os.path.abspath(__file__))
    raiz = os.path.dirname(aqui)
    for cand in (os.path.join(raiz, "app"), raiz, "/app"):
        if os.path.isdir(os.path.join(cand, "unifi")):
            sys.path.insert(0, cand)
            return
    sys.exit("nao encontrei o pacote `unifi` a partir de " + aqui)


_achar_pacote()

from unifi import db  # noqa: E402
from unifi import config as cfgmod  # noqa: E402
from unifi.client import UnifiClient, UnifiError  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Revoga vouchers em lote")
    ap.add_argument("--lote", help="create_time do lote (todos gerados juntos)")
    ap.add_argument("--site", help="id do site")
    ap.add_argument("--nota", help="filtra pela finalidade (campo Nome)")
    ap.add_argument("--aplicar", action="store_true",
                    help="revoga de fato; sem isto so mostra")
    ap.add_argument("--por", default="script",
                    help="quem esta revogando (vai para a auditoria)")
    args = ap.parse_args()

    if not (args.lote or args.site or args.nota):
        sys.exit("informe ao menos --lote, --site ou --nota")

    db.wait_ready(float(os.getenv("DB_WAIT_TIMEOUT", "60")))
    with db.connection() as conn:
        alvos = db.list_voucher_grants(
            conn, site_id=args.site or None,
            create_time=int(args.lote) if args.lote else None,
            somente_ativos=True, limit=100000)
        if args.nota:
            alvos = [r for r in alvos if (r["note"] or "") == args.nota]

    if not alvos:
        print("nenhum voucher corresponde ao filtro.")
        return

    agora = int(time.time())
    ainda_valem = sum(
        1 for r in alvos
        if (r["created_at"] or 0) + (r["duration_min"] or 0) * 60 > agora)

    print(f"\n{len(alvos)} voucher(s) selecionado(s):")
    print(f"  site      : {alvos[0]['site_desc']}")
    print(f"  finalidade: {alvos[0]['note'] or '-'}")
    print(f"  ainda validos no controller: {ainda_valem}")
    print(f"  ja vencidos                : {len(alvos) - ainda_valem}")
    print("\n  amostra:")
    for r in alvos[:5]:
        print(f"    {r['code'][:5]}-{r['code'][5:]}  {r['status'] or 'nao conferido'}")
    if len(alvos) > 5:
        print(f"    ... e mais {len(alvos) - 5}")

    if not args.aplicar:
        print("\n>> SIMULACAO. Nada foi alterado. Repita com --aplicar para revogar.")
        return

    with db.connection() as conn:
        cands = cfgmod.collector_candidates(conn)
    if not cands:
        sys.exit("sem credencial disponivel: faca login na interface web.")
    cr = cands[0]
    cli = UnifiClient(host=cr["host"], username=cr["username"],
                      password=cr["password"], site=cr["site"],
                      verify_ssl=cr["verify"])
    cli.login()
    print(f"\nrevogando com a conta de {cr['username']}...")

    ok = falhou = ja_sumiu = 0
    with db.connection() as conn:
        for i, r in enumerate(alvos, 1):
            if not r["voucher_id"]:
                ja_sumiu += 1
                db.mark_voucher_revogado(conn, r["id"], args.por)
                continue
            try:
                cli.site = r["site_id"]
                cli.delete_voucher(r["voucher_id"])
                ok += 1
            except UnifiError as exc:
                # ja expurgado pela UniFi conta como sucesso: o objetivo
                # (o codigo nao funcionar mais) ja esta cumprido
                if "404" in str(exc) or "not found" in str(exc).lower():
                    ja_sumiu += 1
                else:
                    falhou += 1
                    print(f"  falhou {r['code']}: {str(exc)[:90]}")
                    continue
            db.mark_voucher_revogado(conn, r["id"], args.por)
            if i % 20 == 0:
                print(f"  {i}/{len(alvos)}...")

        db.add_event(conn, agora, alvos[0]["site_id"], alvos[0]["site_desc"],
                     "-", "voucher_revogado",
                     f"lote de {len(alvos)} | {ok} no controller, "
                     f"{ja_sumiu} ja ausentes | por {args.por}")

    cli.logout()
    print(f"\nrevogados no controller : {ok}")
    print(f"ja nao estavam la       : {ja_sumiu}")
    print(f"falhas                  : {falhou}")
    print("\nOs registros continuam no historico, marcados como revogados.")


if __name__ == "__main__":
    main()
