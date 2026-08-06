# Resolvate

Resolvate — система технической поддержки с Telegram и server-to-server Web API. Обращения из
бота и с сайта попадают в отдельные темы закрытой Forum-группы, где работают операторы. Сообщения,
фотографии, состояние тикетов и история действий сохраняются и переживают перезапуск.

> Версия: `v3.5.0`.

## Возможности

- один постоянный тикет и одна Forum-тема на идентичность в каждом канале;
- двусторонняя передача текста и Telegram-медиа;
- Web API для текста и одного фото, cursor polling ответов, close/rating/reopen;
- компактная статистика Telegram/Web в General topic;
- общая тема быстрых ответов с произвольными хештегами и нативным поиском Telegram;
- закрытие и повторное открытие обращений, оценки, блокировка и внутренние заметки;
- единый список администраторов с доступом ко всем командам;
- durable очередь доставки с повторами и восстановлением после сбоев;
- SQLite для простого запуска и PostgreSQL для production-инсталляции;
- независимые Web Support API и Operator API;
- healthcheck, readiness, Prometheus-метрики, JSON-логи и backup/restore;
- полноценная интеграция с Remnawave 2.8.x: подписка, продление, перевыпуск ссылки и ключей,
  сброс устройств и безопасное восстановление неизвестного результата;
- подписанный notification webhook с durable at-least-once доставкой.

## Как это работает

```text
Telegram-клиент ── Telegram bot ──┐
                                  ├── Resolvate + БД ── Forum-темы операторов
Backend сайта ───── Web API ──────┘
                                  │
                           Operator API (опционально)
```

Первое сообщение создаёт тикет и тему. Ответ из темы доставляется Telegram-клиенту либо становится
доступен backend сайта через polling. После закрытия новое сообщение повторно открывает тикет.

## Быстрый запуск

Понадобятся Docker Compose v2, Telegram-бот и приватная supergroup с включёнными Topics.

1. Клонируйте репозиторий и подготовьте конфигурацию:

   ```bash
   git clone https://github.com/Dever502/resolvate.git
   cd resolvate
   cp .env.example .env
   ```

2. Создайте бота через [@BotFather](https://t.me/BotFather), добавьте его администратором
   Forum-группы с правами управления темами и удаления сообщений, затем заполните `.env`:

   ```dotenv
   SUPPORT_BOT_TOKEN=replace-with-bot-token
   SUPPORT_GROUP_ID=replace-with-forum-group-id
   ADMIN_TELEGRAM_IDS=replace-with-admin-id
   DATA_DIR=./data
   ```

   Для поддержки сайта дополнительно включите `WEB_API_ENABLED`, задайте отдельный
   `WEB_API_TOKEN` и выберите `WEB_IDENTITY_MODE`. Браузер к этому API не обращается: запросы
   выполняет только backend сайта через HTTPS reverse proxy.

3. Запустите SQLite-вариант:

   ```bash
   ./scripts/start.sh sqlite
   ```

Скрипт скачивает image `v3.5.0`, закрепляет фактически полученный digest, проверяет Compose и ждёт
успешного healthcheck. Для PostgreSQL задайте в `.env` три разных пароля и запустите:

```bash
./scripts/start.sh postgres
```

Полная конфигурация PostgreSQL и разделение migration/runtime ролей описаны в
[руководстве по эксплуатации](docs/OPERATIONS.md).

Скрипт — только прозрачная обёртка. Прямой запуск Compose остаётся доступен:

```bash
export APP_IMAGE='ghcr.io/dever502/resolvate@sha256:<digest>'
export RESOLVATE_ENV_FILE="$PWD/.env"
docker compose --env-file .env -f compose.production.sqlite.yaml up --detach --wait
```

## Container image

Официальный способ поставки — container image в GHCR. `scripts/start.sh` автоматически разрешает
version tag в immutable digest. Для явной установки и rollback также можно передать digest:

```bash
./scripts/start.sh postgres 'ghcr.io/dever502/resolvate@sha256:<digest>'
```

PyPI-пакет и wheel для релиза не публикуются.

## Документация

- [Эксплуатация](docs/OPERATIONS.md) — настройка, PostgreSQL, deploy, backup/restore и диагностика.
- [Техническое устройство](docs/TECHNICAL.md) — архитектура, данные, очереди, API и интеграции.
- [Наблюдаемость](docs/OBSERVABILITY.md) — метрики, alerts и incident runbook.

## Лицензия

[MIT](LICENSE)
