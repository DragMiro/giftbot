"""SQLite-хранилище аккаунтов пользователей бота."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from config import get_settings
from storage.crypto import decrypt, encrypt, is_encrypted, master_key
from storage.fs import secure_path, secure_tree

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None


@dataclass(slots=True)
class UserAccount:
    user_id: int
    api_id: int
    api_hash: str
    phone: str | None
    tg_user_id: int | None
    tg_username: str | None
    tg_first_name: str | None
    authorized_at: str | None
    session_enc: str | None = None


def _db_path() -> Path:
    settings = get_settings()
    root = Path(settings.data_dir)
    secure_path(root, is_dir=True)
    return root / "giftbot.db"


def _decrypt_field(value: str | None) -> str | None:
    if not value:
        return None
    if is_encrypted(value):
        return decrypt(value)
    return value


def _parse_api_id(raw) -> int:
    if isinstance(raw, int):
        return raw
    text = str(raw)
    if is_encrypted(text):
        return int(decrypt(text))
    return int(text)


def _row_to_account(row: aiosqlite.Row) -> UserAccount:
    phone_raw = row["phone_enc"] if "phone_enc" in row.keys() and row["phone_enc"] else row["phone"]
    return UserAccount(
        user_id=row["user_id"],
        api_id=_parse_api_id(row["api_id"]),
        api_hash=decrypt(row["api_hash_enc"]),
        phone=_decrypt_field(phone_raw),
        tg_user_id=row["tg_user_id"],
        tg_username=row["tg_username"],
        tg_first_name=row["tg_first_name"],
        authorized_at=row["authorized_at"],
        session_enc=row["session_enc"] if "session_enc" in row.keys() else None,
    )


async def _migrate_schema(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(users)") as cur:
        cols = {row[1] for row in await cur.fetchall()}

    if "session_enc" not in cols:
        await conn.execute("ALTER TABLE users ADD COLUMN session_enc TEXT")
    if "phone_enc" not in cols:
        await conn.execute("ALTER TABLE users ADD COLUMN phone_enc TEXT")

    # Перешифровать legacy plaintext phone → phone_enc
    async with conn.execute(
        "SELECT user_id, phone FROM users WHERE phone IS NOT NULL AND phone != '' "
        "AND (phone_enc IS NULL OR phone_enc = '')"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await conn.execute(
            "UPDATE users SET phone_enc = ?, phone = NULL WHERE user_id = ?",
            (encrypt(row["phone"]), row["user_id"]),
        )

    async with conn.execute("SELECT user_id, api_id FROM users") as cur:
        api_rows = await cur.fetchall()
    for row in api_rows:
        if not is_encrypted(str(row["api_id"])):
            await conn.execute(
                "UPDATE users SET api_id = ? WHERE user_id = ?",
                (encrypt(str(row["api_id"])), row["user_id"]),
            )

    await conn.commit()


async def init_db() -> None:
    global _db
    if _db is not None:
        return

    root = Path(get_settings().data_dir)
    secure_tree(root)

    _db = await aiosqlite.connect(_db_path())
    _db.row_factory = aiosqlite.Row
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            api_id TEXT NOT NULL,
            api_hash_enc TEXT NOT NULL,
            phone TEXT,
            phone_enc TEXT,
            session_enc TEXT,
            tg_user_id INTEGER,
            tg_username TEXT,
            tg_first_name TEXT,
            authorized_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    await _db.commit()
    await _migrate_schema(_db)

    try:
        secure_path(_db_path(), is_dir=False)
    except OSError:
        pass

    logger.info("БД: %s", _db_path())


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def get_user(user_id: int) -> UserAccount | None:
    assert _db is not None
    async with _db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_account(row)


async def save_user(
    *,
    user_id: int,
    api_id: int,
    api_hash: str,
    phone: str | None,
    tg_user_id: int | None,
    tg_username: str | None,
    tg_first_name: str | None,
    session_enc: str | None = None,
) -> None:
    assert _db is not None
    now = datetime.now(timezone.utc).isoformat()
    phone_enc = encrypt(phone) if phone else None
    await _db.execute(
        """
        INSERT INTO users (
            user_id, api_id, api_hash_enc, phone, phone_enc, session_enc,
            tg_user_id, tg_username, tg_first_name,
            authorized_at, created_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            api_id = excluded.api_id,
            api_hash_enc = excluded.api_hash_enc,
            phone = NULL,
            phone_enc = excluded.phone_enc,
            session_enc = excluded.session_enc,
            tg_user_id = excluded.tg_user_id,
            tg_username = excluded.tg_username,
            tg_first_name = excluded.tg_first_name,
            authorized_at = excluded.authorized_at
        """,
        (
            user_id,
            encrypt(str(api_id)),
            encrypt(api_hash),
            phone_enc,
            session_enc,
            tg_user_id,
            tg_username,
            tg_first_name,
            now,
            now,
        ),
    )
    await _db.commit()


async def update_session_enc(user_id: int, session_enc: str) -> None:
    assert _db is not None
    await _db.execute(
        "UPDATE users SET session_enc = ? WHERE user_id = ?",
        (session_enc, user_id),
    )
    await _db.commit()


async def remove_user(user_id: int) -> None:
    assert _db is not None
    await _db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    await _db.commit()
