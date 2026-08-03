r"""Importa cadastros de uma planilha .xlsx para o banco local (client_info).

SIMULA por padrao (nao grava nada). Para gravar de fato, use --apply.

Uso:
    .\.venv\Scripts\python.exe importar.py "Wifi (1).xlsx"           # simulacao
    .\.venv\Scripts\python.exe importar.py "Wifi (1).xlsx" --apply   # grava
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

from unifi import db
from unifi.sheet_import import finalize, parse_workbook

load_dotenv()


def _merge_units(a: str, b: str) -> str:
    toks = set()
    for s in (a, b):
        for t in (s or "").replace(";", ",").split(","):
            if t.strip():
                toks.add(t.strip())
    return ", ".join(sorted(toks))


def _merge_notes(old: str, new: str) -> str:
    parts = [p for p in (old or "").split(" | ") if p]
    for p in (new or "").split(" | "):
        if p and p not in parts:
            parts.append(p)
    return " | ".join(parts)[:2000]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    path = args[0] if args else "Wifi (1).xlsx"
    if not os.path.exists(path):
        sys.exit(f"Arquivo nao encontrado: {path}")

    records, stats = parse_workbook(path)

    conn = db.connect(os.getenv("DB_PATH"))
    active = {r["mac"] for r in conn.execute(
        "SELECT DISTINCT mac FROM mac_state WHERE in_allow_list=1")}

    print(f"\n=== {'GRAVANDO' if apply else 'SIMULACAO (nada gravado)'} :: {path} ===\n")
    print(f"{'ABA':<24} {'unid':>5} {'linhas':>7} {'import':>7} {'sem MAC':>8}")
    print("-" * 56)
    for s in stats:
        print(f"{s['sheet']:<24} {str(s['unit'] or '-'):>5} "
              f"{s['rows']:>7} {s['imported']:>7} {s['skipped']:>8}")

    total_macs = len(records)
    com_nome = sum(1 for r in records.values() if r["nome"])
    na_rede = sum(1 for mac in records if mac in active)
    fora = total_macs - na_rede

    print("\n--- RESUMO ---")
    print(f"  MACs unicos na planilha : {total_macs}")
    print(f"  com nome                : {com_nome}")
    print(f"  na rede (ativos)        : {na_rede}")
    print(f"  fora da rede (-> removido/deletado, dados guardados): {fora}")

    if not apply:
        print("\n  Amostra (5 primeiros):")
        for mac, rec in list(records.items())[:5]:
            f = finalize(rec)
            print(f"   {mac} | {f['nome'][:30]:<30} | unid={f['unidade'] or '-':<10} "
                  f"| setor={f['setor'][:14]}")
        print("\n>> SIMULACAO. Para gravar de fato rode novamente com --apply")
        conn.close()
        return

    # grava (merge com cadastro existente)
    written = 0
    for mac, rec in records.items():
        f = finalize(rec)
        cur = db.get_client_info(conn, mac) or {}
        merged = {
            "nome": f["nome"] or cur.get("nome", ""),
            "setor": f["setor"] or cur.get("setor", ""),
            "funcao": f["funcao"] or cur.get("funcao", ""),
            "lider": f["lider"] or cur.get("lider", ""),
            "chamado": f["chamado"] or cur.get("chamado", ""),
            "unidade": _merge_units(cur.get("unidade", ""), f["unidade"]),
            "notes": _merge_notes(cur.get("notes", ""), f["notes"]),
        }
        db.upsert_client_info(conn, mac, merged)
        written += 1
    conn.close()
    print(f"\n  GRAVADO: {written} cadastro(s) em client_info.")


if __name__ == "__main__":
    main()
