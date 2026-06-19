"""Premium/custom emoji — UTF-16 offsets и конвертация aiogram → Telethon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    TypeMessageEntity,
)


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
    """Извлекает entities из Telethon Message (Hikka / userbot)."""
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
    """Извлекает entities из aiogram Message (Bot API)."""
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
    """Склеивает несколько сообщений, сдвигая offset entities."""
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
