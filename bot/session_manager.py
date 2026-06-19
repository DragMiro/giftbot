"""Per-user Telethon-сессии: encrypted StringSession в SQLite, без .session на диске."""

from __future__ import annotations

import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from config import get_settings
from core.gifts import GiftCatalog, GiftSender
from storage import UserAccount, get_user, remove_user, save_user, update_session_enc
from storage.crypto import encrypt
from storage.fs import secure_tree

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        self._catalogs: dict[int, GiftCatalog] = {}
        self._senders: dict[int, GiftSender] = {}
        self._pending: dict[int, TelegramClient] = {}

    def _legacy_session_path(self, user_id: int) -> Path:
        return Path(get_settings().data_dir) / "sessions" / str(user_id)

    def _purge_legacy_files(self, user_id: int) -> None:
        base = self._legacy_session_path(user_id)
        for suffix in ("", "-journal"):
            path = Path(f"{base}.session{suffix}")
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Не удалось удалить %s: %s", path, exc)

    async def is_linked(self, user_id: int) -> bool:
        account = await get_user(user_id)
        if account is None:
            return False
        if account.session_enc:
            return True
        legacy = Path(f"{self._legacy_session_path(user_id)}.session")
        return legacy.exists()

    async def get_catalog(self, user_id: int) -> GiftCatalog:
        await self._ensure_client(user_id)
        return self._catalogs[user_id]

    async def get_sender(self, user_id: int) -> GiftSender:
        await self._ensure_client(user_id)
        return self._senders[user_id]

    async def _load_session_string(self, user_id: int, account: UserAccount) -> str:
        if account.session_enc:
            from storage.crypto import decrypt

            return decrypt(account.session_enc)

        legacy = self._legacy_session_path(user_id)
        legacy_file = Path(f"{legacy}.session")
        if not legacy_file.exists():
            raise RuntimeError("Сессия не найдена. /login — привязать заново")

        client = TelegramClient(str(legacy), account.api_id, account.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия истекла. /login — привязать заново")
            session_str = client.session.save()
        finally:
            await client.disconnect()

        enc = encrypt(session_str)
        await update_session_enc(user_id, enc)
        self._purge_legacy_files(user_id)
        logger.info("Legacy .session мигрирован в БД для user_id=%s", user_id)
        return session_str

    async def _ensure_client(self, user_id: int) -> TelegramClient:
        if user_id in self._clients:
            client = self._clients[user_id]
            if not client.is_connected():
                await client.connect()
            return client

        account = await get_user(user_id)
        if account is None:
            raise RuntimeError("Аккаунт не привязан. Используй /login")

        session_str = await self._load_session_string(user_id, account)
        client = TelegramClient(
            StringSession(session_str),
            account.api_id,
            account.api_hash,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await self.disconnect_user(user_id)
            await remove_user(user_id)
            raise RuntimeError("Сессия истекла. Привяжи аккаунт заново: /login")

        me = await client.get_me()
        logger.info("User %s → TG @%s", user_id, me.username)
        settings = get_settings()
        self._clients[user_id] = client
        self._catalogs[user_id] = GiftCatalog(client)
        self._senders[user_id] = GiftSender(client, delay=settings.gift_send_delay)
        return client

    async def _persist_session(self, user_id: int, client: TelegramClient) -> None:
        session_str = client.session.save()
        if not session_str:
            return
        await update_session_enc(user_id, encrypt(session_str))
        self._purge_legacy_files(user_id)

    async def start_login(self, user_id: int, api_id: int, api_hash: str, phone: str) -> None:
        await self.cancel_login(user_id)
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        await client.send_code_request(phone)
        self._pending[user_id] = client
        logger.info("Код отправлен для bot_user_id=%s", user_id)

    async def confirm_code(
        self,
        user_id: int,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        code: str,
    ) -> str | None:
        client = self._pending.get(user_id)
        if client is None:
            raise RuntimeError("Сессия входа не найдена. Начни заново: /login")

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            return "password"

        await self._finalize_login(user_id, client, api_id, api_hash, phone)
        return None

    async def confirm_password(
        self,
        user_id: int,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        password: str,
    ) -> None:
        client = self._pending.get(user_id)
        if client is None:
            raise RuntimeError("Сессия входа не найдена. Начни заново: /login")
        await client.sign_in(password=password)
        await self._finalize_login(user_id, client, api_id, api_hash, phone)

    async def _finalize_login(
        self,
        user_id: int,
        client: TelegramClient,
        api_id: int,
        api_hash: str,
        phone: str,
    ) -> None:
        me = await client.get_me()
        session_str = client.session.save()
        session_enc = encrypt(session_str) if session_str else None

        await save_user(
            user_id=user_id,
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            tg_user_id=me.id,
            tg_username=me.username,
            tg_first_name=me.first_name,
            session_enc=session_enc,
        )
        self._pending.pop(user_id, None)
        self._purge_legacy_files(user_id)

        settings = get_settings()
        self._clients[user_id] = client
        self._catalogs[user_id] = GiftCatalog(client)
        self._senders[user_id] = GiftSender(client, delay=settings.gift_send_delay)
        logger.info("Аккаунт привязан: bot_user=%s tg=@%s", user_id, me.username)

    async def cancel_login(self, user_id: int) -> None:
        client = self._pending.pop(user_id, None)
        if client is not None:
            await client.disconnect()

    async def disconnect_user(self, user_id: int) -> None:
        await self.cancel_login(user_id)
        client = self._clients.pop(user_id, None)
        self._catalogs.pop(user_id, None)
        self._senders.pop(user_id, None)
        if client is not None:
            if client.session:
                await self._persist_session(user_id, client)
            await client.disconnect()

    async def logout(self, user_id: int) -> None:
        await self.disconnect_user(user_id)
        self._purge_legacy_files(user_id)

    async def shutdown(self) -> None:
        for uid in list(self._pending.keys()):
            await self.cancel_login(uid)
        for uid in list(self._clients.keys()):
            await self.disconnect_user(uid)
        secure_tree(Path(get_settings().data_dir))


sessions = SessionManager()
