"""Legacy: для мультиюзера используй /login в боте. Этот скрипт — для отладки одного аккаунта."""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient


async def main() -> None:
    print("Для мультипользовательского режима используй /login в Telegram-боте.")
    print("Этот скрипт — ручная авторизация одной сессии.\n")

    api_id = input("api_id: ").strip()
    api_hash = input("api_hash: ").strip()
    if not api_id or not api_hash:
        sys.exit(1)

    client = TelegramClient("manual_session", int(api_id), api_hash)
    await client.start()
    me = await client.get_me()
    print(f"✅ {me.first_name} (@{me.username})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
