"""Каталог подарков и отправка через Telethon (Telegram Stars)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.payments import (
    CheckCanSendGiftRequest,
    GetPaymentFormRequest,
    GetStarGiftsRequest,
    SendStarsFormRequest,
)
from telethon.tl.types import InputInvoiceStarGift, TextWithEntities

from .entities import TextPart, to_telethon_entities
from .models import GiftInfo, SendPlan

if TYPE_CHECKING:
    from telethon.tl.types import StarGift

logger = logging.getLogger(__name__)


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
        """Отправляет все части. Возвращает (успешно, список ошибок)."""
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
