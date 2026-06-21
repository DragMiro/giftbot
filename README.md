# GiftSender 🎁

Модуль для **Hikka**, **Heroku** и других Telethon-userbot'ов.  
Отправляет **Telegram Gifts** (Stars) с текстом, разбитым на части — с поддержкой **premium emoji**.

> Stars списываются с **твоего** аккаунта userbot'а.

---

## Быстрый старт (Hikka / Heroku)

### 1. Склонируй репозиторий на сервер / VPS

```bash
git clone https://github.com/DragMiro/giftbot.git
cd giftbot
pip install -r requirements-hikka.txt
```

### 2. Загрузи модуль

**Hikka:**
```text
.loadmod /path/to/giftbot/GiftSender.py
```

**Heroku** (и форки):
```text
.loadmod GiftSender.py
```
*(если репозиторий уже лежит в папке модулей)*

**Через репозиторий (Hikka / Heroku):**
```text
.addrepo https://raw.githubusercontent.com/DragMiro/giftbot/main
.dlm GiftSender
```

Или одной командой:
```text
.dlm https://raw.githubusercontent.com/DragMiro/giftbot/main/GiftSender.py
```

> `GiftSender.py` — один самодостаточный файл, без загрузок из интернета.

### 3. Используй

```text
.gift          — мастер отправки
.giftcancel    — отмена
.giftdone      — завершить ввод текста (или /done)
```

---

## CursorAgent 🤖

Модуль с [Cursor SDK](https://cursor.com/docs/sdk/python) — AI-агент из лички userbot'а.

```text
.dlm https://raw.githubusercontent.com/DragMiro/giftbot/main/CursorAgent.py
```

Настройка:

```text
.cfg CursorAgent
```

Поле **`cursor_api_key`**:

1. Открой [cursor.com/dashboard/integrations](https://cursor.com/dashboard/integrations)
2. **API Keys** → **Create** (или Add)
3. Скопируй ключ формата `crsr_...` (показывается один раз)
4. Вставь в `.cfg CursorAgent` → `cursor_api_key`

> Не публикуй ключ в чатах и GitHub. Если утёк — удали и создай новый в Integrations.
- `repo_url` / `repo_branch` — репозиторий для cloud-агента (по умолчанию giftbot)

Команды:

```text
.cursor <вопрос>   — один запрос
.cursorchat        — диалог (пиши сообщения, .cursorstop — выход)
.cursorstop        — завершить диалог
.afkcursor         — AFK: ИИ-менеджер отвечает в личку за вас
.afkcursor off     — выключить AFK
```

---

## Поток `.gift`

| Шаг | Действие |
|-----|----------|
| 1 | Выбор подарка (номер из списка) |
| 2 | Получатель `@username` / id |
| 3 | Текст + **premium emoji** (можно несколькими сообщениями) |
| 4 | `/done` или `.giftdone` |
| 5 | Число частей |
| 6 | Подтверждение `да` / `нет` |
| 7 | Отправка |

---

## Premium emoji ✨

Вставляй emoji из клавиатуры Telegram прямо в текст.  
Модуль сохраняет `MessageEntityCustomEmoji` и прикрепляет к каждому подарку.

---

## Настройки модуля

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `send_delay` | `2.0` | Пауза между подарками (сек) |

---

## Структура репозитория

```
giftbot/
├── GiftSender.py      ← главный модуль для Hikka / Heroku
├── core/              ← логика подарков, emoji, разбиение текста
├── bot/               ← опциональный Telegram-бот (aiogram)
├── docs/
│   ├── HIKKA.md
│   └── BOT.md
├── requirements-hikka.txt
└── requirements.txt
```

---

## Опционально: Telegram-бот

Multi-user бот с `/login` и кнопками — см. [docs/BOT.md](docs/BOT.md).

---

## Требования

- Python 3.10+
- Telethon ≥ 1.38
- Telegram Stars на аккаунте userbot'а
- Hikka / Heroku / любой loader с `@loader.command`

---

## Лицензия

MIT — см. [LICENSE](LICENSE)
