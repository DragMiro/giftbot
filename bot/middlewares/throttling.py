"""Базовый антифлуд для всех апдейтов."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, min_interval: float = 0.35) -> None:
        self._min_interval = min_interval
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        prev = self._last.get(user.id, 0.0)
        if now - prev < self._min_interval:
            if isinstance(event, CallbackQuery):
                await event.answer("Не так быстро")
            return None

        self._last[user.id] = now
        if len(self._last) > 10000:
            cutoff = now - 120
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return await handler(event, data)
