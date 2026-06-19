"""Мастер отправки подарков."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import confirm_keyboard, gifts_keyboard
from bot.session_manager import sessions
from bot.states import GiftFlow
from bot.text_flow import (
    build_text_part,
    chunk_from_message,
    chunks_to_state,
    format_preview,
    total_custom_emoji,
)
from config import get_settings
from core.entities import entity_to_dict
from core.models import SendPlan
from core.text_splitter import split_text_parts

logger = logging.getLogger(__name__)
router = Router()


def _is_allowed(user_id: int) -> bool:
    allowed = get_settings().allowed_ids()
    if not allowed:
        return True
    return user_id in allowed


async def _require_linked(message: Message) -> bool:
    if await sessions.is_linked(message.from_user.id):
        return True
    await message.answer(
        "❌ Сначала привяжи свой Telegram-аккаунт:\n\n"
        "/login — ввести api_id, api_hash и войти\n\n"
        "<i>Stars списываются с твоего аккаунта, не с бота.</i>"
    )
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этому боту.")
        return
    await state.clear()
    linked = await sessions.is_linked(message.from_user.id)
    status = "✅ Аккаунт привязан" if linked else "❌ Аккаунт не привязан — /login"
    await message.answer(
        "🎁 <b>Gift Bot</b>\n\n"
        f"{status}\n\n"
        "Отправка Telegram-подарков с текстом, разбитым на части.\n\n"
        "<b>Команды:</b>\n"
        "/login — привязать аккаунт (api_id + api_hash)\n"
        "/account — статус аккаунта\n"
        "/gift — отправить подарки\n"
        "/logout — отвязать аккаунт\n"
        "/cancel — отменить текущее действие\n\n"
        "✨ <b>Premium emoji</b> — вставляй из клавиатуры Telegram при вводе текста.\n\n"
        "<i>Каждый пользователь использует свой Telegram и свои Stars.</i>",
    )


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = "Отменено."
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text)
    else:
        await event.answer(text)


@router.message(Command("gift"))
async def cmd_gift(message: Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not await _require_linked(message):
        return

    await state.clear()
    user_id = message.from_user.id
    try:
        catalog = await sessions.get_catalog(user_id)
        gifts = await catalog.list_gifts()
    except RuntimeError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_gifts failed")
        await message.answer(f"⚠️ Ошибка каталога:\n<code>{exc}</code>")
        return

    if not gifts:
        await message.answer("Каталог подарков пуст или недоступен.")
        return

    await state.set_state(GiftFlow.choose_gift)
    await message.answer(
        "Выбери подарок:",
        reply_markup=gifts_keyboard(gifts, page=0),
    )


@router.callback_query(GiftFlow.choose_gift, F.data.startswith("gifts_page:"))
async def on_gifts_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    catalog = await sessions.get_catalog(callback.from_user.id)
    gifts = await catalog.list_gifts()
    await callback.message.edit_reply_markup(reply_markup=gifts_keyboard(gifts, page=page))
    await callback.answer()


@router.callback_query(GiftFlow.choose_gift, F.data.startswith("gift:"))
async def on_gift_selected(callback: CallbackQuery, state: FSMContext) -> None:
    gift_id = int(callback.data.split(":")[1])
    catalog = await sessions.get_catalog(callback.from_user.id)
    gift = await catalog.get_gift(gift_id)
    if gift is None:
        await callback.answer("Подарок недоступен", show_alert=True)
        return

    await state.update_data(gift_id=gift.id, gift_stars=gift.stars, gift_title=gift.title, hide_name=False)
    await state.set_state(GiftFlow.choose_recipient)
    await callback.message.edit_text(
        f"Выбран: <b>{gift.title}</b> ({gift.stars}⭐)\n\n"
        "Кому отправить?\n"
        "• @username\n"
        "• t.me/username\n"
        "• или числовой id"
    )
    await callback.answer()


@router.message(GiftFlow.choose_recipient)
async def on_recipient(message: Message, state: FSMContext) -> None:
    recipient = message.text.strip().lstrip("@")
    if recipient.startswith("https://t.me/"):
        recipient = recipient.split("/")[-1]
    if not recipient:
        await message.answer("Укажи получателя (@username или id).")
        return

    await state.update_data(recipient=recipient, text_chunks=[])
    await state.set_state(GiftFlow.enter_text)
    await message.answer(
        "✍️ <b>Введи текст</b> для подарков.\n\n"
        "✨ <b>Premium emoji</b> — вставляй из клавиатуры Telegram прямо в текст.\n"
        "Можно несколькими сообщениями.\n\n"
        "Когда готов — /done"
    )


def _parts_from_state(data: dict) -> list:
    from core.entities import TextPart, entity_from_dict

    raw = data.get("parts") or []
    return [
        TextPart(
            text=p["text"],
            entities=[entity_from_dict(e) for e in p.get("entities", [])],
        )
        for p in raw
    ]


def _parts_to_state(parts) -> list[dict]:
    return [{"text": p.text, "entities": [entity_to_dict(e) for e in p.entities]} for p in parts]


def _confirm_text(data: dict, parts) -> str:
    gift_stars = data["gift_stars"]
    total = gift_stars * len(parts)
    hide = data.get("hide_name", False)
    emoji_total = sum(
        len([e for e in p.entities if e.type == "custom_emoji"]) for p in parts
    )
    emoji_line = f"Premium emoji: <b>{emoji_total}</b>\n" if emoji_total else ""
    return (
        f"📋 <b>Подтверждение</b>\n\n"
        f"Подарок: {data.get('gift_title')} ({gift_stars}⭐ × {len(parts)})\n"
        f"Получатель: @{data['recipient'].lstrip('@')}\n"
        f"Итого: <b>{total}⭐</b>\n"
        f"Скрыть имя: {'да' if hide else 'нет'}\n"
        f"{emoji_line}\n"
        f"Части:\n{format_preview(parts)}"
    )


@router.message(GiftFlow.enter_text, Command("done"))
async def on_text_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chunks = data.get("text_chunks") or []
    if not chunks:
        await message.answer("Сначала отправь хотя бы одно сообщение с текстом или emoji.")
        return
    await state.set_state(GiftFlow.enter_parts)
    emoji_n = total_custom_emoji(chunks)
    extra = f"\n✨ Premium emoji: {emoji_n}" if emoji_n else ""
    await message.answer(f"Текст принят.{extra}\n\nНа сколько частей разделить? (число, например 5)")


@router.message(GiftFlow.enter_text)
async def on_text(message: Message, state: FSMContext) -> None:
    chunk = chunk_from_message(message)
    if chunk is None:
        await message.answer(
            "Отправь текст или premium emoji.\n"
            "Когда закончишь — /done"
        )
        return
    text, entities = chunk
    if not text.strip() and not entities:
        await message.answer("Пустое сообщение. Добавь текст или premium emoji.")
        return

    data = await state.get_data()
    chunks = list(data.get("text_chunks") or [])
    chunks.append({"text": text, "entities": [entity_to_dict(e) for e in entities]})
    await state.update_data(text_chunks=chunks)

    emoji_n = total_custom_emoji(chunks)
    hint = f" (premium emoji: {emoji_n})" if emoji_n else ""
    await message.answer(f"➕ Добавлено{hint}. Ещё сообщения или /done")


@router.message(GiftFlow.enter_parts)
async def on_parts(message: Message, state: FSMContext) -> None:
    try:
        parts_count = int(message.text.strip())
        if parts_count < 1 or parts_count > 100:
            raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 100.")
        return

    data = await state.get_data()
    try:
        source = build_text_part(data.get("text_chunks"))
        parts = split_text_parts(source, parts_count)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    catalog = await sessions.get_catalog(message.from_user.id)
    gift = await catalog.get_gift(data["gift_id"])
    if gift is None:
        await message.answer("Подарок больше недоступен. /gift — заново.")
        await state.clear()
        return

    hide = data.get("hide_name", False)
    await state.update_data(parts=_parts_to_state(parts), parts_count=parts_count)
    await state.set_state(GiftFlow.confirm)
    await message.answer(
        _confirm_text({**data, "gift_title": gift.title}, parts),
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(GiftFlow.confirm, F.data == "toggle_hide_name")
async def toggle_hide_name(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    hide = not data.get("hide_name", False)
    await state.update_data(hide_name=hide)
    await callback.answer(f"Скрыть имя: {'да' if hide else 'нет'}")
    parts = _parts_from_state(data)
    await callback.message.edit_text(
        _confirm_text(data, parts),
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(GiftFlow.confirm, F.data == "confirm:yes")
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = callback.from_user.id
    catalog = await sessions.get_catalog(user_id)
    gift = await catalog.get_gift(data["gift_id"])
    if gift is None:
        await callback.answer("Подарок недоступен", show_alert=True)
        return

    plan = SendPlan(
        gift=gift,
        recipient=data["recipient"],
        parts=_parts_from_state(data),
        total_stars=gift.stars * len(data["parts"]),
        hide_name=data.get("hide_name", False),
    )

    await callback.message.edit_text("⏳ Отправляю подарки...")
    await callback.answer()

    try:
        sender = await sessions.get_sender(user_id)
        sent, errors = await sender.send_plan(plan)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_plan failed")
        await callback.message.edit_text(f"❌ Ошибка: {exc}")
        await state.clear()
        return

    await state.clear()
    if errors:
        err_text = "\n".join(errors[:5])
        await callback.message.edit_text(
            f"⚠️ Отправлено {sent}/{len(plan.parts)}.\n\nОшибки:\n{err_text}"
        )
    else:
        await callback.message.edit_text(
            f"✅ Готово! Отправлено {sent} подарков ({plan.total_stars}⭐)."
        )
