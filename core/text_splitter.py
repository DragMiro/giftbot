"""Разбиение текста на N частей с сохранением premium emoji entities."""

from __future__ import annotations

from core.entities import EntityData, TextPart, extract_utf16_slice, utf16_len


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
    """Не режем premium emoji — переносим границу за конец entity."""
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
        # Найти utf16-диапазон слов в исходном тексте (приблизительно через join)
        start_py = source.text.find(chunk_words[0], 0 if not parts else source.text.find(parts[-1].text) + len(parts[-1].text))
        if start_py < 0:
            start_py = 0
        end_py = start_py + len(chunk_text)
        from core.entities import py_index_to_utf16

        start_u = py_index_to_utf16(source.text, start_py)
        end_u = py_index_to_utf16(source.text, end_py)
        part_entities = _entities_in_range(source.entities, start_u, end_u)
        parts.append(TextPart(text=chunk_text, entities=part_entities))
    return parts
