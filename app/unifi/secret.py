"""Criptografia da senha do UniFi guardada no banco.

A senha e cifrada com Fernet; a chave fica em data/secret.key (arquivo local,
fora do git), nunca no codigo.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet


# ------------------------------------------------- criptografia (UniFi)
def _load_key(key_path: str) -> bytes:
    if not os.path.exists(key_path):
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        key = Fernet.generate_key()
        with open(key_path, "wb") as fh:
            fh.write(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key
    with open(key_path, "rb") as fh:
        return fh.read()


def encrypt(key_path: str, plaintext: str) -> str:
    if not plaintext:
        return ""
    return Fernet(_load_key(key_path)).encrypt(plaintext.encode()).decode()


def decrypt(key_path: str, token: str) -> str:
    if not token:
        return ""
    try:
        return Fernet(_load_key(key_path)).decrypt(token.encode()).decode()
    except Exception:
        return ""
