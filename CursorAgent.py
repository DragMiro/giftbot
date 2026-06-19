# @version=1.0.0
# @description Cursor AI агент из Telegram (cloud)
# @author giftbot
# requires: cursor-sdk>=0.1.0
"""CursorAgent — Cursor SDK в Heroku / Hikka userbot.

Команды:
  .cursor <вопрос>  — один запрос к агенту
  .cursorchat       — диалог с агентом
  .cursorstop       — завершить диалог
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon.tl.custom import Message

logger = logging.getLogger(__name__)

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from cursor_sdk import AsyncAgent, AsyncClient


def _chunks(text: str, size: int = 3900) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:size])
        text = text[size:]
    return parts


if loader:

    class CursorAgentMod(loader.Module):
        """🤖 Cursor AI из Telegram"""

        strings = {
            "name": "CursorAgent",
            "_cls_doc": "🤖 Cursor AI из Telegram",
            "_cmd_doc_cursor": "Запрос к Cursor — .cursor <вопрос>",
            "_cmd_doc_cursorchat": "Начать диалог — .cursorchat",
            "_cmd_doc_cursorstop": "Завершить диалог — .cursorstop",
            "no_key": "Укажи Cursor API key в .cfg CursorAgent → cursor_api_key",
            "thinking": "⏳ Cursor думает...",
            "chat_on": "💬 Диалог с Cursor начат. Пиши сообщения, .cursorstop — выход.",
            "chat_off": "Диалог завершён.",
            "no_chat": "Сначала .cursorchat",
            "error": "❌ Cursor: {}",
        }

        strings_ru = strings.copy()

        def __init__(self) -> None:
            self._client: AsyncClient | None = None
            self._agents: dict[int, AsyncAgent] = {}
            self._chat_users: set[int] = set()
            self.config = loader.ModuleConfig(
                loader.ConfigValue(
                    "cursor_api_key",
                    "",
                    validator=loader.validators.Hidden(),
                    doc="Cursor API key (Dashboard → Integrations)",
                ),
                loader.ConfigValue(
                    "model",
                    "composer-2.5",
                    lambda v: str(v).strip() or "composer-2.5",
                    doc="Модель Cursor",
                ),
                loader.ConfigValue(
                    "repo_url",
                    "https://github.com/DragMiro/giftbot",
                    lambda v: str(v).strip(),
                    doc="GitHub-репозиторий для cloud-агента",
                ),
                loader.ConfigValue(
                    "repo_branch",
                    "main",
                    lambda v: str(v).strip() or "main",
                    doc="Ветка репозитория",
                ),
            )

        def _api_key(self) -> str:
            key = (self.config["cursor_api_key"] or "").strip()
            if key:
                return key
            import os

            return (os.environ.get("CURSOR_API_KEY") or "").strip()

        def _cloud_options(self):
            from cursor_sdk import AgentOptions, CloudAgentOptions, CloudRepository

            return AgentOptions(
                api_key=self._api_key(),
                model=self.config["model"],
                cloud=CloudAgentOptions(
                    repos=[
                        CloudRepository(
                            url=self.config["repo_url"],
                            starting_ref=self.config["repo_branch"],
                        )
                    ],
                    skip_reviewer_request=True,
                ),
            )

        async def _ensure_client(self) -> AsyncClient:
            from cursor_sdk import AsyncClient

            if self._client is None:
                self._client = await AsyncClient.launch_bridge()
            return self._client

        async def _get_agent(self, uid: int) -> AsyncAgent:
            if uid in self._agents:
                return self._agents[uid]

            client = await self._ensure_client()
            agent = await client.create_agent(self._cloud_options())
            self._agents[uid] = agent
            return agent

        async def _close_agent(self, uid: int) -> None:
            agent = self._agents.pop(uid, None)
            self._chat_users.discard(uid)
            if agent is not None:
                await agent.close()

        async def _reply_text(self, message: Message, text: str) -> None:
            for chunk in _chunks(text):
                await utils.answer(message, chunk)

        async def _ask(self, message: Message, prompt: str, *, chat: bool = False) -> None:
            if not self._api_key():
                await utils.answer(message, self.strings("no_key"))
                return

            await utils.answer(message, self.strings("thinking"))

            try:
                from cursor_sdk import AsyncAgent, CursorAgentError

                client = await self._ensure_client()

                if chat:
                    agent = await self._get_agent(message.sender_id)
                    run = await agent.send(prompt)
                    result = await run.wait()
                else:
                    result = await AsyncAgent.prompt(
                        prompt,
                        self._cloud_options(),
                        client=client,
                    )

                if result.status == "error":
                    await utils.answer(message, self.strings("error").format("run failed"))
                    return

                text = (result.result or "").strip() or "(пустой ответ)"
                await self._reply_text(message, text)
            except Exception as exc:
                name = type(exc).__name__
                if name == "CursorAgentError":
                    await utils.answer(message, self.strings("error").format(exc))
                else:
                    logger.exception("cursor ask failed")
                    await utils.answer(message, self.strings("error").format(exc))

        @loader.command(ru_doc="Запрос к Cursor — .cursor <вопрос>")
        async def cursorcmd(self, message: Message) -> None:
            """Запрос к Cursor — .cursor <вопрос>"""
            prompt = utils.get_args_raw(message)
            if not prompt:
                await utils.answer(
                    message,
                    "<b>Cursor</b>\n"
                    ".cursor &lt;вопрос&gt; — один запрос\n"
                    ".cursorchat — диалог\n"
                    ".cursorstop — выход из диалога",
                )
                return
            await self._ask(message, prompt)

        @loader.command(ru_doc="Начать диалог — .cursorchat")
        async def cursorchatcmd(self, message: Message) -> None:
            """Начать диалог — .cursorchat"""
            if not self._api_key():
                await utils.answer(message, self.strings("no_key"))
                return
            uid = message.sender_id
            await self._close_agent(uid)
            await self._get_agent(uid)
            self._chat_users.add(uid)
            await utils.answer(message, self.strings("chat_on"))

        @loader.command(ru_doc="Завершить диалог — .cursorstop")
        async def cursorstopcmd(self, message: Message) -> None:
            """Завершить диалог — .cursorstop"""
            await self._close_agent(message.sender_id)
            await utils.answer(message, self.strings("chat_off"))

        @loader.watcher(
            incoming=True,
            func=lambda m: m.is_private and not getattr(m, "out", False),
        )
        async def cursor_watcher(self, message: Message) -> None:
            uid = message.sender_id
            if uid not in self._chat_users:
                return
            raw = (message.raw_text or "").strip()
            if not raw or raw.startswith("."):
                return
            await self._ask(message, raw, chat=True)

else:
    CursorAgentMod = None  # type: ignore[misc, assignment]
