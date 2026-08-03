"""Modelo HIBRIDO de credenciais do UniFi.

Como funciona
-------------
- Cada pessoa entra com a PROPRIA conta UniFi, na tela de login -- como na
  versao desktop. Nada de credencial em variavel de ambiente.
- A senha e guardada CIFRADA no banco (`user_creds`, Fernet). Com isso:
  * as telas e as acoes de escrita usam a conta de QUEM ESTA LOGADO, entao o
    log nativo da UniFi registra o autor real de cada alteracao (o desktop
    nao fazia isso: tudo saia com uma conta so);
  * o coletor, que roda de madrugada sem ninguem logado, tem uma credencial
    valida para usar.
- O coletor usa a credencial que autenticou mais recentemente. Se ela deixar de
  valer (a pessoa trocou a senha), ele tenta as anteriores antes de desistir.

Endereco do controller
----------------------
`host`/`site` ficam em `settings` e sao editaveis pela tela de configuracao --
tudo pela web, sem editar arquivo. As variaveis UNIFI_HOST/UNIFI_SITE servem
apenas como semente do primeiro arranque.

Conta de servico (opcional)
---------------------------
Se UNIFI_SERVICE_USERNAME/PASSWORD estiverem definidos, o COLETOR prefere essa
conta -- util para que a coleta nunca pare por troca de senha pessoal. As telas
continuam usando a conta de cada usuario de qualquer forma.
"""
from __future__ import annotations

import os


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def _env(name: str, default: str = "") -> str:
    """Le a variavel aceitando o padrao Docker secret `<NOME>_FILE`."""
    path = os.getenv(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return os.getenv(name, default)


# ------------------------------------------------- endereco do controller
def get_host(conn) -> dict:
    """host/site/verify efetivos: banco primeiro, .env como semente."""
    from . import db as _db
    host = _db.get_setting(conn, "unifi_host", "") or _env("UNIFI_HOST")
    site = _db.get_setting(conn, "unifi_site", "") or _env("UNIFI_SITE", "default")
    verify_raw = _db.get_setting(conn, "unifi_verify", None)
    verify = (verify_raw == "1") if verify_raw is not None \
        else _truthy(_env("UNIFI_VERIFY_SSL"))
    return {"host": (host or "").rstrip("/"), "site": site or "default",
            "verify": verify}


def set_host(conn, host: str, site: str, verify: bool) -> None:
    from . import db as _db
    _db.set_setting(conn, "unifi_host", (host or "").strip().rstrip("/"))
    _db.set_setting(conn, "unifi_site", (site or "default").strip() or "default")
    _db.set_setting(conn, "unifi_verify", "1" if verify else "0")


def is_configured(conn) -> bool:
    return bool(get_host(conn)["host"])


# --------------------------------------------------- conta de servico (opc.)
def service_account() -> dict | None:
    """Credencial fixa opcional para o coletor. None se nao configurada."""
    user = _env("UNIFI_SERVICE_USERNAME").strip()
    pw = _env("UNIFI_SERVICE_PASSWORD")
    if not user or not pw:
        return None
    return {"username": user, "password": pw}


# ----------------------------------------------- credenciais do usuario logado
def user_credentials(conn, username: str) -> dict | None:
    """Credencial da pessoa logada, para as telas e as acoes de escrita."""
    from . import db as _db
    return _db.get_user_creds(conn, username)


def collector_candidates(conn) -> list[dict]:
    """Credenciais que o coletor deve tentar, na ordem.

    A conta de servico (se existir) vem primeiro por ser estavel; em seguida as
    contas de usuario, da que autenticou mais recentemente para a mais antiga.
    """
    from . import db as _db
    cfg = get_host(conn)
    out: list[dict] = []
    svc = service_account()
    if svc and cfg["host"]:
        out.append({"username": svc["username"], "password": svc["password"],
                    "host": cfg["host"], "site": cfg["site"],
                    "verify": cfg["verify"], "origem": "conta de servico"})
    for c in _db.collector_creds(conn):
        out.append({**c, "origem": f"login de {c['username']}"})
    return out


def describe(conn) -> str:
    """Resumo sem segredo, para a tela de configuracao e para os logs."""
    from . import db as _db
    cfg = get_host(conn)
    if not cfg["host"]:
        return "controller nao configurado (defina o host na tela de Configuração)"
    n = len(_db.list_user_creds(conn))
    svc = "com conta de servico" if service_account() else "sem conta de servico"
    return (f"host={cfg['host']} site={cfg['site']} verify_ssl={cfg['verify']} "
            f"| {n} credencial(is) de usuario | {svc}")
