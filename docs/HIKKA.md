# Hikka / Heroku — установка GiftSender

## Hikka

1. SSH на сервер с Hikka
2. Клонируй репо в удобное место:

```bash
git clone https://github.com/DragMiro/giftbot.git ~/giftbot
```

3. В Telegram (личка с userbot):

```text
.loadmod /root/giftbot/GiftSender.py
```

4. Проверка:

```text
.gift
```

### Обновление

```bash
cd ~/giftbot && git pull
```

Перезагрузи модуль:

```text
.reload GiftSender
```

---

## Heroku (и аналоги)

Heroku использует тот же формат модулей, что Hikka.

1. Положи репозиторий на сервер (git clone)
2. `.loadmod /path/to/giftbot/GiftSender.py`
3. `.gift`

---

## addrepo (Hikka / Heroku)

```text
.addrepo https://raw.githubusercontent.com/DragMiro/giftbot/main
.dlm GiftSender
```

Или без addrepo:

```text
.dlm https://raw.githubusercontent.com/DragMiro/giftbot/main/GiftSender.py
```

> URL должен быть `raw.githubusercontent.com`, не `github.com/...` — иначе Heroku не сможет разобрать ссылку.  
> Модуль — один файл, ничего не качает из сети при загрузке.

---

## Частые проблемы

| Ошибка | Решение |
|--------|---------|
| `expected 4, got 2` при `.dlm` | Используй `raw.githubusercontent.com`, не `github.com/DragMiro/giftbot` |
| `No module named 'core'` | Обнови модуль: `.dlm GiftSender` или `.loadmod` из полного clone |
| `Loading failed` | `.logs`. Удали старый `cursor_ai.py` (`.ulm cursor_ai`), поставь `_cursor_ai.py`, обнови GiftSender v1.2.2+ |
| `BALANCE_TOO_LOW` | Пополни Stars на аккаунте userbot |
| Premium emoji не отображаются | Вставляй emoji в текст сообщения userbot'у, не plain text |

---

## Команды

| Команда | Описание |
|---------|----------|
| `.gift` | Начать мастер |
| `.giftcancel` | Отмена |
| `.giftdone` | Закончить ввод текста |
| `/done` | То же в шаге текста |

---

## Cursor + GiftSender (поиск песен)

**Не используй raw URL для `_cursor_ai`** — только через addrepo:

```text
.addrepo https://raw.githubusercontent.com/DragMiro/giftbot/main
.dlm _cursor_ai
.dlm GiftSender
.dlm CursorAgent
.pip install cursor-sdk httpx
.restart -f
```

Если уже ошибся с URL и в логах `has no attribute 'register'`:

```text
.ulm cursor_ai
.ulm _cursor_ai
.restart -f
```

Потом снова через `.addrepo` (см. выше).

> `_cursor_ai` — библиотека. В списке модулей висит пустышка — это нормально.

---

```text
.dlm https://raw.githubusercontent.com/DragMiro/giftbot/main/CursorAgent.py
```

### API key

1. [cursor.com/dashboard/integrations](https://cursor.com/dashboard/integrations)
2. **API Keys** → создать ключ (`crsr_...`)
3. В Telegram:

```text
.cfg CursorAgent
```

→ поле **`cursor_api_key`**

### Команды

| Команда | Описание |
|---------|----------|
| `.cursor <вопрос>` | Один запрос |
| `.cursorchat` | Диалог |
| `.cursorstop` | Выход из диалога |
