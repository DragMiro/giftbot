"""Библиотека Cursor SDK для GiftSender / CursorAgent."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_bridge = None

_SONG_PROMPT = """Ты помощник для Telegram-подарков с текстом песни.

Песня: {query}

Верни ТОЛЬКО ключевые фразы или отдельные слова из этой песни (припев и главные строки).
Требования:
- каждая фраза на отдельной строке
- без нумерации, без markdown, без пояснений
- до {max_lines} строк
- каждая строка до 45 символов
- на языке оригинала песни
- выбирай самые запоминающиеся, эмоциональные части

Если песню не знаешь или не уверен — первая строка должна быть ровно:
ERROR: песня не найдена
"""

_SORT_PROMPT = """Отсортируй слова/фразы для серии Telegram-подарков (от слабого к сильному эмоциональному финалу).

Исходный список:
{lines}

Верни ТОЛЬКО тот же набор строк, по одной на строку, без нумерации и комментариев.
Количество строк должно совпадать.
"""


def import_cursor_sdk():
    try:
        import cursor_sdk

        return cursor_sdk
    except ImportError as exc:
        raise ImportError("cursor-sdk") from exc


async def ensure_bridge():
    global _bridge
    cs = import_cursor_sdk()
    if _bridge is None or not hasattr(_bridge, "create_agent"):
        _bridge = await cs.AsyncClient.launch_bridge()
    return _bridge


def cloud_options(
    *,
    api_key: str,
    model: str = "composer-2.5",
    repo_url: str = "https://github.com/DragMiro/giftbot",
    repo_branch: str = "main",
):
    cs = import_cursor_sdk()
    return cs.AgentOptions(
        api_key=api_key,
        model=model,
        cloud=cs.CloudAgentOptions(
            repos=[
                cs.CloudRepository(
                    url=repo_url.strip(),
                    starting_ref=repo_branch.strip() or "main",
                )
            ],
            skip_reviewer_request=True,
        ),
    )


async def cursor_prompt(
    prompt: str,
    *,
    api_key: str,
    model: str = "composer-2.5",
    repo_url: str = "https://github.com/DragMiro/giftbot",
    repo_branch: str = "main",
) -> str:
    if not (api_key or "").strip():
        raise ValueError("Не задан cursor_api_key")

    cs = import_cursor_sdk()
    bridge = await ensure_bridge()
    result = await cs.AsyncAgent.prompt(
        prompt,
        cloud_options(
            api_key=api_key.strip(),
            model=model,
            repo_url=repo_url,
            repo_branch=repo_branch,
        ),
        client=bridge,
    )
    if result.status == "error":
        raise RuntimeError("Cursor run failed")
    return (result.result or "").strip()


def parse_line_list(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        line = re.sub(r"^\d+[\.\)\-]\s*", "", line)
        line = line.strip("•-* ")
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


async def song_phrases(
    query: str,
    *,
    api_key: str,
    model: str = "composer-2.5",
    max_lines: int = 20,
    repo_url: str = "https://github.com/DragMiro/giftbot",
) -> list[str]:
    text = await cursor_prompt(
        _SONG_PROMPT.format(query=query.strip(), max_lines=max_lines),
        api_key=api_key,
        model=model,
        repo_url=repo_url,
    )
    if text.upper().startswith("ERROR:"):
        raise ValueError(text.split(":", 1)[-1].strip() or "песня не найдена")
    lines = parse_line_list(text)
    if len(lines) < 2:
        raise ValueError("Cursor вернул слишком мало фраз — уточни название песни")
    return lines[:max_lines]


async def sort_phrases(
    lines: list[str],
    *,
    api_key: str,
    model: str = "composer-2.5",
    repo_url: str = "https://github.com/DragMiro/giftbot",
) -> list[str]:
    if len(lines) < 2:
        return lines
    text = await cursor_prompt(
        _SORT_PROMPT.format(lines="\n".join(lines)),
        api_key=api_key,
        model=model,
        repo_url=repo_url,
    )
    sorted_lines = parse_line_list(text)
    if len(sorted_lines) != len(lines):
        return lines
    return sorted_lines


async def analyze_image_openai(
    image_bytes: bytes,
    prompt: str,
    *,
    api_key: str,
    mime: str = "image/jpeg",
) -> str:
    import base64

    import httpx

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt or "Опиши изображение подробно по-русски."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 1200,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


def _resolve_loader():
    try:
        from .. import loader

        return loader
    except ImportError:
        pass
    try:
        from heroku import loader

        return loader
    except ImportError:
        return None


_loader = _resolve_loader()

if _loader:

    class CursorAiLibMod(_loader.Module):
        """Пустой модуль-обёртка — библиотека без команд."""

        strings = {
            "name": "CursorAiLib",
            "_cls_doc": "📚 Cursor library (без команд, не удаляй)",
        }

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            pass


def register(name):  # noqa: ARG001 — Heroku .dlm по raw URL
    """Heroku вызывает register(), если не нашёл loader.Module в модуле."""
    loader = _resolve_loader()
    if loader is None:
        raise ImportError("Heroku loader not available")
    mod_cls = globals().get("CursorAiLibMod")
    if mod_cls is not None:
        return mod_cls()

    class CursorAiLibModFallback(loader.Module):
        strings = {"name": "CursorAiLib", "_cls_doc": "lib"}

        async def client_ready(self, client, db) -> None:  # noqa: ARG002
            pass

    return CursorAiLibModFallback()
