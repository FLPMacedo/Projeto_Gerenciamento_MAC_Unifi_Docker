"""Criptografia das senhas do UniFi guardadas no banco (Fernet).

Sobre a chave
-------------
Por padrao a chave e gerada na primeira execucao e guardada em
`settings.creds_key`. Isso mantem a promessa de "tudo pela web": ninguem
precisa editar arquivo nem variavel para o sistema funcionar.

Seja honesto sobre o que isso protege: com a chave no MESMO banco do texto
cifrado, quem tiver acesso ao banco consegue abrir as senhas. Protege contra
dump de tabela isolado, backup vazado em CSV e leitura casual -- nao contra
comprometimento do banco inteiro.

Para endurecer em producao, defina `CREDS_KEY` (uma chave Fernet) como variavel
de ambiente ou Docker secret: nesse caso a chave nunca toca o banco.
Gerar: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def _key_from_env() -> bytes | None:
    path = os.getenv("CREDS_KEY_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                v = fh.read().strip()
            if v:
                return v.encode()
        except OSError:
            pass
    v = (os.getenv("CREDS_KEY") or "").strip()
    return v.encode() if v else None


def load_key(conn) -> bytes:
    """Chave de cripto: variavel de ambiente se houver, senao a do banco."""
    k = _key_from_env()
    if k:
        return k
    # import tardio: db importa este modulo
    from . import db as _db
    return _db.get_or_create_setting(
        conn, "creds_key", lambda: Fernet.generate_key().decode()).encode()


def encrypt(conn, plaintext: str) -> str:
    if not plaintext:
        return ""
    return Fernet(load_key(conn)).encrypt(plaintext.encode()).decode()


def decrypt(conn, token: str) -> str:
    """Devolve "" se nao der para abrir (chave trocada, dado corrompido)."""
    if not token:
        return ""
    try:
        return Fernet(load_key(conn)).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""
