"""Credenciais do UniFi no servidor: CONTA DE SERVICO unica.

Por que mudou em relacao ao desktop
-----------------------------------
Na versao desktop cada usuario usava a PROPRIA conta UniFi, e a senha dele era
gravada cifrada num `creds.enc` LOCAL de cada maquina. Esse desenho nao
sobrevive ao Docker: com N pessoas atendidas pelo MESMO container existe um
unico `creds.enc`, e o ultimo que fizesse login sobrescreveria a credencial de
todos -- a coleta agendada passaria a rodar com a conta de quem entrou por
ultimo, sem ninguem perceber.

Como funciona agora
-------------------
- Uma conta de servico dedicada (somente leitura no controller + permissao de
  editar a allow-list) faz TODA a comunicacao com a UniFi: coleta e as acoes de
  add/remover/troca.
- O login da tela continua sendo validado contra o controller com a conta
  PESSOAL de cada um (ver `login()` em app.py), garantindo que so quem tem
  acesso no UniFi entra. A senha do usuario NAO e persistida em lugar nenhum.
- A autoria das acoes continua rastreada por pessoa: os eventos gravados em
  `events` registram `session["user"]` como autor.

A credencial vem de variavel de ambiente ou de Docker secret. Nunca da imagem.
"""
from __future__ import annotations

import os


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def _env(name: str, default: str = "") -> str:
    """Le uma variavel aceitando tambem o padrao Docker secret `<NOME>_FILE`.

    O Portainer/Compose monta segredos como arquivo em /run/secrets/<nome>;
    apontar UNIFI_SERVICE_PASSWORD_FILE para la evita a senha aparecer em
    `docker inspect` ou na listagem de variaveis do stack.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return os.getenv(name, default)


def resolve() -> dict | None:
    """Credenciais da conta de servico, ou None se nao estiver configurada."""
    host = _env("UNIFI_HOST").strip()
    if not host:
        return None
    return {
        "host": host.rstrip("/"),
        "site": _env("UNIFI_SITE", "default").strip() or "default",
        "username": _env("UNIFI_SERVICE_USERNAME").strip(),
        "password": _env("UNIFI_SERVICE_PASSWORD"),
        "verify": _truthy(_env("UNIFI_VERIFY_SSL")),
    }


def is_configured() -> bool:
    c = resolve()
    return bool(c and c["host"] and c["username"] and c["password"])


def describe() -> str:
    """Resumo sem segredo, para a tela de configuracao e para os logs."""
    c = resolve()
    if not c:
        return "nao configurado (defina UNIFI_HOST)"
    who = c["username"] or "?"
    pw = "definida" if c["password"] else "AUSENTE"
    return (f"host={c['host']} site={c['site']} conta_servico={who} "
            f"senha={pw} verify_ssl={c['verify']}")
