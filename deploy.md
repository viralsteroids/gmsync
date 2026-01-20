# Документация по деплою приложения GMSync

Это приложение состоит из двух сервисов на Google App Engine:
1. **default** - синхронизация Exchange → Gmail
2. **checklistbot** - Telegram-бот для ежедневных чеклистов

## Предварительные требования

1. Установлен Google Cloud SDK (`gcloud`)
2. Настроен проект в Google Cloud Platform
3. Включен App Engine API для проекта
4. Настроена аутентификация: `gcloud auth login`

## Настройка конфигурации

### 1. Основной сервис синка (app.yaml)

Отредактируйте `app.yaml` и укажите реальные значения:

**GMAIL_TOKEN_JSON** - содержимое файла `token.json`, полученного через OAuth:
```yaml
GMAIL_TOKEN_JSON: |
  {
    "token": "ya29.xxx...",
    "refresh_token": "1//xxx...",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "xxx.apps.googleusercontent.com",
    "client_secret": "GOCSPX-xxx",
    "scopes": [...]
  }
```

**Настройки окна синка** (опционально):
- `IMPORT_LAST_DAYS` - окно для быстрого синка (по умолчанию 2 дня)
- `DEEP_IMPORT_LAST_DAYS` - окно для глубокого синка (по умолчанию 10 дней)
- `SYNC_GRACE_MINUTES` - запас времени для синка (по умолчанию 180 минут)

### 2. Сервис бота (checklistbot/app.yaml)

Отредактируйте `checklistbot/app.yaml`:

**BOT_TOKEN** - токен Telegram-бота от @BotFather:
```yaml
BOT_TOKEN: "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
```

**CHAT_ID** - ID группы/чата для отправки чеклистов:
- Получить можно командой `/getchatid` в группе после добавления бота
- Для групп обычно отрицательное число, например: `-635944095`

**APP_BASE_URL** - URL вашего приложения на GAE:
```yaml
APP_BASE_URL: "https://checklistbot-dot-ocrtest-339912.ew.r.appspot.com"
```
Замените `ocrtest-339912` на ваш project ID.

## Деплой

### Шаг 1: Деплой основного сервиса синка

```bash
cd C:\temp\GMSync
gcloud app deploy app.yaml --quiet
```

Это задеплоит сервис `default` с синхронизацией Exchange → Gmail.

### Шаг 2: Деплой сервиса бота

```bash
gcloud app deploy checklistbot/app.yaml --quiet
```

Это задеплоит сервис `checklistbot` с Telegram-ботом.

### Шаг 3: Настройка cron-задач

```bash
gcloud app deploy cron.yaml --quiet
```

Это настроит автоматические задачи:
- **Exchange → Gmail sync**: каждые 10 минут (быстрый синк)
- **Exchange → Gmail deep sync**: каждый час (глубокий синк)
- **Daily Telegram checklist**: каждый день в 01:00 (Europe/Tallinn)

### Шаг 4: Настройка webhook для Telegram-бота

После деплоя бота нужно настроить webhook:

1. Откройте в браузере:
   ```
   https://checklistbot-dot-<PROJECT_ID>.ew.r.appspot.com/telegram/set_webhook
   ```
   Замените `<PROJECT_ID>` на ваш project ID.

2. Должен вернуться ответ:
   ```json
   {"ok":true,"result":true,"description":"Webhook was set"}
   ```

## Проверка работы

### Проверка основного сервиса

1. **Проверка синка**:
   ```bash
   gcloud app logs read -s default --limit 20
   ```
   Ищите строки `=== run_sync_once: start ===` и `=== run_sync_once: end ===`

2. **Ручной запуск синка**:
   Откройте в браузере:
   ```
   https://<PROJECT_ID>.ew.r.appspot.com/tasks/sync
   ```

3. **Проверка глубокого синка**:
   ```
   https://<PROJECT_ID>.ew.r.appspot.com/tasks/sync_deep
   ```

### Проверка сервиса бота

1. **Проверка статуса**:
   ```bash
   gcloud app logs read -s checklistbot --limit 20
   ```

2. **Проверка конфигурации**:
   Откройте в браузере:
   ```
   https://checklistbot-dot-<PROJECT_ID>.ew.r.appspot.com/health
   ```
   Должен вернуться JSON с информацией о конфигурации.

3. **Информация о боте**:
   ```
   https://checklistbot-dot-<PROJECT_ID>.ew.r.appspot.com/telegram/bot_info
   ```

4. **Тестовая отправка чеклиста**:
   ```
   https://checklistbot-dot-<PROJECT_ID>.ew.r.appspot.com/tasks/daily_checklist
   ```

5. **В Telegram**:
   - Отправьте `/start` боту в группе
   - Отправьте `/now` для немедленной отправки чеклиста
   - Отправьте `/getchatid` для получения ID чата

## Полезные команды

### Просмотр логов

```bash
# Логи основного сервиса
gcloud app logs tail -s default

# Логи сервиса бота
gcloud app logs tail -s checklistbot

# Логи конкретной версии
gcloud app logs read -s default --version=<VERSION_ID> --limit 50
```

### Просмотр версий

```bash
# Список версий основного сервиса
gcloud app versions list -s default

# Список версий сервиса бота
gcloud app versions list -s checklistbot
```

### Просмотр cron-задач

В консоли Google Cloud:
```
https://console.cloud.google.com/appengine/taskqueues/cron?project=<PROJECT_ID>
```

### Открыть приложение в браузере

```bash
# Основной сервис
gcloud app browse

# Сервис бота
gcloud app browse -s checklistbot
```

## Структура проекта

```
GMSync/
├── app.yaml              # Конфигурация основного сервиса (default)
├── main.py               # Точка входа основного сервиса
├── sync.py               # Логика синхронизации Exchange → Gmail
├── requirements.txt      # Зависимости основного сервиса
├── cron.yaml             # Конфигурация cron-задач
├── checklistbot/
│   ├── app.yaml          # Конфигурация сервиса бота
│   ├── bot.py            # Код Telegram-бота
│   └── requirements.txt  # Зависимости бота
└── deploy.md             # Эта документация
```

## Обновление конфигурации

### Изменение переменных окружения

1. Отредактируйте соответствующий `app.yaml`
2. Задеплойте:
   ```bash
   gcloud app deploy app.yaml --quiet
   # или
   gcloud app deploy checklistbot/app.yaml --quiet
   ```

### Изменение расписания cron

1. Отредактируйте `cron.yaml`
2. Задеплойте:
   ```bash
   gcloud app deploy cron.yaml --quiet
   ```

## Устранение проблем

### Бот не отвечает

1. Проверьте, что webhook настроен: `/telegram/set_webhook`
2. Проверьте логи: `gcloud app logs read -s checklistbot`
3. Проверьте, что `BOT_TOKEN` и `CHAT_ID` указаны правильно

### Синк не работает

1. Проверьте логи: `gcloud app logs read -s default`
2. Проверьте, что `GMAIL_TOKEN_JSON` содержит валидный JSON
3. Убедитесь, что токен не истёк (он должен обновляться автоматически)

### Cron-задачи не запускаются

1. Проверьте конфигурацию в консоли Google Cloud
2. Убедитесь, что `cron.yaml` задеплоен: `gcloud app deploy cron.yaml`
3. Проверьте, что URL в cron.yaml соответствуют реальным эндпоинтам

## Безопасность

⚠️ **Важно**: Не коммитьте в git файлы с реальными токенами и паролями!

- Используйте `.gitignore` для исключения файлов с секретами
- Храните реальные значения только в `app.yaml` на сервере
- Используйте шаблоны (как сейчас) для версионирования

## Дополнительная информация

- [Документация Google App Engine](https://cloud.google.com/appengine/docs)
- [Документация Telegram Bot API](https://core.telegram.org/bots/api)
- [Документация Exchange Web Services](https://docs.microsoft.com/en-us/exchange/client-developer/exchange-web-services/ews-api-reference)
