# @version=1.4.0
# @description Cursor AI агент из Telegram (cloud, изображения, AFK)
# @author giftbot
"""CursorAgent — Cursor SDK в Heroku / Hikka userbot.

Команды:
  .cursor <вопрос>  — запрос с контекстом чата
  .cursor img: ...  — генерация картинки
  .cursor ssh: ...  — команда на SSH-сервере
  .cursorimg        — картинка по описанию
  .cursorssh        — выполнить команду по SSH
  .cursorchat       — диалог с агентом (можно слать фото)
  .cursorstop       — завершить диалог
  .cursorwatch      — следить за чатом и предлагать помощь
  .cursorunwatch    — перестать следить
  .afkcursor        — AFK: ИИ-менеджер отвечает в личку за вас
  .afkcursor off    — выключить AFK
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse

from telethon.tl.custom import Message

logger = logging.getLogger(__name__)

# Heroku перезаписывает self._client Telegram-клиентом — храним мост SDK вне instance.
_cursor_sdk_bridge = None

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]

try:
    from . import CursorAiLib as _cursor_ai
except ImportError:
    try:
        from . import _cursor_ai as _cursor_ai  # legacy
    except ImportError:
        try:
            from . import cursor_ai as _cursor_ai  # legacy
        except ImportError:
            _cursor_ai = None  # type: ignore[assignment]

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

_AFK_HEADER = """Ты — ИИ-менеджер пользователя {owner_name}.
Пользователь сейчас недоступен (AFK / не в сети). Ты временно отвечаешь за него в личных сообщениях.

Изучи историю переписки ниже — пойми, кто собеседник, какой у вас стиль общения, о чём вы обычно говорите.

Правила ответа:
1. Кратко представься: ты ИИ-менеджер {owner_name}, пользователь сейчас не может ответить, поэтому отвечаешь ты.
2. Если на сообщение можно ответить по существу (приветствие, small talk, вопрос из контекста переписки) — ответь.
3. Если нужна личная информация только от пользователя или решение за него — вежливо скажи, что передашь, когда он вернётся.
4. Пиши по-русски, естественно, 1–4 предложения. Без markdown, без списков. Максимум 1 emoji.
5. Не выдумывай факты о пользователе — опирайся только на историю чата.
6. Подстраивай тон под собеседника и вашу переписку.

{context}

---
Новое сообщение от собеседника:
"""

_HELP_TRIGGERS = re.compile(
    r"(?:\?|помоги|помощь|не работает|ошибк|баг|как сделать|как настроить|"
    r"не получается|не могу|подскаж|help|how to|issue|problem|stuck)",
    re.IGNORECASE,
)

_IMG_PREFIX = re.compile(
    r"^(?:img|image|картинка|нарисуй|рисуй|draw)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_SSH_PREFIX = re.compile(
    r"^(?:ssh|сервер|server)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_route(prompt: str) -> tuple[str, str]:
    text = (prompt or "").strip()
    for pattern, kind in ((_IMG_PREFIX, "img"), (_SSH_PREFIX, "ssh")):
        match = pattern.match(text)
        if match:
            return kind, match.group(1).strip()
    return "ask", text


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
            "_cmd_doc_cursorimg": "Картинка по описанию — .cursorimg",
            "_cmd_doc_cursorssh": "Команда по SSH — .cursorssh",
            "_cmd_doc_afkcursor": "AFK: ИИ-менеджер отвечает в личку — .afkcursor",
            "no_key": (
                "🔑 <b>Нужен Cursor API key</b>\n\n"
                "1. <a href=\"https://cursor.com/dashboard/integrations\">Integrations</a>\n"
                "2. API Keys → Create → <code>crsr_...</code>\n"
                "3. <code>.cfg CursorAgent</code> → <code>cursor_api_key</code>"
            ),
            "thinking": "⏳ <i>Cursor анализирует чат...</i>",
            "chat_on": (
                "💬 <b>Диалог с Cursor</b>\n\n"
                "Пиши сообщения или присылай фото — учитываю контекст чата.\n"
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
                "<code>.addrepo https://raw.githubusercontent.com/DragMiro/giftbot/main</code>\n"
                "<code>.dlm CursorAiLib</code>\n"
                "<code>.dlm CursorAgent</code>\n"
                "<code>.restart -f</code>"
            ),
            "error": "❌ <b>Cursor</b>\n\n{}",
            "owner_only": "🔒 Только владелец userbot может это делать.",
            "img_generating": "🎨 <i>Генерирую картинку...</i>",
            "img_analyzing": "📷 <i>Анализирую изображение...</i>",
            "img_too_large": "❌ Изображение слишком большое (макс 4 МБ).",
            "img_no_openai": (
                "📷 Фото получено, но для анализа картинки нужен "
                "<code>openai_api_key</code> в <code>.cfg CursorAgent</code>."
            ),
            "img_caption": "🎨 <b>Картинка</b>\n\n💬 <b>Запрос</b>\n{query}",
            "ssh_running": "🖥 <i>Выполняю на сервере...</i>",
            "ssh_need_cfg": (
                "🖥 <b>SSH не настроен</b>\n\n"
                "<code>.cfg CursorAgent</code> →\n"
                "<code>ssh_enabled</code> = True\n"
                "<code>ssh_host</code>, <code>ssh_user</code>\n"
                "<code>ssh_password</code> или <code>ssh_key_path</code>"
            ),
            "no_paramiko": (
                "Нет <code>paramiko</code>.\n"
                "<code>.terminal pip install paramiko</code>"
            ),
            "afk_on": (
                "🌙 <b>AFK-режим включён</b>\n\n"
                "ИИ-менеджер отвечает в личку за вас, пока вы недоступны.\n"
                "Изучает историю переписки и отвечает от вашего имени.\n\n"
                "<code>.afkcursor off</code> — выключить"
            ),
            "afk_off": "☀️ AFK-режим выключен. ИИ-менеджер больше не отвечает в личку.",
            "afk_already": (
                "🌙 AFK уже включён.\n"
                "<code>.afkcursor off</code> — выключить"
            ),
        }

        strings_ru = strings.copy()

        def __init__(self) -> None:
            self._cursor_agents: dict = {}
            self._chat_users: set[int] = set()
            self._watched_chats: set[int] = set()
            self._proactive_at: dict[int, float] = {}
            self._afk_enabled: bool = False
            self._afk_at: dict[int, float] = {}
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
                loader.ConfigValue(
                    "afk_history_messages",
                    150,
                    lambda: "Сколько сообщений истории читать в AFK-режиме",
                    validator=loader.validators.Integer(minimum=100, maximum=200),
                ),
                loader.ConfigValue(
                    "afk_cooldown",
                    15,
                    lambda: "Пауза между AFK-ответами одному собеседнику (сек)",
                    validator=loader.validators.Integer(minimum=0),
                ),
                loader.ConfigValue(
                    "image_provider",
                    "pollinations",
                    lambda: "Генерация картинок: pollinations или openai",
                ),
                loader.ConfigValue(
                    "openai_api_key",
                    "",
                    lambda: "OpenAI API key для DALL-E (если provider=openai)",
                ),
                loader.ConfigValue(
                    "ssh_enabled",
                    False,
                    lambda: "Разрешить SSH-команды (.cursorssh / ssh:)",
                    validator=loader.validators.Boolean(),
                ),
                loader.ConfigValue(
                    "ssh_host",
                    "",
                    lambda: "SSH хост (IP или домен)",
                ),
                loader.ConfigValue(
                    "ssh_port",
                    22,
                    lambda: "SSH порт",
                    validator=loader.validators.Integer(minimum=1, maximum=65535),
                ),
                loader.ConfigValue(
                    "ssh_user",
                    "",
                    lambda: "SSH пользователь",
                ),
                loader.ConfigValue(
                    "ssh_password",
                    "",
                    lambda: "SSH пароль (или оставь пустым и укажи ssh_key_path)",
                    validator=loader.validators.Hidden(),
                ),
                loader.ConfigValue(
                    "ssh_key_path",
                    "",
                    lambda: "Путь к приватному SSH-ключу на сервере userbot",
                ),
            )

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            saved = self._db.get(self.strings("name"), "watched_chats", [])
            if isinstance(saved, list):
                self._watched_chats = {int(x) for x in saved}
            self._afk_enabled = bool(self._db.get(self.strings("name"), "afk_enabled", False))

        def _save_watched(self) -> None:
            self._db.set(self.strings("name"), "watched_chats", list(self._watched_chats))

        def _save_afk(self) -> None:
            self._db.set(self.strings("name"), "afk_enabled", self._afk_enabled)

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

        async def _recent_messages(self, message: Message, *, limit: int | None = None) -> list[str]:
            if limit is None:
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

        async def _build_afk_context(self, message: Message) -> str:
            try:
                meta = await self._describe_chat(message)
                limit = int(self.config["afk_history_messages"])
                history = await self._recent_messages(message, limit=limit)
            except Exception:
                logger.exception("afk context failed")
                return "Контекст чата недоступен."

            parts = ["=== Контекст Telegram ===", *meta]
            if history:
                parts.append(f"\n=== История переписки (последние {len(history)} сообщений) ===")
                parts.extend(history)
            return "\n".join(parts)

        def _wrap_afk_prompt(self, prompt: str, context: str, owner_name: str) -> str:
            return _AFK_HEADER.format(owner_name=owner_name, context=context) + prompt

        def _afk_cooldown_ok(self, chat_id: int) -> bool:
            cooldown = int(self.config["afk_cooldown"])
            if cooldown <= 0:
                return True
            last = self._afk_at.get(chat_id, 0.0)
            return time.time() - last >= cooldown

        def _should_afk_reply(self, message: Message) -> bool:
            if not self._afk_enabled:
                return False
            if getattr(message, "out", False):
                return False
            if message.sender_id == self.tg_id:
                return False
            text = (message.raw_text or "").strip()
            if text.startswith(".") or text.startswith("/"):
                return False
            if not text and not self._is_image_message(message):
                return False
            return True

        async def _reply_afk_plain(self, message: Message, text: str) -> None:
            body = (text or "").strip()
            if not body:
                return
            for chunk in _chunks(body, size=4000):
                await message.reply(chunk)

        async def _ask_afk(self, message: Message, prompt: str) -> None:
            try:
                cs = self._import_cursor_sdk()
            except ImportError:
                logger.warning("afk: cursor-sdk not installed")
                return

            if not self._api_key():
                logger.warning("afk: no api key")
                return

            try:
                me = await self.client.get_me()
                owner_name = _person_name(me)
                context = await self._build_afk_context(message)
                full_prompt = self._wrap_afk_prompt(prompt, context, owner_name)
                bridge = await self._ensure_bridge()
                result = await cs.AsyncAgent.prompt(
                    full_prompt,
                    self._cloud_options(),
                    client=bridge,
                )

                if result.status == "error":
                    logger.warning("afk: cursor run failed")
                    return

                text = (result.result or "").strip()
                if not text:
                    return

                await self._reply_afk_plain(message, text)
                self._afk_at[message.chat_id] = time.time()
            except Exception:
                logger.exception("afk reply failed")

        async def _ask_afk_with_media(self, message: Message, prompt: str) -> None:
            if not self._is_image_message(message):
                await self._ask_afk(message, prompt)
                return

            caption = (message.raw_text or "").strip()
            user_prompt = caption or prompt or "Собеседник прислал изображение."

            try:
                image_bytes, mime = await self._download_image(message)
                if len(image_bytes) > 4 * 1024 * 1024:
                    user_prompt = (
                        f"{user_prompt}\n\n"
                        "[Собеседник прислал изображение, но оно слишком большое для анализа. "
                        "Сообщи, что пользователь offline, и попроси описать или повторить позже.]"
                    )
                else:
                    openai_key = (self.config["openai_api_key"] or "").strip()
                    if openai_key and _cursor_ai:
                        vision_text = await _cursor_ai.analyze_image_openai(
                            image_bytes,
                            user_prompt,
                            api_key=openai_key,
                            mime=mime,
                        )
                        user_prompt = f"{user_prompt}\n\n[Содержимое изображения]\n{vision_text}"
                    else:
                        user_prompt = (
                            f"{user_prompt}\n\n"
                            "[Собеседник прислал изображение без подписи. "
                            "Сообщи, что пользователь offline, и что передашь, когда он вернётся.]"
                        )

                await self._ask_afk(message, user_prompt)
            except Exception:
                logger.exception("afk image failed")

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

        def _owner_only(self, message: Message) -> bool:
            return message.sender_id == self.tg_id

        @staticmethod
        def _is_image_message(message: Message) -> bool:
            if message.photo:
                return True
            doc = message.document
            mime = getattr(doc, "mime_type", None) or ""
            return bool(doc and mime.startswith("image/"))

        async def _download_image(self, message: Message) -> tuple[bytes, str]:
            if message.photo:
                data = await message.download_media(bytes)
                return data, "image/jpeg"
            doc = message.document
            data = await message.download_media(bytes)
            mime = getattr(doc, "mime_type", None) or "image/jpeg"
            return data, mime

        async def _ask_with_media(
            self,
            message: Message,
            prompt: str,
            *,
            chat: bool = False,
        ) -> None:
            if not self._is_image_message(message):
                await self._dispatch(message, prompt, chat=chat)
                return

            caption = (message.raw_text or "").strip()
            user_prompt = caption or prompt or "Опиши изображение и ответь по контексту чата."

            await utils.answer(message, self.strings("img_analyzing"))
            try:
                image_bytes, mime = await self._download_image(message)
                if len(image_bytes) > 4 * 1024 * 1024:
                    await utils.answer(message, self.strings("img_too_large"))
                    return

                openai_key = (self.config["openai_api_key"] or "").strip()
                if openai_key and _cursor_ai:
                    vision_text = await _cursor_ai.analyze_image_openai(
                        image_bytes,
                        user_prompt,
                        api_key=openai_key,
                        mime=mime,
                    )
                    enriched = f"{user_prompt}\n\n[Содержимое изображения]\n{vision_text}"
                else:
                    await utils.answer(message, self.strings("img_no_openai"))
                    enriched = (
                        f"{user_prompt}\n\n"
                        "[Пользователь прислал изображение. "
                        "Полный анализ недоступен без openai_api_key — ответь по подписи и контексту.]"
                    )

                await self._ask(message, enriched, chat=chat)
            except Exception as exc:
                logger.exception("image analyze failed")
                await utils.answer(message, self.strings("error").format(_escape(str(exc))))

        @staticmethod
        def _ssh_exec(
            host: str,
            port: int,
            user: str,
            password: str,
            key_path: str,
            command: str,
            timeout: int = 45,
        ) -> tuple[int, str, str]:
            import paramiko

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs: dict = {
                "hostname": host,
                "port": port,
                "username": user,
                "timeout": timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if key_path:
                kwargs["key_filename"] = key_path
            else:
                kwargs["password"] = password
            client.connect(**kwargs)
            try:
                _, stdout, stderr = client.exec_command(command, timeout=timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                code = stdout.channel.recv_exit_status()
                return code, out, err
            finally:
                client.close()

        async def _fetch_image_bytes(self, prompt: str) -> bytes:
            provider = (self.config["image_provider"] or "pollinations").strip().lower()

            if provider == "openai":
                import httpx

                key = (self.config["openai_api_key"] or "").strip()
                if not key:
                    raise RuntimeError("Нужен openai_api_key в .cfg CursorAgent")
                async with httpx.AsyncClient(timeout=120) as http:
                    resp = await http.post(
                        "https://api.openai.com/v1/images/generations",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": "dall-e-3",
                            "prompt": prompt,
                            "n": 1,
                            "size": "1024x1024",
                        },
                    )
                    resp.raise_for_status()
                    url = resp.json()["data"][0]["url"]
                    img = await http.get(url)
                    img.raise_for_status()
                    return img.content

            import httpx

            url = (
                "https://image.pollinations.ai/prompt/"
                + urllib.parse.quote(prompt)
                + "?width=1024&height=1024&nologo=true"
            )
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                return resp.content

        async def _cmd_image(self, message: Message, prompt: str) -> None:
            if not self._owner_only(message):
                await utils.answer(message, self.strings("owner_only"))
                return
            if not prompt:
                await utils.answer(
                    message,
                    "🎨 <b>Картинка</b>\n\n"
                    "<code>.cursorimg кот в космосе</code>\n"
                    "<code>.cursor img: закат над морем</code>\n"
                    "<code>.cursor нарисуй: пиксельный дракон</code>",
                )
                return

            await utils.answer(message, self.strings("img_generating"))
            try:
                data = await self._fetch_image_bytes(prompt)
                caption = self.strings("img_caption").format(query=_quote(prompt))
                await message.reply(file=data, message=caption)
            except Exception as exc:
                logger.exception("image generation failed")
                await utils.answer(message, self.strings("error").format(_escape(str(exc))))

        async def _cmd_ssh(self, message: Message, command: str) -> None:
            if not self._owner_only(message):
                await utils.answer(message, self.strings("owner_only"))
                return
            if not command:
                await utils.answer(
                    message,
                    "🖥 <b>SSH</b>\n\n"
                    "<code>.cursorssh ls -la</code>\n"
                    "<code>.cursor ssh: df -h</code>\n"
                    "<code>.cursor сервер: uptime</code>\n\n"
                    "Настройка: <code>.cfg CursorAgent</code>",
                )
                return
            if not self.config["ssh_enabled"]:
                await utils.answer(message, self.strings("ssh_need_cfg"))
                return

            host = (self.config["ssh_host"] or "").strip()
            user = (self.config["ssh_user"] or "").strip()
            if not host or not user:
                await utils.answer(message, self.strings("ssh_need_cfg"))
                return

            try:
                import paramiko  # noqa: F401
            except ImportError:
                await utils.answer(message, self.strings("no_paramiko"))
                return

            await utils.answer(message, self.strings("ssh_running"))
            try:
                port = int(self.config["ssh_port"] or 22)
                password = (self.config["ssh_password"] or "").strip()
                key_path = (self.config["ssh_key_path"] or "").strip()
                code, out, err = await utils.run_sync(
                    self._ssh_exec,
                    host,
                    port,
                    user,
                    password,
                    key_path,
                    command,
                )
                body = out.strip() or err.strip() or "(пустой вывод)"
                if code:
                    body = f"exit {code}\n{body}"
                reply = (
                    "🖥 <b>SSH</b>\n\n"
                    f"💬 <b>Команда</b>\n{_quote(command)}\n\n"
                    f"✨ <b>Вывод</b>\n{_quote(body, expandable=True)}"
                )
                for chunk in _chunks(reply):
                    await utils.answer(message, chunk)
            except Exception as exc:
                logger.exception("ssh failed")
                await utils.answer(message, self.strings("error").format(_escape(str(exc))))

        async def _dispatch(self, message: Message, prompt: str, *, chat: bool = False) -> None:
            kind, payload = _parse_route(prompt)
            if kind == "img":
                await self._cmd_image(message, payload)
            elif kind == "ssh":
                await self._cmd_ssh(message, payload)
            else:
                await self._ask(message, payload, chat=chat)

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
                        hint = f"{_escape(str(exc))} [CursorAgent v1.4.0]"
                        await utils.answer(message, self.strings("error").format(hint))
                else:
                    logger.exception("cursor proactive failed")
                return None

        @loader.command(ru_doc="Картинка по описанию — .cursorimg")
        async def cursorimgcmd(self, message: Message) -> None:
            """Картинка по описанию — .cursorimg"""
            await self._cmd_image(message, utils.get_args_raw(message))

        @loader.command(ru_doc="Команда по SSH — .cursorssh")
        async def cursorsshcmd(self, message: Message) -> None:
            """Команда по SSH — .cursorssh"""
            await self._cmd_ssh(message, utils.get_args_raw(message))

        @loader.command(ru_doc="Запрос к Cursor — .cursor <вопрос>")
        async def cursorcmd(self, message: Message) -> None:
            """Запрос к Cursor — .cursor <вопрос>"""
            prompt = utils.get_args_raw(message)
            if self._is_image_message(message):
                await self._ask_with_media(message, prompt, chat=False)
                return
            if not prompt:
                await utils.answer(
                    message,
                    "🤖 <b>Cursor</b> <i>v1.4.0</i>\n\n"
                    "▫️ <code>.cursor &lt;вопрос&gt;</code> — AI с контекстом чата\n"
                    "▫️ Отправь фото с подписью — анализ изображения\n"
                    "▫️ <code>.cursor img: описание</code> — картинка\n"
                    "▫️ <code>.cursor ssh: команда</code> — SSH на сервер\n"
                    "▫️ <code>.cursorimg</code> / <code>.cursorssh</code>\n"
                    "▫️ <code>.cursorchat</code> — диалог (можно слать фото)\n"
                    "▫️ <code>.cursorstop</code> — выход\n"
                    "▫️ <code>.cursorwatch</code> — следить за чатом\n"
                    "▫️ <code>.afkcursor</code> — AFK: ИИ-менеджер в личке\n\n"
                    "🔑 Cursor: <a href=\"https://cursor.com/dashboard/integrations\">Integrations</a>\n"
                    "📷 Vision: <code>openai_api_key</code> в <code>.cfg CursorAgent</code>\n"
                    "⚙️ <code>.cfg CursorAgent</code> — ключи, SSH, картинки",
                )
                return
            await self._dispatch(message, prompt)

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

        @loader.command(ru_doc="AFK: ИИ-менеджер отвечает в личку — .afkcursor")
        async def afkcursorcmd(self, message: Message) -> None:
            """AFK: ИИ-менеджер отвечает в личку — .afkcursor"""
            if not self._owner_only(message):
                await utils.answer(message, self.strings("owner_only"))
                return

            args = (utils.get_args_raw(message) or "").strip().lower()
            if args in ("off", "stop", "выкл", "стоп"):
                if not self._afk_enabled:
                    await utils.answer(message, self.strings("afk_off"))
                    return
                self._afk_enabled = False
                self._save_afk()
                await utils.answer(message, self.strings("afk_off"))
                return

            if not self._api_key():
                await utils.answer(message, self.strings("no_key"))
                return

            try:
                self._import_cursor_sdk()
            except ImportError:
                await utils.answer(message, self.strings("no_sdk"))
                return

            if self._afk_enabled:
                await utils.answer(message, self.strings("afk_already"))
                return

            self._afk_enabled = True
            self._save_afk()
            await utils.answer(message, self.strings("afk_on"))

        @loader.watcher(
            incoming=True,
            func=lambda m: m.is_private and not getattr(m, "out", False),
        )
        async def cursor_afk_watcher(self, message: Message) -> None:
            if not self._should_afk_reply(message):
                return
            if not self._afk_cooldown_ok(message.chat_id):
                return
            raw = (message.raw_text or "").strip()
            if self._is_image_message(message):
                await self._ask_afk_with_media(message, raw)
                return
            await self._ask_afk(message, raw)

        @loader.watcher(
            incoming=True,
            func=lambda m: m.is_private and not getattr(m, "out", False),
        )
        async def cursor_watcher(self, message: Message) -> None:
            uid = message.sender_id
            if uid not in self._chat_users:
                return
            raw = (message.raw_text or "").strip()
            if self._is_image_message(message):
                await self._ask_with_media(message, raw, chat=True)
                return
            if not raw or raw.startswith("."):
                return
            await self._dispatch(message, raw, chat=True)

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
