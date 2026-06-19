# @version=1.1.2
# @description Отправка Telegram-подарков с текстом и premium emoji
# @author giftbot
# requires: telethon>=1.38.0
"""GiftSender — модуль для Hikka / Heroku / Telethon userbot.

Отправляет Telegram Gifts (Stars) с текстом, разбитым на части.
Поддерживает premium emoji из сообщений.

Команды:
  .gift       — мастер отправки
  .giftcancel — отмена
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.custom import Message
from telethon.tl.functions.payments import (
    CheckCanSendGiftRequest,
    GetPaymentFormRequest,
    GetStarGiftsRequest,
    SendStarsFormRequest,
)
from telethon.tl.types import (
    InputInvoiceStarGift,
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    TextWithEntities,
    TypeMessageEntity,
)

if TYPE_CHECKING:
    from telethon.tl.types import StarGift

logger = logging.getLogger(__name__)

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]


@dataclass(slots=True)
class EntityData:
    type: str
    offset: int
    length: int
    custom_emoji_id: int | None = None
    url: str | None = None


@dataclass(slots=True)
class TextPart:
    text: str
    entities: list[EntityData] = field(default_factory=list)


@dataclass(slots=True)
class GiftInfo:
    id: int
    stars: int
    title: str | None
    sold_out: bool
    emoji: str = "🎁"


@dataclass(slots=True)
class SendPlan:
    gift: GiftInfo
    recipient: str
    parts: list[TextPart]
    total_stars: int
    hide_name: bool = False


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_to_py_index(text: str, offset: int) -> int:
    if offset <= 0:
        return 0
    pos = 0
    units = 0
    for i, ch in enumerate(text):
        if units >= offset:
            return i
        units += len(ch.encode("utf-16-le")) // 2
        pos = i + 1
    return pos


def py_index_to_utf16(text: str, index: int) -> int:
    return utf16_len(text[:index])


def extract_utf16_slice(text: str, start: int, end: int) -> str:
    return text[utf16_to_py_index(text, start) : utf16_to_py_index(text, end)]


def entities_from_telethon(message) -> list[EntityData]:
    result: list[EntityData] = []
    for ent in message.entities or []:
        name = type(ent).__name__
        if name == "MessageEntityCustomEmoji":
            result.append(
                EntityData(
                    type="custom_emoji",
                    offset=ent.offset,
                    length=ent.length,
                    custom_emoji_id=ent.document_id,
                )
            )
        elif name == "MessageEntityBold":
            result.append(EntityData(type="bold", offset=ent.offset, length=ent.length))
        elif name == "MessageEntityItalic":
            result.append(EntityData(type="italic", offset=ent.offset, length=ent.length))
        elif name == "MessageEntityUnderline":
            result.append(EntityData(type="underline", offset=ent.offset, length=ent.length))
        elif name == "MessageEntityStrike":
            result.append(EntityData(type="strikethrough", offset=ent.offset, length=ent.length))
        elif name == "MessageEntitySpoiler":
            result.append(EntityData(type="spoiler", offset=ent.offset, length=ent.length))
        elif name == "MessageEntityTextUrl":
            result.append(
                EntityData(
                    type="text_link",
                    offset=ent.offset,
                    length=ent.length,
                    url=ent.url,
                )
            )
    return result


def merge_message_parts(parts: list[tuple[str, list[EntityData]]]) -> TextPart:
    full = ""
    merged: list[EntityData] = []
    for text, entities in parts:
        base = utf16_len(full)
        full += text
        for ent in entities:
            merged.append(
                EntityData(
                    type=ent.type,
                    offset=ent.offset + base,
                    length=ent.length,
                    custom_emoji_id=ent.custom_emoji_id,
                    url=ent.url,
                )
            )
    return TextPart(text=full, entities=merged)


def to_telethon_entities(entities: list[EntityData]) -> list[TypeMessageEntity]:
    out: list[TypeMessageEntity] = []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id is not None:
            out.append(
                MessageEntityCustomEmoji(
                    offset=ent.offset,
                    length=ent.length,
                    document_id=ent.custom_emoji_id,
                )
            )
        elif ent.type == "bold":
            out.append(MessageEntityBold(offset=ent.offset, length=ent.length))
        elif ent.type == "italic":
            out.append(MessageEntityItalic(offset=ent.offset, length=ent.length))
        elif ent.type == "underline":
            out.append(MessageEntityUnderline(offset=ent.offset, length=ent.length))
        elif ent.type == "strikethrough":
            out.append(MessageEntityStrike(offset=ent.offset, length=ent.length))
        elif ent.type == "spoiler":
            out.append(MessageEntitySpoiler(offset=ent.offset, length=ent.length))
        elif ent.type == "text_link" and ent.url:
            out.append(MessageEntityTextUrl(offset=ent.offset, length=ent.length, url=ent.url))
    return out


def count_custom_emoji(entities: list[EntityData]) -> int:
    return sum(1 for e in entities if e.type == "custom_emoji")


def preview_line(index: int, part: TextPart) -> str:
    emoji_n = count_custom_emoji(part.entities)
    suffix = f" + {emoji_n} premium emoji" if emoji_n else ""
    safe = part.text.replace("<", "&lt;").replace(">", "&gt;")
    if len(safe) > 80:
        safe = safe[:80] + "…"
    return f"{index}. <code>{safe}</code>{suffix}"


def split_text_parts(
    source: TextPart,
    parts_count: int,
    *,
    by_words: bool = False,
) -> list[TextPart]:
    text = source.text.strip()
    if not text and not source.entities:
        raise ValueError("Текст не может быть пустым")
    if parts_count < 1:
        raise ValueError("Число частей должно быть >= 1")
    if parts_count == 1:
        return [TextPart(text=text, entities=list(source.entities))]

    if by_words:
        return _split_by_words(source, parts_count)

    total_utf16 = utf16_len(text)
    if parts_count > total_utf16:
        raise ValueError(
            f"Частей ({parts_count}) больше, чем символов ({total_utf16}). "
            "Уменьши число частей."
        )

    chunk = total_utf16 // parts_count
    remainder = total_utf16 % parts_count
    parts: list[TextPart] = []
    start = 0
    for i in range(parts_count):
        size = chunk + (1 if i < remainder else 0)
        end = start + size
        if i < parts_count - 1:
            end = _adjust_split_end(source.entities, end, total_utf16)
        else:
            end = total_utf16
        part_text = extract_utf16_slice(text, start, end)
        part_entities = _entities_in_range(source.entities, start, end)
        if part_text.strip() or part_entities:
            parts.append(TextPart(text=part_text, entities=part_entities))
        start = end

    return [p for p in parts if p.text.strip() or p.entities]


def _adjust_split_end(entities: list[EntityData], end: int, total: int) -> int:
    for ent in entities:
        ent_end = ent.offset + ent.length
        if ent.offset < end < ent_end:
            end = ent_end
    return min(end, total)


def _entities_in_range(entities: list[EntityData], start: int, end: int) -> list[EntityData]:
    out: list[EntityData] = []
    for ent in entities:
        ent_end = ent.offset + ent.length
        if ent.offset >= start and ent_end <= end:
            out.append(
                EntityData(
                    type=ent.type,
                    offset=ent.offset - start,
                    length=ent.length,
                    custom_emoji_id=ent.custom_emoji_id,
                    url=ent.url,
                )
            )
    return out


def _split_by_words(source: TextPart, parts_count: int) -> list[TextPart]:
    words = source.text.split()
    if not words:
        raise ValueError("Нет слов для разбиения")
    if parts_count > len(words):
        raise ValueError(
            f"Частей ({parts_count}) больше, чем слов ({len(words)}). "
            "Уменьши число частей."
        )
    chunk_size = len(words) // parts_count
    remainder = len(words) % parts_count
    parts: list[TextPart] = []
    idx = 0
    for i in range(parts_count):
        size = chunk_size + (1 if i < remainder else 0)
        chunk_words = words[idx : idx + size]
        idx += size
        chunk_text = " ".join(chunk_words)
        if not chunk_text:
            continue
        start_py = source.text.find(
            chunk_words[0],
            0 if not parts else source.text.find(parts[-1].text) + len(parts[-1].text),
        )
        if start_py < 0:
            start_py = 0
        end_py = start_py + len(chunk_text)
        start_u = py_index_to_utf16(source.text, start_py)
        end_u = py_index_to_utf16(source.text, end_py)
        part_entities = _entities_in_range(source.entities, start_u, end_u)
        parts.append(TextPart(text=chunk_text, entities=part_entities))
    return parts


def _gift_emoji(gift: StarGift) -> str:
    doc = getattr(gift, "sticker", None)
    if doc and getattr(doc, "attributes", None):
        for attr in doc.attributes:
            alt = getattr(attr, "alt", None)
            if alt:
                return alt
    return "🎁"


def _gift_title(gift: StarGift) -> str | None:
    title = getattr(gift, "title", None)
    if title:
        return title
    emoji = _gift_emoji(gift)
    return f"{emoji} подарок"


class GiftCatalog:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._cache: list[GiftInfo] | None = None
        self._hash = 0

    async def list_gifts(self, *, force_refresh: bool = False) -> list[GiftInfo]:
        if self._cache is not None and not force_refresh:
            return self._cache

        result = await self._client(GetStarGiftsRequest(hash=self._hash))
        if hasattr(result, "gifts"):
            gifts = [
                GiftInfo(
                    id=g.id,
                    stars=int(g.stars),
                    title=_gift_title(g),
                    sold_out=bool(getattr(g, "sold_out", False)),
                    emoji=_gift_emoji(g),
                )
                for g in result.gifts
                if not getattr(g, "sold_out", False)
            ]
            self._cache = sorted(gifts, key=lambda x: x.stars)
            self._hash = getattr(result, "hash", 0)
        return self._cache or []

    async def get_gift(self, gift_id: int) -> GiftInfo | None:
        for gift in await self.list_gifts():
            if gift.id == gift_id:
                return gift
        return None


class GiftSender:
    def __init__(self, client: TelegramClient, *, delay: float = 2.0) -> None:
        self._client = client
        self._delay = delay

    async def send_plan(self, plan: SendPlan) -> tuple[int, list[str]]:
        peer = await self._client.get_input_entity(plan.recipient)
        errors: list[str] = []
        sent = 0

        for i, part in enumerate(plan.parts, start=1):
            try:
                await self._send_single(
                    peer=peer,
                    gift_id=plan.gift.id,
                    part=part,
                    hide_name=plan.hide_name,
                )
                sent += 1
                logger.info("Подарок %d/%d отправлен: %r", i, len(plan.parts), part.text[:50])
            except RPCError as exc:
                msg = f"Часть {i}: {exc}"
                logger.error(msg)
                errors.append(msg)
            except Exception as exc:  # noqa: BLE001
                msg = f"Часть {i}: {exc}"
                logger.exception(msg)
                errors.append(msg)

            if i < len(plan.parts):
                await asyncio.sleep(self._delay)

        return sent, errors

    async def _send_single(
        self,
        *,
        peer,
        gift_id: int,
        part: TextPart,
        hide_name: bool,
    ) -> None:
        check = await self._client(CheckCanSendGiftRequest(gift_id=gift_id))
        if type(check).__name__ == "CheckCanSendGiftResultFail":
            reason = getattr(getattr(check, "reason", None), "text", "нельзя отправить")
            raise ValueError(str(reason))

        entities = to_telethon_entities(part.entities)
        invoice = InputInvoiceStarGift(
            peer=peer,
            gift_id=gift_id,
            message=TextWithEntities(part.text, entities),
            hide_name=hide_name or None,
        )
        form = await self._client(GetPaymentFormRequest(invoice=invoice))
        await self._client(
            SendStarsFormRequest(form_id=form.form_id, invoice=invoice)
        )


class _Flow:
    __slots__ = (
        "step",
        "gift_id",
        "gift_stars",
        "gift_title",
        "recipient",
        "text_chunks",
        "parts",
        "hide_name",
        "_gift_list",
    )

    def __init__(self) -> None:
        self.step = ""
        self.gift_id = 0
        self.gift_stars = 0
        self.gift_title = ""
        self.recipient = ""
        self.text_chunks: list[tuple[str, list]] = []
        self.parts: list[TextPart] = []
        self.hide_name = False
        self._gift_list = []


if loader:

    @loader.tds
    class GiftSenderMod(loader.Module):
        """🎁 Подарки с текстом и premium emoji"""

        strings = {
            "name": "GiftSender",
            "done_hint": "Готово. /done или .giftdone",
            "cancelled": "Отменено.",
        }

        strings_ru = {
            "name": "GiftSender",
            "_cls_doc": "🎁 Подарки с текстом и premium emoji",
            "done_hint": "Готово. /done или .giftdone",
            "cancelled": "Отменено.",
        }

        def __init__(self) -> None:
            self._flows: dict[int, _Flow] = {}
            self._catalog: GiftCatalog | None = None
            self._sender: GiftSender | None = None
            self.config = loader.ModuleConfig(
                loader.ConfigValue(
                    "send_delay",
                    2.0,
                    lambda v: max(0.5, float(v)),
                    "Задержка между подарками (сек)",
                ),
            )

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            self._catalog = GiftCatalog(client)
            delay = self.config["send_delay"]
            self._sender = GiftSender(client, delay=delay)

        def _flow(self, uid: int) -> _Flow:
            if uid not in self._flows:
                self._flows[uid] = _Flow()
            return self._flows[uid]

        def _clear(self, uid: int) -> None:
            self._flows.pop(uid, None)

        @loader.command(ru_doc="Мастер отправки подарков — .gift")
        async def giftcmd(self, message: Message) -> None:
            """Мастер отправки подарков — .gift"""
            uid = message.sender_id
            self._clear(uid)
            flow = self._flow(uid)
            flow.step = "gift"

            gifts = await self._catalog.list_gifts()
            if not gifts:
                await utils.answer(message, "Каталог подарков пуст.")
                return

            lines = ["🎁 <b>Выбери подарок</b> (номер):\n"]
            for i, g in enumerate(gifts[:20], 1):
                lines.append(f"{i}. {g.emoji} {g.title} — {g.stars}⭐")
            flow._gift_list = gifts[:20]
            await utils.answer(message, "\n".join(lines))

        @loader.command(ru_doc="Завершить ввод текста — .giftdone")
        async def giftdonecmd(self, message: Message) -> None:
            """Завершить ввод текста — .giftdone"""
            await self._finish_text(message)

        @loader.command(ru_doc="Отменить мастер — .giftcancel")
        async def giftcancelcmd(self, message: Message) -> None:
            """Отменить мастер — .giftcancel"""
            self._clear(message.sender_id)
            await utils.answer(message, self.strings("cancelled"))

        @loader.watcher(
            incoming=True,
            func=lambda m: m.is_private and not getattr(m, "out", False),
        )
        async def gift_watcher(self, message: Message) -> None:
            uid = message.sender_id
            flow = self._flows.get(uid)
            if not flow or not flow.step:
                return

            raw = (message.raw_text or "").strip()

            if flow.step == "gift":
                await self._handle_gift_pick(message, flow, uid)
                return

            if flow.step == "text" and raw.lower() in ("/done", ".giftdone", "готово"):
                await self._finish_text(message)
                return

            if flow.step == "text":
                text = message.text or ""
                if not text.strip() and not message.entities:
                    await utils.answer(message, "Текст или premium emoji. /done когда готов.")
                    return
                flow.text_chunks.append((text, entities_from_telethon(message)))
                emoji_n = count_custom_emoji(merge_message_parts(flow.text_chunks).entities)
                hint = f" (premium emoji: {emoji_n})" if emoji_n else ""
                await utils.answer(message, f"➕ Добавлено{hint}. Ещё или /done")
                return

            if flow.step == "recipient":
                flow.recipient = raw.lstrip("@")
                flow.step = "text"
                flow.text_chunks = []
                await utils.answer(
                    message,
                    "✍️ Текст для подарков.\n"
                    "✨ Premium emoji — из клавиатуры Telegram.\n"
                    "Несколькими сообщениями. /done — готово",
                )
            elif flow.step == "parts":
                await self._handle_parts(message, flow, uid, raw)
            elif flow.step == "confirm":
                low = raw.lower()
                if low in ("да", "yes", "+"):
                    await self._do_send(message, flow, uid)
                elif low in ("нет", "no", "-"):
                    self._clear(uid)
                    await utils.answer(message, self.strings("cancelled"))
                else:
                    await utils.answer(message, "Ответь <b>да</b> или <b>нет</b>.")

        async def _finish_text(self, message: Message) -> None:
            uid = message.sender_id
            flow = self._flows.get(uid)
            if not flow or flow.step != "text":
                return
            if not flow.text_chunks:
                await utils.answer(message, "Сначала отправь текст или emoji.")
                return
            flow.step = "parts"
            part = merge_message_parts(flow.text_chunks)
            emoji_n = count_custom_emoji(part.entities)
            extra = f"\n✨ Premium emoji: {emoji_n}" if emoji_n else ""
            await utils.answer(message, f"Текст принят.{extra}\nНа сколько частей разделить?")

        async def _handle_gift_pick(self, message: Message, flow: _Flow, uid: int) -> None:
            try:
                num = int((message.raw_text or "").strip())
                gift = flow._gift_list[num - 1]
            except (ValueError, IndexError):
                return

            flow.gift_id = gift.id
            flow.gift_stars = gift.stars
            flow.gift_title = gift.title or "🎁"
            flow.step = "recipient"
            await utils.answer(
                message,
                f"Выбран: <b>{flow.gift_title}</b> ({flow.gift_stars}⭐)\n"
                "Кому? @username или id",
            )

        async def _handle_parts(self, message: Message, flow: _Flow, uid: int, text: str) -> None:
            try:
                n = int(text)
                source = merge_message_parts(flow.text_chunks)
                parts = split_text_parts(source, n)
            except ValueError as exc:
                await utils.answer(message, str(exc))
                return

            flow.parts = parts
            flow.step = "confirm"
            total = flow.gift_stars * len(parts)
            preview = "\n".join(preview_line(i, p) for i, p in enumerate(parts, 1))
            await utils.answer(
                message,
                f"📋 <b>Подтверждение</b>\n"
                f"{flow.gift_title} × {len(parts)} = {total}⭐\n"
                f"Кому: {flow.recipient}\n\n{preview}\n\n"
                f"Отправить? (да/нет)",
            )

        async def _do_send(self, message: Message, flow: _Flow, uid: int) -> None:
            gift = await self._catalog.get_gift(flow.gift_id)
            if not gift:
                await utils.answer(message, "Подарок недоступен.")
                self._clear(uid)
                return

            plan = SendPlan(
                gift=gift,
                recipient=flow.recipient,
                parts=flow.parts,
                total_stars=flow.gift_stars * len(flow.parts),
                hide_name=flow.hide_name,
            )
            await utils.answer(message, "⏳ Отправляю...")
            sent, errors = await self._sender.send_plan(plan)
            self._clear(uid)
            if errors:
                await utils.answer(message, f"⚠️ {sent}/{len(plan.parts)}. {errors[:2]}")
            else:
                await utils.answer(message, f"✅ {sent} подарков ({plan.total_stars}⭐)")

else:
    GiftSenderMod = None  # type: ignore[misc, assignment]
