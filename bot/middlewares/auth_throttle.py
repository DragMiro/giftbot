"""Строгий лимит на /login и шаги авторизации."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from bot.states import AuthFlow

_AUTH_STATES = {s.state for s in AuthFlow.__all_states__}


class AuthThrottleMiddleware(BaseMiddleware):
    def __init__(self, min_interval: float = 3.0, max_attempts: int = 8, window: float = 600.0) -> None:
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._window = window
        self._last: dict[int, float] = {}
        self._attempts: dict[int, list[float]] = {}

    def _too_many(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = [t for t in self._attempts.get(user_id, []) if now - t < self._window]
        self._attempts[user_id] = hits
        return len(hits) >= self._max_attempts

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)

        current = await state.get_state()
        if current not in _AUTH_STATES:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.monotonic()

        if self._too_many(user_id):
            await event.answer("⛔ Слишком много попыток входа. Подожди 10 минут или /cancel.")
            return None

        prev = self._last.get(user_id, 0.0)
        if now - prev < self._min_interval:
            await event.answer("Подожди пару секунд перед следующим шагом.")
            return None

        self._last[user_id] = now
        self._attempts.setdefault(user_id, []).append(now)
        return await handler(event, data)
