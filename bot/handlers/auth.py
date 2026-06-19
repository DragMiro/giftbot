"""Привязка Telegram-аккаунта: api_id, api_hash, телефон, код."""

from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.session_manager import sessions
from bot.states import AuthFlow
from config import get_settings
from storage import get_user, remove_user

logger = logging.getLogger(__name__)
router = Router()

_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuthFlow.api_id)
    await message.answer(
        "🔐 <b>Привязка аккаунта</b>\n\n"
        "Подарки отправляются с <b>твоего</b> Telegram — Stars спишутся с тебя.\n\n"
        "Шаг 1/4 — введи <b>api_id</b> (число с https://my.telegram.org):\n\n"
        "<i>Как получить:</i>\n"
        "1. my.telegram.org → войти\n"
        "2. API development tools → Create application\n"
        "3. Скопировать App api_id",
    )


@router.message(AuthFlow.api_id)
async def on_api_id(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    try:
        api_id = int(text)
        if api_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("api_id — это число. Попробуй ещё раз:")
        return

    await state.update_data(api_id=api_id)
    await state.set_state(AuthFlow.api_hash)
    await message.answer(
        f"✅ api_id: <code>{api_id}</code>\n\n"
        "Шаг 2/4 — введи <b>api_hash</b> (строка с my.telegram.org):"
    )


@router.message(AuthFlow.api_hash)
async def on_api_hash(message: Message, state: FSMContext) -> None:
    api_hash = message.text.strip()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    if len(api_hash) < 16 or not api_hash.isalnum():
        await message.answer("api_hash выглядит некорректно. Вставь строку с my.telegram.org:")
        return

    await state.update_data(api_hash=api_hash)
    await state.set_state(AuthFlow.phone)
    await message.answer(
        "✅ api_hash сохранён.\n\n"
        "Шаг 3/4 — номер телефона аккаунта Telegram\n"
        "Формат: <code>+79991234567</code>"
    )


@router.message(AuthFlow.phone)
async def on_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(phone):
        await message.answer("Неверный формат. Пример: <code>+79991234567</code>")
        return
    if not phone.startswith("+"):
        phone = f"+{phone}"

    data = await state.get_data()
    try:
        await sessions.start_login(
            message.from_user.id,
            data["api_id"],
            data["api_hash"],
            phone,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("start_login failed")
        await sessions.cancel_login(message.from_user.id)
        await message.answer(f"❌ Не удалось отправить код:\n<code>{exc}</code>")
        await state.clear()
        return

    await state.update_data(phone=phone)
    await state.set_state(AuthFlow.code)
    await message.answer(
        f"📱 Код отправлен на <code>{phone}</code>\n\n"
        "Шаг 4/4 — введи код из Telegram:"
    )


@router.message(AuthFlow.code)
async def on_code(message: Message, state: FSMContext) -> None:
    code = message.text.strip().replace(" ", "").replace("-", "")
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    if not code.isdigit():
        await message.answer("Код — только цифры. Попробуй ещё раз:")
        return

    data = await state.get_data()
    try:
        need_password = await sessions.confirm_code(
            message.from_user.id,
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            phone=data["phone"],
            code=code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_code failed")
        await sessions.cancel_login(message.from_user.id)
        await message.answer(f"❌ Ошибка входа:\n<code>{exc}</code>\n\n/login — заново")
        await state.clear()
        return

    if need_password == "password":
        await state.set_state(AuthFlow.password)
        await message.answer("🔒 Включена 2FA. Введи пароль облачного пароля Telegram:")
        return

    await state.clear()
    account = await get_user(message.from_user.id)
    name = account.tg_first_name if account else "аккаунт"
    username = f"@{account.tg_username}" if account and account.tg_username else ""
    await message.answer(
        f"✅ Аккаунт привязан!\n\n"
        f"👤 {name} {username}\n\n"
        "Теперь можно отправлять подарки: /gift"
    )


@router.message(AuthFlow.password)
async def on_password(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    try:
        await sessions.confirm_password(
            message.from_user.id,
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            phone=data["phone"],
            password=password,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_password failed")
        await sessions.cancel_login(message.from_user.id)
        await message.answer(f"❌ Неверный пароль или ошибка:\n<code>{exc}</code>")
        await state.clear()
        return

    await state.clear()
    account = await get_user(message.from_user.id)
    username = f"@{account.tg_username}" if account and account.tg_username else ""
    await message.answer(f"✅ Аккаунт привязан! {username}\n\n/gift — отправить подарки")


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    await sessions.logout(message.from_user.id)
    await remove_user(message.from_user.id)
    await message.answer("🔓 Аккаунт отвязан. Сессия удалена.\n\n/login — привязать снова")


@router.message(Command("account"))
async def cmd_account(message: Message) -> None:
    account = await get_user(message.from_user.id)
    linked = await sessions.is_linked(message.from_user.id)

    if not account or not linked:
        await message.answer(
            "❌ Аккаунт не привязан.\n\n"
            "/login — привязать Telegram (нужны api_id и api_hash с my.telegram.org)"
        )
        return

    username = f"@{account.tg_username}" if account.tg_username else "—"
    await message.answer(
        "👤 <b>Твой аккаунт</b>\n\n"
        f"Telegram: {account.tg_first_name} ({username})\n"
        f"api_id: <code>{account.api_id}</code>\n"
        f"Телефон: <code>{account.phone or '—'}</code>\n"
        f"Привязан: {account.authorized_at or '—'}\n\n"
        "/logout — отвязать\n"
        "/gift — отправить подарки"
    )
