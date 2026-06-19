# Telegram-бот (опционально)

Multi-user бот на **aiogram 3** — каждый пользователь привязывает свой аккаунт через `/login`.

Основной сценарий проекта — **модуль GiftSender.py** для Hikka/Heroku.  
Бот нужен, если хочешь UI с кнопками без userbot у каждого юзера.

## Запуск

```bash
cp .env.example .env
# BOT_TOKEN, ENCRYPTION_KEY
pip install -r requirements.txt
python main.py
```

## systemd

```bash
sudo cp deploy/giftbot.service /etc/systemd/system/
sudo systemctl enable --now giftbot
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/login` | api_id + api_hash + телефон |
| `/gift` | Мастер с кнопками |
| `/logout` | Отвязать аккаунт |

Секреты (`.env`, `data/`) не коммитятся в git.
