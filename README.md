# ⚽ Bet Bot v2

Telegram-бот для прогнозов на матчи: напоминания, авто-парсинг из Discord, ролевой доступ.

## Возможности

- 📥 **Ручное добавление** прогнозов через `/add`
- 🤖 **Авто-парсинг** прогнозов из Discord-канала
- 🔔 **Уведомления** за 30 и 5 минут до матча
- 🗑 **Автоудаление** через 5 минут после старта матча
- 🔒 **Авторизация по паролю** + whitelist пользователей
- 👥 **Роли**: админы добавляют/удаляют, юзеры только смотрят
- 💾 **Хранение в GitHub Gist** — данные не пропадают при редеплое

---

## Переменные окружения (Railway → Variables)

### Обязательные

| Переменная | Описание | Где взять |
|---|---|---|
| `BOT_TOKEN` | Токен TG-бота | [@BotFather](https://t.me/BotFather) |
| `ACCESS_PASSWORD` | Пароль для доступа | Придумай сам |
| `ADMIN_CHAT_IDS` | ID админов через запятую: `123,456,789` | [@userinfobot](https://t.me/userinfobot) |
| `GITHUB_TOKEN` | Токен GitHub с правом `gist` | [github.com/settings/tokens](https://github.com/settings/tokens) |
| `GIST_ID` | ID приватного Gist'а с `users.json` | URL твоего Gist |

### Опциональные (для авто-парсинга Discord)

| Переменная | Описание |
|---|---|
| `DISCORD_TOKEN` | Токен Discord-бота |
| `DISCORD_CHANNEL_ID` | ID текстового канала с прогнозами |
| `DISCORD_TARGET_TG_CHAT_ID` | TG chat_id куда пушить (личка или группа) |

### Дополнительные

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DEFAULT_TZ_OFFSET` | `3` | Часовой пояс UTC+N для новых пользователей |

---

## Настройка GitHub Gist (для хранения)

1. Открой https://gist.github.com
2. Filename: `users.json`, body: `{}`
3. Нажми **Create secret gist**
4. Скопируй ID из URL: `gist.github.com/user/<ID_ТУТ>`
5. Создай токен на https://github.com/settings/tokens (Classic) с правом `gist`
6. Добавь `GITHUB_TOKEN` и `GIST_ID` в Railway

Бот сам создаст файлы `predictions.json` и `settings.json` внутри этого Gist при первой записи.

---

## Команды

### Для всех авторизованных
- `/start` — начало работы / запрос пароля
- `/list` — список активных прогнозов

### Только для админов
- `/add` — добавить прогнозы
- `/delete <id>` — удалить один
- `/clear` — очистить все
- `/settings` — настройки (часовой пояс)
- `/users` — список авторизованных пользователей
- `/ban <user_id>` — забанить
- `/unban <user_id>` — разбанить
- `/remove <user_id>` — удалить из whitelist

---

## Работа в группе

1. Добавь бота в группу
2. BotFather → твой бот → Bot Settings → **Group Privacy → Turn off**
3. Сделай бота админом группы
4. Уведомления будут приходить в группу автоматически
5. Юзеры группы видят `/list`, добавляют только админы

---

## Discord setup

1. https://discord.com/developers/applications → New Application
2. Bot → Reset Token → скопировать
3. **Включи Message Content Intent!** (без него бот не видит текст)
4. OAuth2 → URL Generator → bot + права `View Channel`, `Read Message History`
5. Перейди по ссылке → добавь на сервер

---

## Формат прогнозов

Главное — наличие времени `HH-MM` или `HH:MM` в тексте. Несколько прогнозов отделяй пустой строкой:

```
Soccer. Brazil. Acreano U20. 2-00
Santa Cruz Acre U20 — Independencia FC U20
ф1-4,5

Soccer. Australia. 11-00
Hurstville U20 — Mariners U20 п2 4+
```

---

## Файловая структура

```
bot.py              ← точка входа
config.py           ← все переменные окружения
auth.py             ← авторизация
storage.py          ← прогнозы и настройки
gist_storage.py     ← бэкенд GitHub Gist (общий)
handlers.py         ← все команды TG
parser.py           ← разбор прогнозов
scheduler.py        ← напоминания + автоудаление
models.py           ← Prediction, UserSettings
discord_listener.py ← авто-парсинг из Discord
```
