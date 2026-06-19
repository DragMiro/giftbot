# @version=1.0.5
# @description Cursor AI агент из Telegram (cloud)
# @author giftbot
"""CursorAgent — Cursor SDK в Heroku / Hikka userbot.

Команды:
  .cursor <вопрос>  — один запрос к агенту
  .cursorchat       — диалог с агентом
  .cursorstop       — завершить диалог
"""

from __future__ import annotations

import logging

from telethon.tl.custom import Message

logger = logging.getLogger(__name__)

# Heroku перезаписывает self._client Telegram-клиентом — храним мост SDK вне instance.
_cursor_sdk_bridge = None

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]


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
            "no_key": (
                "Нужен Cursor API key.\n\n"
                "1. <a href=\"https://cursor.com/dashboard/integrations\">cursor.com/dashboard/integrations</a>\n"
                "2. API Keys → Create → скопируй <code>crsr_...</code>\n"
                "3. <code>.cfg CursorAgent</code> → <code>cursor_api_key</code>"
            ),
            "thinking": "⏳ Cursor думает...",
            "chat_on": "💬 Диалог с Cursor начат. Пиши сообщения, .cursorstop — выход.",
            "chat_off": "Диалог завершён.",
            "no_chat": "Сначала .cursorchat",
            "no_sdk": (
                "Нет пакета <code>cursor-sdk</code>.\n"
                "В <code>.terminal</code>:\n"
                "<code>pip install cursor-sdk</code>\n"
                "Потом <code>.restart -f</code>"
            ),
            "load_hint": (
                "Если модуль не грузится — очисти кэш:\n"
                "<code>rm -f ~/.heroku/modules_cache/*.py</code>"
            ),
            "error": "❌ Cursor: {}",
        }

        strings_ru = strings.copy()

        def __init__(self) -> None:
            self._cursor_agents: dict = {}
            self._chat_users: set[int] = set()
            self.config = loader.ModuleConfig(
                loader.ConfigValue(
                    "cursor_api_key",
                    "",
                    lambda: "Cursor API key (crsr_... из Integrations)",
                ),
                loader.ConfigValue(
                    "model",
                    "composer-2.5",
                    lambda: "Модель Cursor",
                ),
                loader.ConfigValue(
                    "repo_url",
                    "https://github.com/DragMiro/giftbot",
                    lambda: "GitHub-репозиторий для cloud-агента",
                ),
                loader.ConfigValue(
                    "repo_branch",
                    "main",
                    lambda: "Ветка репозитория",
                ),
            )

        @staticmethod
        def _import_cursor_sdk():
            try:
                import cursor_sdk

                return cursor_sdk
            except ImportError as exc:
                raise ImportError("cursor-sdk") from exc

        def _api_key(self) -> str:
            key = (self.config["cursor_api_key"] or "").strip()
            if key:
                return key
            import os

            return (os.environ.get("CURSOR_API_KEY") or "").strip()

        def _cloud_options(self):
            cs = self._import_cursor_sdk()
            return cs.AgentOptions(
                api_key=self._api_key(),
                model=(self.config["model"] or "composer-2.5").strip(),
                cloud=cs.CloudAgentOptions(
                    repos=[
                        cs.CloudRepository(
                            url=(self.config["repo_url"] or "").strip(),
                            starting_ref=(self.config["repo_branch"] or "main").strip(),
                        )
                    ],
                    skip_reviewer_request=True,
                ),
            )

        async def _ensure_bridge(self):
            global _cursor_sdk_bridge
            cs = self._import_cursor_sdk()
            bridge = _cursor_sdk_bridge
            if bridge is None or not hasattr(bridge, "create_agent"):
                bridge = await cs.AsyncClient.launch_bridge()
                _cursor_sdk_bridge = bridge
            return bridge

        async def _get_agent(self, uid: int):
            if uid in self._cursor_agents:
                return self._cursor_agents[uid]

            bridge = await self._ensure_bridge()
            agent = await bridge.create_agent(self._cloud_options())
            self._cursor_agents[uid] = agent
            return agent

        async def _close_agent(self, uid: int) -> None:
            agent = self._cursor_agents.pop(uid, None)
            self._chat_users.discard(uid)
            if agent is not None:
                await agent.close()

        async def _reply_text(self, message: Message, text: str) -> None:
            for chunk in _chunks(text):
                await utils.answer(message, chunk)

        async def _ask(self, message: Message, prompt: str, *, chat: bool = False) -> None:
            try:
                cs = self._import_cursor_sdk()
            except ImportError:
                await utils.answer(message, self.strings("no_sdk"))
                return

            if not self._api_key():
                await utils.answer(message, self.strings("no_key"))
                return

            await utils.answer(message, self.strings("thinking"))

            try:
                bridge = await self._ensure_bridge()

                if chat:
                    agent = await self._get_agent(message.sender_id)
                    run = await agent.send(prompt)
                    result = await run.wait()
                else:
                    result = await cs.AsyncAgent.prompt(
                        prompt,
                        self._cloud_options(),
                        client=bridge,
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
                    hint = f"{exc} [CursorAgent v1.0.5]"
                    await utils.answer(message, self.strings("error").format(hint))

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
                    ".cursorstop — выход\n\n"
                    "Ключ: <a href=\"https://cursor.com/dashboard/integrations\">Integrations</a> "
                    "→ API Keys → <code>crsr_...</code>\n"
                    "Вставить: <code>.cfg CursorAgent</code> → <code>cursor_api_key</code>",
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
