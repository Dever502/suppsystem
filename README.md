# Suppsystem

Suppsystem — Telegram-first система технической поддержки. Клиенты пишут боту, а операторы
обрабатывают обращения в отдельных темах закрытой Forum-группы. Сообщения, состояние тикетов и
история действий сохраняются в базе данных и переживают перезапуск приложения.

> Версия: `v1.0.0`.

## Возможности

- один постоянный тикет и одна Forum-тема на клиента;
- двусторонняя передача текста и Telegram-медиа;
- закрытие и повторное открытие обращений, оценки, блокировка и внутренние заметки;
- единый список администраторов с доступом ко всем командам;
- durable очередь доставки с повторами и восстановлением после сбоев;
- SQLite для простого запуска и PostgreSQL для production-инсталляции;
- опциональный Operator API;
- healthcheck, readiness, Prometheus-метрики, JSON-логи и backup/restore;
- полноценная интеграция с Remnawave 2.8.x: подписка, продление, перевыпуск ссылки и ключей,
  сброс устройств и безопасное восстановление неизвестного результата;
- подписанный notification webhook с durable at-least-once доставкой.

## Как это работает

```text
Клиент                    Suppsystem                   Операторы
Telegram-чат  <──>  Telegram-бот + база данных  <──>  Forum-тема
                              │
                       Operator API
                       (опционально)
```

Первое сообщение создаёт тикет и тему. Ответ оператора из темы доставляется клиенту. После
закрытия история сохраняется, а новое сообщение повторно открывает тот же тикет.

## Быстрый запуск

Понадобятся Docker Compose v2, Telegram-бот и приватная supergroup с включёнными Topics.

1. Клонируйте репозиторий и подготовьте конфигурацию:

   ```bash
   git clone https://github.com/Dever502/suppsystem.git
   cd suppsystem
   cp .env.example .env
   ```

2. Создайте бота через [@BotFather](https://t.me/BotFather), добавьте его администратором
   Forum-группы с правом управления темами и заполните `.env`:

   ```dotenv
   SUPPORT_BOT_TOKEN=replace-with-bot-token
   SUPPORT_GROUP_ID=replace-with-forum-group-id
   ADMIN_TELEGRAM_IDS=replace-with-admin-id
   DATA_DIR=./data
   ```

3. Запустите SQLite-вариант:

   ```bash
   ./scripts/start.sh sqlite
   ```

Скрипт скачивает image `v1.0.0`, закрепляет фактически полученный digest, проверяет Compose и ждёт
успешного healthcheck. Для PostgreSQL задайте в `.env` три разных пароля и запустите:

```bash
./scripts/start.sh postgres
```

Полная конфигурация PostgreSQL и разделение migration/runtime ролей описаны в
[руководстве по эксплуатации](docs/OPERATIONS.md).

Скрипт — только прозрачная обёртка. Прямой запуск Compose остаётся доступен:

```bash
export APP_IMAGE='ghcr.io/dever502/suppsystem@sha256:<digest>'
export SUPPSYSTEM_ENV_FILE="$PWD/.env"
docker compose --env-file .env -f compose.production.sqlite.yaml up --detach --wait
```

## Container image

Официальный способ поставки — container image в GHCR. `scripts/start.sh` автоматически разрешает
version tag в immutable digest. Для явной установки и rollback также можно передать digest:

```bash
./scripts/start.sh postgres 'ghcr.io/dever502/suppsystem@sha256:<digest>'
```

PyPI-пакет и wheel для релиза не публикуются.

## Документация

- [Эксплуатация](docs/OPERATIONS.md) — настройка, PostgreSQL, deploy, backup/restore и диагностика.
- [Техническое устройство](docs/TECHNICAL.md) — архитектура, данные, очереди, API и интеграции.
- [Наблюдаемость](docs/OBSERVABILITY.md) — метрики, alerts и incident runbook.

## Лицензия

[MIT](LICENSE)
