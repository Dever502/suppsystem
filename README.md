# Telegram Support Platform

Suppsystem — Telegram-first система технической поддержки. Клиенты пишут боту, а операторы
обрабатывают обращения в отдельных темах закрытой Forum-группы. Сообщения, состояние тикетов и
история действий сохраняются в базе данных и переживают перезапуск приложения.

> Текущая версия — single-instance release candidate `v0.1.0`.

## Возможности

- один постоянный тикет и одна Forum-тема на клиента;
- двусторонняя передача текста и Telegram-медиа;
- закрытие и повторное открытие обращений, оценки, блокировка и внутренние заметки;
- роли администратора, оператора и оператора только для чтения;
- durable очередь доставки с повторами и восстановлением после сбоев;
- SQLite для простого запуска и PostgreSQL для production-инсталляции;
- опциональный Operator API;
- healthcheck, readiness, Prometheus-метрики, JSON-логи и backup/restore;
- экспериментальные интеграции Remnawave и notification webhook.

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

Понадобятся Docker с Compose, Telegram-бот и приватная supergroup с включёнными Topics.

1. Создайте бота через [@BotFather](https://t.me/BotFather).
2. Добавьте его администратором Forum-группы с правом управления темами.
3. Подготовьте конфигурацию:

   ```bash
   cp .env.example .env
   ```

   ```dotenv
   SUPPORT_BOT_TOKEN=replace-with-bot-token
   SUPPORT_GROUP_ID=replace-with-forum-group-id
   FULL_ADMIN_TELEGRAM_IDS=replace-with-admin-id
   DATA_DIR=./data
   ```

4. Соберите image и запустите SQLite-вариант:

   ```bash
   docker build --tag suppsystem:local .
   export APP_IMAGE=suppsystem:local
   export SUPPORTBOT_ENV_FILE="$PWD/.env"
   docker compose --env-file .env -f compose.production.sqlite.yaml up --detach
   ```

Для PostgreSQL добавьте пароли из руководства и используйте второй manifest:

```bash
docker compose --env-file .env -f compose.production.postgres.yaml up --detach
```

Полная конфигурация PostgreSQL и разделение migration/runtime ролей описаны в
[руководстве по эксплуатации](docs/OPERATIONS.md).

## Container image

Официальный способ поставки — container image в GHCR. Для установки и rollback используйте
immutable digest:

```bash
export APP_IMAGE='ghcr.io/dever502/suppsystem@sha256:<digest>'
export SUPPORTBOT_ENV_FILE="$PWD/.env"
docker compose --env-file .env -f compose.production.postgres.yaml pull
docker compose --env-file .env -f compose.production.postgres.yaml up --detach --no-build
```

PyPI-пакет и wheel для релиза не публикуются.

## Документация

- [Эксплуатация](docs/OPERATIONS.md) — настройка, PostgreSQL, deploy, backup/restore и диагностика.
- [Техническое устройство](docs/TECHNICAL.md) — архитектура, данные, очереди, API и интеграции.
- [Наблюдаемость](docs/OBSERVABILITY.md) — метрики, alerts и incident runbook.

## Ограничения

- Поддерживается один экземпляр приложения; горизонтальное масштабирование отсутствует.
- Telegram delivery использует at-least-once семантику, поэтому после аварии возможен редкий
  дубль сообщения.
- Operator API использует один статический административный токен и должен публиковаться только
  через HTTPS reverse proxy.
- Remnawave и notification webhook выключены по умолчанию и не входят в поддерживаемые гарантии
  текущего релиза.

## Разработка

Нужны Python 3.12+, [uv](https://docs.astral.sh/uv/) и Docker Compose для PostgreSQL-тестов.

```bash
uv sync --frozen --all-groups
make verify
make test-postgres
```

## Лицензия

[MIT](LICENSE)
