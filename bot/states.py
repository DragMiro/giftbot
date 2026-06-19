"""FSM-состояния для aiogram-бота."""

from aiogram.fsm.state import State, StatesGroup


class AuthFlow(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class GiftFlow(StatesGroup):
    choose_gift = State()
    choose_recipient = State()
    enter_text = State()
    enter_parts = State()
    confirm = State()
