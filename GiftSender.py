# @version=1.1.0
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

import importlib.util
import logging
import sys
import urllib.request
from pathlib import Path
from types import ModuleType

from telethon.tl.custom import Message

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CORE_RAW_URL = "https://raw.githubusercontent.com/DragMiro/giftbot/main/giftsender_core.py"


def _load_giftsender_core() -> ModuleType:
    try:
        import giftsender_core as gc

        return gc
    except ImportError:
        local = _ROOT / "giftsender_core.py"
        if local.is_file():
            spec = importlib.util.spec_from_file_location("giftsender_core", local)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["giftsender_core"] = mod
            spec.loader.exec_module(mod)
            return mod

        src = urllib.request.urlopen(_CORE_RAW_URL, timeout=30).read().decode()
        mod = ModuleType("giftsender_core")
        exec(compile(src, _CORE_RAW_URL, "exec"), mod.__dict__)  # noqa: S102
        sys.modules["giftsender_core"] = mod
        return mod


_gc = _load_giftsender_core()
TextPart = _gc.TextPart
count_custom_emoji = _gc.count_custom_emoji
entities_from_telethon = _gc.entities_from_telethon
merge_message_parts = _gc.merge_message_parts
preview_line = _gc.preview_line
GiftCatalog = _gc.GiftCatalog
GiftSender = _gc.GiftSender
SendPlan = _gc.SendPlan
split_text_parts = _gc.split_text_parts

logger = logging.getLogger(__name__)

try:
    from .. import loader, utils
except ImportError:
    loader = None  # type: ignore[assignment]


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

        @loader.command(ru_doc="Отправить подарки с текстом и premium emoji")
        async def giftcmd(self, message: Message) -> None:
            """Start gift wizard — .gift"""
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

        @loader.command(ru_doc="Завершить ввод текста")
        async def giftdonecmd(self, message: Message) -> None:
            """Finish text input — .giftdone"""
            await self._finish_text(message)

        @loader.command(ru_doc="Отменить мастер подарков")
        async def giftcancelcmd(self, message: Message) -> None:
            """Cancel — .giftcancel"""
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
