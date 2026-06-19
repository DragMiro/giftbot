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

## addrepo (Hikka)

Если поддерживается добавление репозитория:

```text
.addrepo https://github.com/DragMiro/giftbot
.dlmod GiftSender
```

---

## Частые проблемы

| Ошибка | Решение |
|--------|---------|
| `No module named 'core'` | Загрузи модуль из **полного clone**, не один файл |
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
