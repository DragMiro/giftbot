"""Сбор текста с premium emoji из сообщений aiogram."""

from __future__ import annotations

from aiogram.types import Message

from core.entities import (
    EntityData,
    TextPart,
    count_custom_emoji,
    entities_from_aiogram,
    entity_from_dict,
    entity_to_dict,
    merge_message_parts,
    preview_line,
)


def message_text_content(message: Message) -> str | None:
    if message.text is not None:
        return message.text
    if message.caption is not None:
        return message.caption
    return None


def chunk_from_message(message: Message) -> tuple[str, list[EntityData]] | None:
    text = message_text_content(message)
    if text is None:
        return None
    return text, entities_from_aiogram(message)


def chunks_from_state(raw_chunks: list[dict] | None) -> list[tuple[str, list[EntityData]]]:
    if not raw_chunks:
        return []
    out: list[tuple[str, list[EntityData]]] = []
    for item in raw_chunks:
        entities = [entity_from_dict(e) for e in item.get("entities", [])]
        out.append((item["text"], entities))
    return out


def chunks_to_state(chunks: list[tuple[str, list[EntityData]]]) -> list[dict]:
    return [
        {"text": text, "entities": [entity_to_dict(e) for e in entities]}
        for text, entities in chunks
    ]


def build_text_part(raw_chunks: list[dict] | None) -> TextPart:
    return merge_message_parts(chunks_from_state(raw_chunks))


def format_preview(parts: list[TextPart]) -> str:
    return "\n".join(preview_line(i, p) for i, p in enumerate(parts, 1))


def total_custom_emoji(raw_chunks: list[dict] | None) -> int:
    part = build_text_part(raw_chunks)
    return count_custom_emoji(part.entities)
