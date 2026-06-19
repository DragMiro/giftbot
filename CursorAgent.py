# @version=1.1.2
# @description Cursor AI агент из Telegram (cloud)
# @author giftbot
"""CursorAgent — Cursor SDK в Heroku / Hikka userbot.

Команды:
  .cursor <вопрос>  — запрос с контекстом чата
  .cursorchat       — диалог с агентом
  .cursorstop       — завершить диалог
  .cursorwatch      — следить за чатом и предлагать помощь
  .cursorunwatch    — перестать следить
"""

from __future__ import annotations

import html
import logging
import re
import time

from telethon.tl.custom import Message

logger = logging.getLogger(__name__)

# Heroku перезаписывает self._client Telegram-клиентом — храним мост SDK вне instance.
_cursor_sdk_bridge = None

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]

_CONTEXT_HEADER = """Ты — умный помощник в Telegram userbot.
Отвечай по-русски, кратко и по делу. Учитывай контекст чата: кто пишет, где, о чём разговор.

{context}

---
Запрос пользователя:
"""

_PROACTIVE_HEADER = """Ты наблюдаешь за Telegram-чатом от имени владельца userbot.
Изучи контекст. Если уместно мягко предложить помощь — напиши 1–3 коротких предложения.
Если вмешиваться не стоит — ответь ровно: SKIP

Не используй markdown. Можно 1 emoji в начале.

{context}

---
Триггерное сообщение:
"""

_HELP_TRIGGERS = re.compile(
    r"(?:\?|помоги|помощь|не работает|ошибк|баг|как сделать|как настроить|"
    r"не получается|не могу|подскаж|help|how to|issue|problem|stuck)",
    re.IGNORECASE,
)


def _chunks(text: str, size: int = 3900) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:size])
        text = text[size:]
    return parts


def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def _format_inline(text: str) -> str:
    text = _escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    return text


def _quote(text: str, *, expandable: bool = False) -> str:
    body = _escape((text or "").strip())
    if not body:
        body = "…"
    if expandable or len(body) > 280:
        return f"<blockquote expandable>{body}</blockquote>"
    return f"<blockquote>{body}</blockquote>"


def _format_answer_body(text: str) -> str:
    raw = (text or "").strip() or "(пустой ответ)"
    parts: list[str] = []
    blocks = re.split(r"```(?:\w+)?\n?", raw)
    for idx, block in enumerate(blocks):
        block = block.strip()
        if not block:
            continue
        if idx % 2 == 1:
            parts.append(f"<pre>{_escape(block)}</pre>")
        else:
            for para in re.split(r"\n{2,}", block):
                para = para.strip()
                if not para:
                    continue
                lines = [_format_inline(line) for line in para.split("\n")]
                parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _format_cursor_reply(
    text: str,
    *,
    model: str,
    query: str | None = None,
    proactive: bool = False,
) -> str:
    parts: list[str] = []

    if proactive:
        parts.append("💡 <b>Подсказка</b>")
        if query:
            parts.append("💬 <b>Сообщение</b>")
            parts.append(_quote(query))
        parts.append("✨ <b>Предложение</b>")
    else:
        parts.append("🤖 <b>Cursor</b>")
        if query:
            parts.append("💬 <b>Запрос</b>")
            parts.append(_quote(query))
        parts.append("✨ <b>Ответ</b>")

    answer = _format_answer_body(text)
    if len((text or "").strip()) > 280:
        parts.append(f"<blockquote expandable>{answer}</blockquote>")
    else:
        parts.append(f"<blockquote>{answer}</blockquote>")

    parts.append(f"<i>— {_escape(model)}</i>")
    return "\n\n".join(parts)


def _person_name(entity) -> str:
    if entity is None:
        return "неизвестно"
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    username = getattr(entity, "username", None)
    if username:
        return f"{name or username} (@{username})"
    title = getattr(entity, "title", None)
    if title:
        return title
    return name or f"id:{getattr(entity, 'id', '?')}"


if loader:

    class CursorAgentMod(loader.Module):
        """🤖 Cursor AI из Telegram"""

        strings = {
            "name": "CursorAgent",
            "_cls_doc": "🤖 Cursor AI из Telegram",
            "_cmd_doc_cursor": "Запрос к Cursor — .cursor <вопрос>",
            "_cmd_doc_cursorchat": "Начать диалог — .cursorchat",
            "_cmd_doc_cursorstop": "Завершить диалог — .cursorstop",
            "_cmd_doc_cursorwatch": "Следить за чатом — .cursorwatch",
            "_cmd_doc_cursorunwatch": "Не следить — .cursorunwatch",
            "no_key": (
                "🔑 <b>Нужен Cursor API key</b>\n\n"
                "1. <a href=\"https://cursor.com/dashboard/integrations\">Integrations</a>\n"
                "2. API Keys → Create → <code>crsr_...</code>\n"
                "3. <code>.cfg CursorAgent</code> → <code>cursor_api_key</code>"
            ),
            "thinking": "⏳ <i>Cursor анализирует чат...</i>",
            "chat_on": (
                "💬 <b>Диалог с Cursor</b>\n\n"
                "Пиши сообщения — я учитываю контекст чата.\n"
                "<code>.cursorstop</code> — выход"
            ),
            "chat_off": "✅ Диалог завершён.",
            "no_chat": "Сначала <code>.cursorchat</code>",
            "watch_on": (
                "👁 <b>Наблюдение включено</b>\n\n"
                "Буду следить за чатом и предлагать помощь, когда уместно.\n"
                "<code>.cursorunwatch</code> — выключить"
            ),
            "watch_off": "👁 Наблюдение за этим чатом выключено.",
            "watch_list_empty": "Нет чатов под наблюдением. Включи: <code>.cursorwatch</code>",
            "no_sdk": (
                "Нет пакета <code>cursor-sdk</code>.\n"
                "В <code>.terminal</code>:\n"
                "<code>pip install cursor-sdk</code>\n"
                "Потом <code>.restart -f</code>"
            ),
            "load_hint": (
                "Старая версия? Сначала:\n"
                "<code>.ulm CursorAgent</code>\n"
                "<code>rm -f ~/.heroku/modules_cache/*.py</code>\n"
                "<code>.dlm https://raw.githubusercontent.com/DragMiro/giftbot/main/CursorAgent.py</code>"
            ),
            "error": "❌ <b>Cursor</b>\n\n{}",
        }

        strings_ru = strings.copy()

        def __init__(self) -> None:
            self._cursor_agents: dict = {}
            self._chat_users: set[int] = set()
            self._watched_chats: set[int] = set()
            self._proactive_at: dict[int, float] = {}
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
                loader.ConfigValue(
                    "context_messages",
                    20,
                    lambda: "Сколько последних сообщений читать для контекста",
                    validator=loader.validators.Integer(minimum=5, maximum=50),
                ),
                loader.ConfigValue(
                    "proactive_enabled",
                    True,
                    lambda: "Предлагать помощь в чатах под наблюдением",
                    validator=loader.validators.Boolean(),
                ),
                loader.ConfigValue(
                    "proactive_cooldown",
                    300,
                    lambda: "Пауза между подсказками в одном чате (сек)",
                    validator=loader.validators.Integer(minimum=60),
                ),
            )

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            saved = self._db.get(self.strings("name"), "watched_chats", [])
            if isinstance(saved, list):
                self._watched_chats = {int(x) for x in saved}

        def _save_watched(self) -> None:
            self._db.set(self.strings("name"), "watched_chats", list(self._watched_chats))

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

        def _model(self) -> str:
            return (self.config["model"] or "composer-2.5").strip()

        def _cloud_options(self):
            cs = self._import_cursor_sdk()
            return cs.AgentOptions(
                api_key=self._api_key(),
                model=self._model(),
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

        async def _reply_text(
            self,
            message: Message,
            text: str,
            *,
            query: str | None = None,
            proactive: bool = False,
        ) -> None:
            body = _format_cursor_reply(
                text,
                model=self._model(),
                query=query,
                proactive=proactive,
            )
            for chunk in _chunks(body):
                await utils.answer(message, chunk)

        async def _describe_chat(self, message: Message) -> list[str]:
            lines: list[str] = []
            chat = await message.get_chat()
            sender = await message.get_sender()

            if message.is_private:
                lines.append("📍 Тип: личная переписка")
                lines.append(f"👤 Собеседник: {_person_name(sender)}")
            elif getattr(message, "is_group", False):
                lines.append("📍 Тип: группа")
                lines.append(f"💬 Чат: {_person_name(chat)}")
                if getattr(chat, "participants_count", None):
                    lines.append(f"👥 Участников: {chat.participants_count}")
            elif getattr(message, "is_channel", False):
                lines.append("📍 Тип: канал")
                lines.append(f"📢 Канал: {_person_name(chat)}")
            else:
                lines.append(f"📍 Чат id: {message.chat_id}")

            me = await self.client.get_me()
            lines.append(f"🤖 Userbot: {_person_name(me)}")
            lines.append(f"✍️ Автор запроса: {_person_name(sender)}")
            return lines

        async def _recent_messages(self, message: Message) -> list[str]:
            limit = int(self.config["context_messages"])
            rows: list[str] = []
            async for msg in self.client.iter_messages(message.chat_id, limit=limit):
                text = (msg.raw_text or msg.message or "").strip()
                if not text:
                    continue
                author = await msg.get_sender()
                who = _person_name(author)
                stamp = msg.date.strftime("%H:%M") if msg.date else "??:??"
                mark = " ← текущее" if msg.id == message.id else ""
                rows.append(f"[{stamp}] {who}: {text[:500]}{mark}")
            rows.reverse()
            return rows

        async def _build_context(self, message: Message) -> str:
            try:
                meta = await self._describe_chat(message)
                history = await self._recent_messages(message)
            except Exception:
                logger.exception("chat context failed")
                return "Контекст чата недоступен."

            parts = ["=== Контекст Telegram ===", *meta]
            if history:
                parts.append("\n=== Последние сообщения ===")
                parts.extend(history)
            return "\n".join(parts)

        def _wrap_prompt(self, prompt: str, context: str, *, proactive: bool = False) -> str:
            header = _PROACTIVE_HEADER if proactive else _CONTEXT_HEADER
            return header.format(context=context) + prompt

        def _should_offer_help(self, message: Message) -> bool:
            if getattr(message, "out", False):
                return False
            if message.sender_id == self.tg_id:
                return False
            text = (message.raw_text or "").strip()
            if len(text) < 8:
                return False
            if text.startswith(".") or text.startswith("/"):
                return False
            return bool(_HELP_TRIGGERS.search(text))

        def _cooldown_ok(self, chat_id: int) -> bool:
            cooldown = int(self.config["proactive_cooldown"])
            last = self._proactive_at.get(chat_id, 0.0)
            return time.time() - last >= cooldown

        async def _ask(
            self,
            message: Message,
            prompt: str,
            *,
            chat: bool = False,
            proactive: bool = False,
            with_context: bool = True,
        ) -> str | None:
            try:
                cs = self._import_cursor_sdk()
            except ImportError:
                await utils.answer(message, self.strings("no_sdk"))
                return None

            if not self._api_key():
                await utils.answer(message, self.strings("no_key"))
                return None

            if not proactive:
                await utils.answer(message, self.strings("thinking"))

            try:
                context = await self._build_context(message) if with_context else ""
                full_prompt = (
                    self._wrap_prompt(prompt, context, proactive=proactive)
                    if with_context
                    else prompt
                )
                bridge = await self._ensure_bridge()

                if chat:
                    agent = await self._get_agent(message.sender_id)
                    run = await agent.send(full_prompt)
                    result = await run.wait()
                else:
                    result = await cs.AsyncAgent.prompt(
                        full_prompt,
                        self._cloud_options(),
                        client=bridge,
                    )

                if result.status == "error":
                    if not proactive:
                        await utils.answer(message, self.strings("error").format("run failed"))
                    return None

                text = (result.result or "").strip()
                if proactive:
                    if not text or text.upper() == "SKIP":
                        return None
                    await self._reply_text(message, text, query=prompt, proactive=True)
                    self._proactive_at[message.chat_id] = time.time()
                    return text

                await self._reply_text(message, text or "(пустой ответ)", query=prompt)
                return text
            except Exception as exc:
                name = type(exc).__name__
                if not proactive:
                    if name == "CursorAgentError":
                        await utils.answer(message, self.strings("error").format(_escape(str(exc))))
                    else:
                        logger.exception("cursor ask failed")
                        hint = f"{_escape(str(exc))} [CursorAgent v1.1.2]"
                        await utils.answer(message, self.strings("error").format(hint))
                else:
                    logger.exception("cursor proactive failed")
                return None

        @loader.command(ru_doc="Запрос к Cursor — .cursor <вопрос>")
        async def cursorcmd(self, message: Message) -> None:
            """Запрос к Cursor — .cursor <вопрос>"""
            prompt = utils.get_args_raw(message)
            if not prompt:
                await utils.answer(
                    message,
                    "🤖 <b>Cursor</b> <i>v1.1.2</i>\n\n"
                    "▫️ <code>.cursor &lt;вопрос&gt;</code> — запрос с контекстом чата\n"
                    "▫️ <code>.cursorchat</code> — диалог\n"
                    "▫️ <code>.cursorstop</code> — выход из диалога\n"
                    "▫️ <code>.cursorwatch</code> — следить за чатом\n"
                    "▫️ <code>.cursorunwatch</code> — не следить\n"
                    "▫️ <code>.cursorwatchlist</code> — список чатов\n\n"
                    "🔑 Ключ: <a href=\"https://cursor.com/dashboard/integrations\">Integrations</a> "
                    "→ <code>crsr_...</code>\n"
                    "⚙️ <code>.cfg CursorAgent</code>",
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

        @loader.command(ru_doc="Следить за чатом — .cursorwatch")
        async def cursorwatchcmd(self, message: Message) -> None:
            """Следить за чатом — .cursorwatch"""
            if message.is_private:
                await utils.answer(
                    message,
                    "👁 Наблюдение для <b>групп и каналов</b>.\n"
                    "Выполни команду прямо в нужном чате.",
                )
                return
            self._watched_chats.add(message.chat_id)
            self._save_watched()
            await utils.answer(message, self.strings("watch_on"))

        @loader.command(ru_doc="Не следить — .cursorunwatch")
        async def cursorunwatchcmd(self, message: Message) -> None:
            """Не следить — .cursorunwatch"""
            self._watched_chats.discard(message.chat_id)
            self._save_watched()
            await utils.answer(message, self.strings("watch_off"))

        @loader.command(ru_doc="Список чатов под наблюдением")
        async def cursorwatchlistcmd(self, message: Message) -> None:
            """Список чатов под наблюдением"""
            if not self._watched_chats:
                await utils.answer(message, self.strings("watch_list_empty"))
                return
            lines = ["👁 <b>Чаты под наблюдением</b>\n"]
            for chat_id in sorted(self._watched_chats):
                try:
                    entity = await self.client.get_entity(chat_id)
                    lines.append(f"▫️ {_escape(_person_name(entity))} <code>{chat_id}</code>")
                except Exception:
                    lines.append(f"▫️ <code>{chat_id}</code>")
            await utils.answer(message, "\n".join(lines))

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

        @loader.watcher(
            incoming=True,
            func=lambda m: not m.is_private and not getattr(m, "out", False),
        )
        async def cursor_proactive_watcher(self, message: Message) -> None:
            if not self.config["proactive_enabled"]:
                return
            if message.chat_id not in self._watched_chats:
                return
            if not self._api_key():
                return
            if not self._should_offer_help(message):
                return
            if not self._cooldown_ok(message.chat_id):
                return
            text = (message.raw_text or "").strip()
            await self._ask(message, text, proactive=True)

else:
    CursorAgentMod = None  # type: ignore[misc, assignment]
