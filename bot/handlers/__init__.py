from aiogram import Router

from bot.handlers.auth import router as auth_router
from bot.handlers.gifts import router as gifts_router

router = Router()
router.include_router(auth_router)
router.include_router(gifts_router)
