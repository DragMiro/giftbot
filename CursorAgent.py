# @version=1.5.1
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

import asyncio
import html
import json
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

_GUARDIAN_ERROR_HEADER = """Ты — страж модуля CursorAgent (Telegram userbot на Hikka/Heroku).
Произошла ошибка. Проанализируй и верни ТОЛЬКО валидный JSON без markdown:
{{"action":"retry"|"reset_bridge"|"disable_afk"|"pause_afk"|"alert_only"|"ignore","reason":"кратко","owner_message":"сообщение владельцу на русском"}}

Доступные action:
- retry — безопасно повторить запрос (1 раз)
- reset_bridge — сбросить мост Cursor SDK
- disable_afk — выключить AFK-режим
- pause_afk — временно остановить AFK-ответы
- alert_only — только уведомить владельца, не чинить самому
- ignore — ничего не делать

Контекст ошибки:
{context}
"""

_GUARDIAN_ANOMALY_HEADER = """Ты — страж модуля CursorAgent (Telegram userbot).
Обнаружена подозрительная активность. Верни ТОЛЬКО валидный JSON без markdown:
{{"proceed":true|false,"reason":"кратко","owner_message":"описание странности для владельца на русском"}}

proceed=false если активность похожа на спам, утечку токенов, зацикливание или атаку.
proceed=true только если это нормальная нагрузка.

Детали:
{context}
"""

_YES_RE = re.compile(r"^(?:да|yes|y|\+|ok|ок|выполн|продолж|разреши)\b", re.IGNORECASE)
_NO_RE = re.compile(r"^(?:нет|no|n|\-|стоп|stop|отмен|запрет|не\s*выполн)\b", re.IGNORECASE)

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


def _unwrap_message(message):
    """Heroku watcher иногда передаёт Event, а не Message."""
    inner = getattr(message, "message", None)
    if inner is not None and inner is not message and hasattr(inner, "id"):
        return inner
    return message


def _msg_chat_id(message) -> int:
    msg = _unwrap_message(message)
    if utils:
        try:
            return utils.get_chat_id(msg)
        except Exception:
            pass
    cid = getattr(msg, "chat_id", None)
    if cid:
        return cid
    chat = getattr(msg, "chat", None)
    if chat is not None:
        return getattr(chat, "id", 0)
    return getattr(message, "chat_id", 0) or 0


def _msg_sender_id(message) -> int:
    msg = _unwrap_message(message)
    for obj in (msg, message):
        sid = getattr(obj, "sender_id", None)
        if sid:
            return sid
    if _msg_is_private(message):
        cid = _msg_chat_id(message)
        if cid:
            return cid
    for obj in (msg, message):
        from_id = getattr(obj, "from_id", None)
        if from_id is not None:
            uid = getattr(from_id, "user_id", None)
            if uid:
                return uid
        peer = getattr(obj, "peer_id", None)
        if peer is not None:
            uid = getattr(peer, "user_id", None)
            if uid:
                return uid
    return 0


def _msg_is_private(message) -> bool:
    msg = _unwrap_message(message)
    for obj in (msg, message):
        val = getattr(obj, "is_private", None)
        if val is not None:
            return bool(val)
        val = getattr(obj, "private", None)
        if val is not None:
            return bool(val)
    peer = getattr(msg, "peer_id", None) or getattr(message, "peer_id", None)
    if peer is not None:
        return bool(getattr(peer, "user_id", None))
    return False


def _watcher_incoming(message) -> bool:
    msg = _unwrap_message(message)
    return not getattr(msg, "out", False)


def _watcher_private(message) -> bool:
    msg = _unwrap_message(message)
    return _msg_is_private(message) and not getattr(msg, "out", False)


def _watcher_group(message) -> bool:
    msg = _unwrap_message(message)
    return not _msg_is_private(message) and not getattr(msg, "out", False)


def _watcher_owner_out(message) -> bool:
    msg = _unwrap_message(message)
    return bool(getattr(msg, "out", False))


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
                "В Telegram (не через <code>.terminal</code>):\n"
                "<code>.pip install cursor-sdk</code>\n"
                "Потом <code>.restart -f</code>\n\n"
                "Если <code>.pip</code> недоступен:\n"
                "<code>.terminal pip install cursor-sdk --break-system-packages</code>"
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
                "ИИ-менеджер отвечает только в личку и только на сообщения, "
                "пришедшие после включения AFK.\n"
                "Старые сообщения и групповые чаты игнорируются.\n\n"
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
            self._chat_locks: dict[int, asyncio.Lock] = {}
            self._watched_chats: set[int] = set()
            self._proactive_at: dict[int, float] = {}
            self._afk_enabled: bool = False
            self._afk_enabled_at: float = 0.0
            self._afk_at: dict[int, float] = {}
            self._afk_blocked: bool = False
            self._outgoing_log: list[tuple[float, str]] = []
            self._guardian_pending: dict[int, dict] = {}
            self._guardian_last_alert: float = 0.0
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
                loader.ConfigValue(
                    "guardian_enabled",
                    True,
                    lambda: "Страж: автофикс ошибок и алерты о подозрительной активности",
                    validator=loader.validators.Boolean(),
                ),
                loader.ConfigValue(
                    "guardian_chat_id",
                    -5475928034,
                    lambda: "ID группы для алертов стража",
                    validator=loader.validators.Integer(),
                ),
                loader.ConfigValue(
                    "guardian_owner_id",
                    432157779,
                    lambda: "ID владельца (кто подтверждает да/нет)",
                    validator=loader.validators.Integer(),
                ),
                loader.ConfigValue(
                    "guardian_max_outgoing",
                    8,
                    lambda: "Макс. исходящих сообщений модуля за окно",
                    validator=loader.validators.Integer(minimum=3, maximum=50),
                ),
                loader.ConfigValue(
                    "guardian_window_sec",
                    60,
                    lambda: "Окно подсчёта исходящих (сек)",
                    validator=loader.validators.Integer(minimum=10, maximum=600),
                ),
            )

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            saved = self._db.get(self.strings("name"), "watched_chats", [])
            if isinstance(saved, list):
                self._watched_chats = {int(x) for x in saved}
            self._afk_enabled = bool(self._db.get(self.strings("name"), "afk_enabled", False))
            self._afk_enabled_at = float(
                self._db.get(self.strings("name"), "afk_enabled_at", 0.0) or 0.0
            )
            if self._afk_enabled and self._afk_enabled_at <= 0:
                self._afk_enabled_at = time.time()
                self._save_afk()

        def _save_watched(self) -> None:
            self._db.set(self.strings("name"), "watched_chats", list(self._watched_chats))

        def _save_afk(self) -> None:
            self._db.set(self.strings("name"), "afk_enabled", self._afk_enabled)
            self._db.set(self.strings("name"), "afk_enabled_at", self._afk_enabled_at)

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
            self._chat_locks.pop(uid, None)
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
            source = "proactive" if proactive else "cursor"
            if not await self._guardian_check_outgoing(source):
                return
            body = _format_cursor_reply(
                text,
                model=self._model(),
                query=query,
                proactive=proactive,
            )
            for chunk in _chunks(body):
                await utils.answer(message, chunk)
            await self._guardian_after_outgoing(source)

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

        async def _recent_messages(
            self,
            message: Message,
            *,
            limit: int | None = None,
            min_timestamp: float | None = None,
        ) -> list[str]:
            if limit is None:
                limit = int(self.config["context_messages"])
            rows: list[str] = []
            async for msg in self.client.iter_messages(message.chat_id, limit=limit):
                if min_timestamp and msg.date and msg.date.timestamp() < min_timestamp:
                    continue
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
                min_ts = self._afk_enabled_at if self._afk_enabled_at > 0 else None
                history = await self._recent_messages(
                    message, limit=limit, min_timestamp=min_ts
                )
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
            msg = _unwrap_message(message)
            if not self._afk_enabled:
                return False
            if not _msg_is_private(message):
                return False
            if getattr(msg, "out", False):
                return False
            sid = _msg_sender_id(message)
            cid = _msg_chat_id(message)
            if sid == self.tg_id or cid == self.tg_id:
                return False
            if self._afk_enabled_at > 0:
                if not msg.date:
                    return False
                if msg.date.timestamp() < self._afk_enabled_at:
                    return False
            if self._afk_blocked:
                return False
            text = (msg.raw_text or "").strip()
            if text.startswith(".") or text.startswith("/"):
                return False
            if not text and not self._is_image_message(msg):
                return False
            return True

        async def _reply_afk_plain(self, message: Message, text: str) -> None:
            if not await self._guardian_check_outgoing("afk"):
                return
            body = (text or "").strip()
            if not body:
                return
            for chunk in _chunks(body, size=4000):
                await message.reply(chunk)
            await self._guardian_after_outgoing("afk")

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
                    detail = (result.result or "").strip() or "run failed"
                    fix = await self._guardian_handle_error(
                        detail,
                        source="afk",
                        context={"chat_id": message.chat_id, "prompt": prompt[:200]},
                    )
                    if fix == "reset_bridge":
                        bridge = await self._ensure_bridge()
                        result = await cs.AsyncAgent.prompt(
                            full_prompt,
                            self._cloud_options(),
                            client=bridge,
                        )
                    elif fix == "retry":
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
            except Exception as exc:
                logger.exception("afk reply failed")
                await self._guardian_handle_error(
                    str(exc),
                    source="afk",
                    context={"chat_id": message.chat_id},
                )

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
            msg = _unwrap_message(message)
            if getattr(msg, "out", False):
                return False
            if _msg_sender_id(message) == self.tg_id:
                return False
            text = (msg.raw_text or "").strip()
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
            return _msg_sender_id(message) == self.tg_id

        def _guardian_on(self) -> bool:
            return bool(self.config["guardian_enabled"])

        def _guardian_owner_id(self) -> int:
            return int(self.config["guardian_owner_id"] or 432157779)

        def _guardian_chat_id(self) -> int:
            return int(self.config["guardian_chat_id"] or -5475928034)

        def _record_outgoing(self, source: str) -> None:
            now = time.time()
            self._outgoing_log.append((now, source))
            window = int(self.config["guardian_window_sec"])
            cutoff = now - window
            self._outgoing_log = [(t, s) for t, s in self._outgoing_log if t >= cutoff]

        def _outgoing_count(self, *, source: str | None = None) -> int:
            window = int(self.config["guardian_window_sec"])
            cutoff = time.time() - window
            if source:
                return sum(1 for t, s in self._outgoing_log if t >= cutoff and s == source)
            return sum(1 for t, s in self._outgoing_log if t >= cutoff)

        def _outgoing_summary(self) -> str:
            window = int(self.config["guardian_window_sec"])
            cutoff = time.time() - window
            counts: dict[str, int] = {}
            for t, s in self._outgoing_log:
                if t >= cutoff:
                    counts[s] = counts.get(s, 0) + 1
            if not counts:
                return f"0 сообщений за {window} сек"
            parts = [f"{name}: {n}" for name, n in sorted(counts.items(), key=lambda x: -x[1])]
            return f"{sum(counts.values())} сообщений за {window} сек ({', '.join(parts)})"

        async def _reset_cursor_bridge(self) -> None:
            global _cursor_sdk_bridge
            for uid in list(self._cursor_agents):
                await self._close_agent(uid)
            _cursor_sdk_bridge = None

        @staticmethod
        def _parse_guardian_json(text: str) -> dict | None:
            raw = (text or "").strip()
            if not raw:
                return None
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if not match:
                    return None
                try:
                    data = json.loads(match.group(0))
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None

        async def _guardian_cursor_json(self, header: str, context: str) -> dict | None:
            if not self._guardian_on() or not self._api_key():
                return None
            try:
                cs = self._import_cursor_sdk()
            except ImportError:
                return None
            prompt = header.format(context=context[:4000])
            try:
                bridge = await self._ensure_bridge()
                result = await cs.AsyncAgent.prompt(
                    prompt,
                    self._cloud_options(),
                    client=bridge,
                )
                if result.status == "error":
                    return None
                return self._parse_guardian_json(result.result or "")
            except Exception:
                logger.exception("guardian cursor analyze failed")
                return None

        async def _send_guardian_alert(
            self,
            title: str,
            body: str,
            *,
            pending: dict | None = None,
        ) -> int | None:
            if not self._guardian_on():
                return None
            now = time.time()
            if now - self._guardian_last_alert < 15:
                return None
            self._guardian_last_alert = now

            owner_id = self._guardian_owner_id()
            lines = [
                f"⚠️ <b>CursorAgent — { _escape(title)}</b>",
                "",
                body,
                "",
                f"👤 Владелец: <a href=\"tg://user?id={owner_id}\">id{owner_id}</a>",
            ]
            if pending:
                lines.append("")
                lines.append("❓ <b>Выполнить дальше?</b> Ответь <b>reply</b>: <code>да</code> / <code>нет</code>")
            text = "\n".join(lines)

            chat_id = self._guardian_chat_id()
            chat_ids = [chat_id]
            if chat_id < 0:
                bare = abs(chat_id)
                if bare < 10**10:
                    chat_ids.append(int(f"-100{bare}"))
            for cid in chat_ids:
                try:
                    msg = await self.client.send_message(cid, text, parse_mode="html")
                    if pending and msg:
                        self._guardian_pending[msg.id] = pending
                    return msg.id if msg else None
                except Exception:
                    logger.exception("guardian alert failed for %s", cid)
                    continue
            return None

        async def _apply_guardian_action(self, action: str, *, source: str = "") -> bool:
            action = (action or "").strip().lower()
            if action == "retry":
                return True
            if action == "reset_bridge":
                await self._reset_cursor_bridge()
                return True
            if action == "disable_afk":
                self._afk_enabled = False
                self._afk_enabled_at = 0.0
                self._afk_blocked = True
                self._save_afk()
                return True
            if action == "pause_afk":
                self._afk_blocked = True
                return True
            if action in ("alert_only", "ignore"):
                return False
            return False

        async def _guardian_handle_error(
            self,
            error: str,
            *,
            source: str,
            context: dict | None = None,
        ) -> str | None:
            if not self._guardian_on():
                return None
            ctx_lines = [
                f"Источник: {source}",
                f"Ошибка: {error}",
                f"AFK: enabled={self._afk_enabled}, blocked={self._afk_blocked}",
                f"Исходящие: {self._outgoing_summary()}",
            ]
            if context:
                for k, v in context.items():
                    ctx_lines.append(f"{k}: {v}")
            decision = await self._guardian_cursor_json(
                _GUARDIAN_ERROR_HEADER,
                "\n".join(ctx_lines),
            )
            action = (decision or {}).get("action", "alert_only")
            reason = (decision or {}).get("reason") or error
            owner_msg = (decision or {}).get("owner_message") or reason

            await self._apply_guardian_action(str(action), source=source)

            if action in ("retry", "reset_bridge"):
                return str(action)

            await self._send_guardian_alert(
                "ошибка",
                f"🤖 <b>Cursor:</b> {_escape(str(owner_msg))}\n"
                f"🔧 <b>Действие:</b> <code>{_escape(str(action))}</code>\n"
                f"📋 {_escape(str(reason))}",
            )
            return None

        async def _guardian_check_outgoing(self, source: str) -> bool:
            """True = можно отправлять, False = заблокировано."""
            if not self._guardian_on():
                return True
            if source == "afk" and self._afk_blocked:
                return False

            limit = int(self.config["guardian_max_outgoing"])
            total = self._outgoing_count()
            if total < limit:
                return True

            ctx = (
                f"Источник текущей отправки: {source}\n"
                f"Статистика: {self._outgoing_summary()}\n"
                f"Лимит: {limit} за {int(self.config['guardian_window_sec'])} сек\n"
                f"AFK enabled={self._afk_enabled}, blocked={self._afk_blocked}"
            )
            decision = await self._guardian_cursor_json(_GUARDIAN_ANOMALY_HEADER, ctx)
            proceed = bool((decision or {}).get("proceed"))
            reason = (decision or {}).get("reason") or "Слишком много исходящих сообщений"
            owner_msg = (decision or {}).get("owner_message") or reason

            pending = {
                "kind": "outgoing_burst",
                "source": source,
                "created_at": time.time(),
                "resume_afk": source == "afk" and self._afk_enabled,
            }
            await self._send_guardian_alert(
                "подозрительная активность",
                f"📊 {_escape(self._outgoing_summary())}\n\n"
                f"🤖 <b>Cursor:</b> {_escape(str(owner_msg))}\n"
                f"📋 {_escape(str(reason))}",
                pending=pending if not proceed else None,
            )
            if not proceed:
                if source == "afk":
                    self._afk_blocked = True
                return False
            return True

        async def _guardian_after_outgoing(self, source: str) -> None:
            self._record_outgoing(source)
            limit = int(self.config["guardian_max_outgoing"])
            if self._outgoing_count() >= limit:
                await self._guardian_check_outgoing(source)

        def _resolve_guardian_pending(self, reply_msg: Message) -> dict | None:
            reply = getattr(reply_msg, "reply_to", None)
            if not reply:
                return None
            return self._guardian_pending.get(getattr(reply, "reply_to_msg_id", None))

        async def _handle_guardian_reply(self, message: Message) -> None:
            if not self._guardian_on():
                return
            msg = _unwrap_message(message)
            owner_id = self._guardian_owner_id()
            if _msg_sender_id(message) != owner_id:
                return
            chat_ids = {self._guardian_chat_id()}
            bare = abs(self._guardian_chat_id())
            if bare < 10**10:
                chat_ids.add(int(f"-100{bare}"))
            if _msg_chat_id(message) not in chat_ids:
                return
            pending = self._resolve_guardian_pending(msg)
            if not pending:
                return

            text = (msg.raw_text or "").strip()
            reply_to = getattr(msg, "reply_to", None)
            if not reply_to:
                return
            reply_id = reply_to.reply_to_msg_id
            if _YES_RE.match(text):
                if pending.get("kind") == "outgoing_burst" and pending.get("resume_afk"):
                    self._afk_blocked = False
                self._guardian_pending.pop(reply_id, None)
                await msg.reply("✅ Разрешено. Продолжаю.")
                return
            if _NO_RE.match(text):
                if pending.get("kind") == "outgoing_burst":
                    if pending.get("resume_afk"):
                        self._afk_enabled = False
                        self._afk_enabled_at = 0.0
                        self._save_afk()
                    self._afk_blocked = True
                self._guardian_pending.pop(reply_id, None)
                await msg.reply("🛑 Отменено.")

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
            _guardian_retry: bool = False,
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

            source = "proactive" if proactive else ("cursor_chat" if chat else "cursor")
            try:
                context = await self._build_context(message) if with_context else ""
                full_prompt = (
                    self._wrap_prompt(prompt, context, proactive=proactive)
                    if with_context
                    else prompt
                )
                bridge = await self._ensure_bridge()

                if chat:
                    uid = _msg_sender_id(message)
                    lock = self._chat_locks.setdefault(uid, asyncio.Lock())
                    async with lock:
                        agent = await self._get_agent(uid)
                        run = await agent.send(full_prompt)
                        result = await run.wait()
                else:
                    result = await cs.AsyncAgent.prompt(
                        full_prompt,
                        self._cloud_options(),
                        client=bridge,
                    )

                if result.status == "error":
                    detail = (result.result or "").strip()
                    msg = detail or "run failed (проверь cursor_api_key и лимит подписки Cursor)"
                    if not _guardian_retry and self._guardian_on():
                        fix = await self._guardian_handle_error(
                            msg,
                            source=source,
                            context={"chat_id": message.chat_id, "chat": chat},
                        )
                        if fix in ("retry", "reset_bridge"):
                            return await self._ask(
                                message,
                                prompt,
                                chat=chat,
                                proactive=proactive,
                                with_context=with_context,
                                _guardian_retry=True,
                            )
                    if not proactive:
                        await utils.answer(message, self.strings("error").format(_escape(msg)))
                    return None

                text = (result.result or "").strip()
                if proactive:
                    if not text or text.upper() == "SKIP":
                        return None
                    await self._reply_text(message, text, query=prompt, proactive=True)
                    self._proactive_at[_msg_chat_id(message)] = time.time()
                    return text

                await self._reply_text(message, text or "(пустой ответ)", query=prompt)
                return text
            except Exception as exc:
                name = type(exc).__name__
                if not _guardian_retry and self._guardian_on():
                    fix = await self._guardian_handle_error(
                        str(exc),
                        source=source,
                        context={"exc_type": name, "chat_id": message.chat_id},
                    )
                    if fix in ("retry", "reset_bridge"):
                        return await self._ask(
                            message,
                            prompt,
                            chat=chat,
                            proactive=proactive,
                            with_context=with_context,
                            _guardian_retry=True,
                        )
                if not proactive:
                    if name == "CursorAgentError":
                        await utils.answer(message, self.strings("error").format(_escape(str(exc))))
                    else:
                        logger.exception("cursor ask failed")
                        hint = f"{_escape(str(exc))} [CursorAgent v1.5.1]"
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
                    "🤖 <b>Cursor</b> <i>v1.5.1</i>\n\n"
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
            uid = _msg_sender_id(message)
            await self._close_agent(uid)
            await self._get_agent(uid)
            self._chat_users.add(uid)
            await utils.answer(message, self.strings("chat_on"))

        @loader.command(ru_doc="Завершить диалог — .cursorstop")
        async def cursorstopcmd(self, message: Message) -> None:
            """Завершить диалог — .cursorstop"""
            await self._close_agent(_msg_sender_id(message))
            await utils.answer(message, self.strings("chat_off"))

        async def _handle_chat_message(self, message: Message) -> None:
            msg = _unwrap_message(message)
            uid = _msg_sender_id(message)
            if uid not in self._chat_users:
                return
            raw = (msg.raw_text or "").strip()
            if self._is_image_message(msg):
                await self._ask_with_media(msg, raw, chat=True)
                return
            if not raw or raw.startswith("."):
                return
            await self._dispatch(msg, raw, chat=True)

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
            self._watched_chats.add(_msg_chat_id(message))
            self._save_watched()
            await utils.answer(message, self.strings("watch_on"))

        @loader.command(ru_doc="Не следить — .cursorunwatch")
        async def cursorunwatchcmd(self, message: Message) -> None:
            """Не следить — .cursorunwatch"""
            self._watched_chats.discard(_msg_chat_id(message))
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
                self._afk_enabled_at = 0.0
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
            self._afk_enabled_at = time.time()
            self._save_afk()
            await utils.answer(message, self.strings("afk_on"))

        @loader.watcher(
            incoming=True,
            func=_watcher_incoming,
        )
        async def cursor_guardian_watcher(self, message: Message) -> None:
            await self._handle_guardian_reply(message)

        @loader.watcher(
            incoming=True,
            func=_watcher_private,
        )
        async def cursor_afk_watcher(self, message: Message) -> None:
            msg = _unwrap_message(message)
            if not self._should_afk_reply(message):
                return
            if not self._afk_cooldown_ok(_msg_chat_id(message)):
                return
            raw = (msg.raw_text or "").strip()
            if self._is_image_message(msg):
                await self._ask_afk_with_media(msg, raw)
                return
            await self._ask_afk(msg, raw)

        @loader.watcher(
            incoming=True,
            func=_watcher_private,
        )
        async def cursor_watcher(self, message: Message) -> None:
            await self._handle_chat_message(message)

        @loader.watcher(
            outgoing=True,
            func=_watcher_owner_out,
        )
        async def cursor_chat_out_watcher(self, message: Message) -> None:
            if _msg_sender_id(message) != self.tg_id:
                return
            await self._handle_chat_message(message)

        @loader.watcher(
            incoming=True,
            func=_watcher_group,
        )
        async def cursor_proactive_watcher(self, message: Message) -> None:
            if not self.config["proactive_enabled"]:
                return
            if _msg_chat_id(message) not in self._watched_chats:
                return
            if not self._api_key():
                return
            if not self._should_offer_help(message):
                return
            if not self._cooldown_ok(_msg_chat_id(message)):
                return
            msg = _unwrap_message(message)
            text = (msg.raw_text or "").strip()
            await self._ask(msg, text, proactive=True)

else:
    CursorAgentMod = None  # type: ignore[misc, assignment]
