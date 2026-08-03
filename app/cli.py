"""CLI somente leitura para inspecionar a rede mobile no UniFi.

Este sistema NAO adiciona, remove, bloqueia ou renomeia nada no controller.
Apenas le e exibe informacoes.

Exemplos:
    python cli.py sites
    python cli.py list                # site do .env (UNIFI_SITE)
    python cli.py list --site <id-do-site>
"""
from __future__ import annotations

import argparse
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

from unifi import UnifiClient, UnifiError


def build_client() -> UnifiClient:
    load_dotenv()
    try:
        host = os.environ["UNIFI_HOST"]
        username = os.environ["UNIFI_USERNAME"]
        password = os.environ["UNIFI_PASSWORD"]
    except KeyError as exc:
        sys.exit(f"Variavel de ambiente ausente: {exc}. Copie .env.example para .env.")
    return UnifiClient(
        host=host, username=username, password=password,
        site=os.getenv("UNIFI_SITE", "default"),
        verify_ssl=os.getenv("UNIFI_VERIFY_SSL", "false").lower()
        in {"1", "true", "yes", "on", "sim"},
    )


def cmd_sites(client: UnifiClient, args: argparse.Namespace) -> None:
    sites = client.get_sites()
    print(f"{len(sites)} site(s):\n")
    for s in sites:
        print(f"  {s['id']:<12} {s['desc']}")


def cmd_list(client: UnifiClient, args: argparse.Namespace) -> None:
    if args.site:
        client.site = args.site
    rows = client.get_clients()
    rows = sorted(rows, key=lambda c: (c.get("name") or c.get("hostname") or "").lower())
    print(f"{len(rows)} cliente(s) online em '{client.site}':\n")
    print(f"{'MAC':<18} {'IP':<16} NOME")
    print("-" * 60)
    for c in rows:
        ip = c.get("ip") or c.get("last_ip") or ""
        name = c.get("name") or c.get("hostname") or c.get("oui") or "(sem nome)"
        print(f"{c.get('mac', ''):<18} {ip:<16} {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspeção SOMENTE LEITURA da rede mobile UniFi.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sites = sub.add_parser("sites", help="Lista os sites do controller")
    p_sites.set_defaults(func=cmd_sites)

    p_list = sub.add_parser("list", help="Lista clientes online")
    p_list.add_argument("--site", help="id do site (default: UNIFI_SITE do .env)")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    try:
        with build_client() as client:
            args.func(client, args)
    except UnifiError as exc:
        sys.exit(f"Erro: {exc}")


if __name__ == "__main__":
    main()
