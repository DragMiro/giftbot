"""Шифрование чувствительных данных (Fernet + SHA-256 key derivation)."""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from config import get_settings

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"


def master_key() -> str:
    settings = get_settings()
    key = (settings.encryption_key or "").strip()
    if key:
        return key
    token = (settings.bot_token or "").strip()
    if token:
        logger.warning(
            "ENCRYPTION_KEY не задан — используется BOT_TOKEN. "
            "Задай отдельный ENCRYPTION_KEY в .env для production."
        )
        return token
    raise RuntimeError("Задай ENCRYPTION_KEY или BOT_TOKEN в .env")


def _fernet(key: str) -> Fernet:
    raw = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(value: str, key: str | None = None) -> str:
    if not value:
        return ""
    k = key or master_key()
    token = _fernet(k).encrypt(value.encode()).decode()
    return f"{_ENC_PREFIX}{token}"


def decrypt(token: str, key: str | None = None) -> str:
    if not token:
        return ""
    k = key or master_key()
    raw = token[len(_ENC_PREFIX) :] if token.startswith(_ENC_PREFIX) else token
    try:
        return _fernet(k).decrypt(raw.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Не удалось расшифровать данные (неверный ENCRYPTION_KEY?)") from exc


def is_encrypted(value: str | None) -> bool:
    return bool(value and value.startswith(_ENC_PREFIX))
