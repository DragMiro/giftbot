from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.models import GiftInfo


def gifts_keyboard(gifts: list[GiftInfo], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = gifts[start : start + per_page]
    rows: list[list[InlineKeyboardButton]] = []

    for gift in chunk:
        label = f"{gift.emoji} {gift.stars}⭐"
        if gift.title:
            label = f"{gift.emoji} {gift.title} — {gift.stars}⭐"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"gift:{gift.id}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"gifts_page:{page - 1}"))
    if start + per_page < len(gifts):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"gifts_page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="confirm:yes"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
            ],
            [InlineKeyboardButton(text="👤 Скрыть имя", callback_data="toggle_hide_name")],
        ]
    )
