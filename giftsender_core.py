"""Общая логика GiftSender — каталог, отправка, текст, emoji."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from telethon import TelegramClient
from telethon.errors import RPCError
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


def entity_to_dict(entity: EntityData) -> dict[str, Any]:
    d: dict[str, Any] = {
        "type": entity.type,
        "offset": entity.offset,
        "length": entity.length,
    }
    if entity.custom_emoji_id is not None:
        d["custom_emoji_id"] = entity.custom_emoji_id
    if entity.url is not None:
        d["url"] = entity.url
    return d


def entity_from_dict(d: dict[str, Any]) -> EntityData:
    return EntityData(
        type=str(d["type"]),
        offset=int(d["offset"]),
        length=int(d["length"]),
        custom_emoji_id=int(d["custom_emoji_id"]) if d.get("custom_emoji_id") is not None else None,
        url=d.get("url"),
    )


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


def entities_from_aiogram(message) -> list[EntityData]:
    raw = message.entities or message.caption_entities or []
    result: list[EntityData] = []
    for ent in raw:
        etype = ent.type.value if hasattr(ent.type, "value") else str(ent.type)
        etype = etype.removeprefix("message_entity_").removeprefix("MessageEntity")
        data = EntityData(type=etype, offset=int(ent.offset), length=int(ent.length))
        if etype == "custom_emoji" and getattr(ent, "custom_emoji_id", None) is not None:
            data.custom_emoji_id = int(ent.custom_emoji_id)
        if etype == "text_link" and getattr(ent, "url", None):
            data.url = ent.url
        result.append(data)
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


def split_text(text: str, parts_count: int, *, by_words: bool = False) -> list[str]:
    return [p.text for p in split_text_parts(TextPart(text=text), parts_count, by_words=by_words)]


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
