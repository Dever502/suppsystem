# Эксплуатация

Краткое руководство по установке, обновлению и восстановлению. Архитектура описана в
[TECHNICAL.md](TECHNICAL.md), наблюдаемость — в [OBSERVABILITY.md](OBSERVABILITY.md).

## Требования и Telegram

- Linux, Docker Engine и Docker Compose v2;
- Telegram-бот и закрытая supergroup с включёнными Topics;
- числовые Telegram ID администраторов;
- HTTPS reverse proxy, если Operator API доступен извне.

Добавьте бота администратором Forum-группы с правом управления темами. При старте preflight
проверяет токен, тип группы, Topics и права бота; `SUPPORT_GROUP_ID` supergroup обычно начинается
с `-100`.

## Быстрый запуск

Скопируйте конфигурацию и заполните обязательные значения:

```bash
cp .env.example .env
```

```dotenv
SUPPORT_BOT_TOKEN=replace-with-bot-token
SUPPORT_GROUP_ID=replace-with-forum-group-id
ADMIN_TELEGRAM_IDS=replace-with-admin-id
DATA_DIR=./data
```

### SQLite

```bash
./scripts/start.sh sqlite
```

SQLite и heartbeat хранятся в volume `suppsystem_data`. Удаление контейнера данные не удаляет;
удаление volume — удаляет.

### PostgreSQL

Добавьте три разных URL-safe пароля длиной не менее 16 символов:

```dotenv
POSTGRES_DB=suppsystem
POSTGRES_ADMIN_USER=postgres
POSTGRES_ADMIN_PASSWORD=replace-with-random-password-1
POSTGRES_MIGRATION_USER=suppsystem_migrator
POSTGRES_MIGRATION_PASSWORD=replace-with-random-password-2
POSTGRES_RUNTIME_USER=suppsystem_runtime
POSTGRES_RUNTIME_PASSWORD=replace-with-random-password-3
```

```bash
./scripts/start.sh postgres
```

`postgres-provision` создаёт least-privilege роли, `postgres-migrate` применяет Alembic, затем
запускается приложение без migration credential. Поддерживается один экземпляр приложения.

`start.sh` скачивает release image, закрепляет его digest, проверяет Compose и ждёт healthcheck.
Другой image можно передать вторым аргументом.

## Конфигурация

Полный шаблон находится в [`.env.example`](../.env.example). Основные переменные:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `SUPPORT_BOT_TOKEN` | обязательна | токен Telegram-бота |
| `SUPPORT_GROUP_ID` | обязательна | ID закрытой Forum-группы |
| `ADMIN_TELEGRAM_IDS` | обязательна без API | ID администраторов через запятую; доступ ко всем командам |
| `DATA_DIR` | `./data` | SQLite и heartbeat-файлы |
| `DATABASE_URL` | SQLite в `DATA_DIR` | async SQLAlchemy URL |
| `MIGRATION_DATABASE_URL` | `DATABASE_URL` | отдельный URL для миграций |
| `MIGRATIONS_AT_STARTUP` | `true` | PostgreSQL Compose меняет на `false` |
| `LOG_LEVEL` | `INFO` | уровень логирования |
| `TELEGRAM_INBOUND_RATE_LIMIT_PER_MINUTE` | `30` | burst-лимит личных сообщений |
| `TELEGRAM_INBOUND_RATE_LIMIT_PER_HOUR` | `150` | длительный лимит на пользователя |

Лимит действует только на личные сообщения клиентов, не сокращает текст и сбрасывается при рестарте.
Интервалы доставки и предел повторов уже имеют безопасные значения. Не уменьшайте их без
измерений: это повышает риск Telegram rate limit и конкуренции SQLite.

### Operator API

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `API_ENABLED` | `false` | включает API |
| `API_HOST` | `0.0.0.0` | bind внутри контейнера |
| `API_PUBLISH_HOST` | `127.0.0.1` | публикация Docker-порта на host |
| `API_PORT` | `8080` | порт |
| `API_ADMIN_TOKEN` | пусто | `X-API-Token`, минимум 32 символа |
| `API_TRUSTED_PROXY_IPS` | пусто | доверенные proxy IP/CIDR |

Пустой `ADMIN_TELEGRAM_IDS` допустим только при `API_ENABLED=true`: в этом случае работа
операторов идёт только через API. Оставляйте публикацию на loopback и используйте HTTPS reverse
proxy. Каждая мутация требует
`X-Idempotency-Key`: точный повтор возвращает сохранённый ответ, другой payload с тем же ключом —
`409 Conflict`. Rate limit и защита токена process-local и сбрасываются при рестарте.

Пример reverse proxy: [`deploy/nginx/suppsystem-api.conf.example`](../deploy/nginx/suppsystem-api.conf.example).

### Remnawave и webhook

Поддерживается Remnawave 2.8.x. Обе интеграции опциональны.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `REMNAWAVE_ENABLED` | `false` | включает Remnawave |
| `REMNAWAVE_BASE_URL` | пусто | HTTPS origin панели без `/api` |
| `REMNAWAVE_API_TOKEN` | пусто | API token, минимум 32 символа |
| `REMNAWAVE_RECONCILE_DELAY_SECONDS` | `10` | задержка перед сверкой timeout |
| `REMNAWAVE_REVOKE_LINK_TELEGRAM_NOTIFICATION` | `true` | отправлять новую ссылку клиенту |
| `NOTIFICATION_WEBHOOK_ENABLED` | `false` | включает webhook-доставку |
| `NOTIFICATION_WEBHOOK_URL` | пусто | HTTPS endpoint |
| `NOTIFICATION_WEBHOOK_SECRET` | пусто | HMAC secret, минимум 32 символа |

`/revokelink` всегда создаёт webhook-событие; флаг управляет только сообщением в Telegram.
Фактическая webhook-доставка требует `NOTIFICATION_WEBHOOK_ENABLED=true`. Получатель проверяет
HMAC-SHA256 и дедуплицирует эффект по `event_id`.

## Команды администраторов

Команды выполняются в связанной Forum-теме. Неизвестные команды клиенту не пересылаются.

| Команда | Действие |
| --- | --- |
| `/info` | локальная информация о тикете |
| `/subinfo` | свежие данные подписки Remnawave |
| `/notes` | последние внутренние заметки |
| `/note текст` | добавить заметку |
| `/stop` | закрыть тикет и уведомить клиента |
| `/hidestop` | закрыть без уведомления |
| `/gift N` | продлить подписку на 1–9999 дней и уведомить клиента |
| `/resetkey` | заменить protocol credentials |
| `/revokelink` | заменить subscription URL и уведомить по настройкам |
| `/resetdevices` | удалить HWID-устройства |
| `/resolvepanel UUID applied\|not_applied` | разрешить проверенный `inconclusive` |
| `/block`, `/unblock` | изменить блокировку клиента |
| `/closeall` | закрыть все открытые тикеты |
| `/synctopics` | синхронизировать Forum-темы |

Обычный ответ в теме отправляется клиенту и при необходимости заново открывает тикет.

## Production

Рекомендуемая схема: конфигурация в `/opt/suppsystem`, PostgreSQL и heartbeat в Docker volumes,
Operator API на loopback. `.env` хранит секреты с правами `0600`; `deployment.env` и
`rollback.env` содержат текущий и предыдущий immutable image.

### Deploy и rollback

Значения `registry.example/...` ниже — placeholders. Deploy принимает tag или digest,
но сохраняет и запускает только фактически полученный RepoDigest.

```bash
DEPLOY_DIR=/opt/suppsystem \
  sh scripts/deploy.sh deploy registry.example/suppsystem:v2.0.0
DEPLOY_DIR=/opt/suppsystem sh scripts/production-compose.sh ps
```

```bash
DEPLOY_DIR=/opt/suppsystem sh scripts/deploy.sh rollback
```

Rollback разрешён только на ранее успешно запущенный image. Скрипт не выполняет downgrade схемы:
сначала подтвердите совместимость миграций со старой версией.

Повседневные команды:

```bash
DEPLOY_DIR=/opt/suppsystem sh scripts/production-compose.sh ps
DEPLOY_DIR=/opt/suppsystem sh scripts/production-compose.sh logs -f --tail=200 suppsystem
DEPLOY_DIR=/opt/suppsystem sh scripts/production-compose.sh restart suppsystem
```

GitHub Actions выполняет verify и PostgreSQL matrix. Push в `main` или release tag публикует GHCR
image, Trivy report, CycloneDX SBOM, checksums и release evidence. Workflow не имеет доступа к
production host; deploy остаётся ручной операцией.

### Health и логи

```bash
sh scripts/production-compose.sh ps
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/health
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/ready
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/metrics
```

HTTP endpoints доступны при включённом API. `/health` проверяет процесс, `/ready` — базу и
настроенные компоненты, `/metrics` отдаёт Prometheus-метрики. Логи — JSON; основные поля:
`event`, `trace_id`, `ticket_id`, `delivery_id`, `operator_action_id`.

## Backup и восстановление

Храните зашифрованные копии вне Docker volumes и регулярно проверяйте restore на отдельном
стенде. Backup содержит пользовательские данные.

### SQLite

Перед запуском экспортируйте `APP_IMAGE` и `SUPPSYSTEM_ENV_FILE` так же, как для Compose.

```bash
COMPOSE_FILE=compose.production.sqlite.yaml \
  ./scripts/backup.sh sqlite /srv/backups/support-$(date +%F-%H%M).db
CONFIRM_RESTORE=yes COMPOSE_FILE=compose.production.sqlite.yaml \
  ./scripts/restore.sh sqlite /srv/backups/support-backup.db
```

Backup не останавливает приложение. Restore проверяет `PRAGMA integrity_check`, останавливает
приложение и атомарно заменяет БД; после ошибки приложение остаётся остановленным.

### PostgreSQL

```bash
PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/suppsystem \
  sh scripts/backup.sh postgres /srv/backups/support-$(date +%F-%H%M).dump
CONFIRM_RESTORE=yes PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/suppsystem \
  sh scripts/restore.sh postgres /srv/backups/support-backup.dump
```

Archive проверяется до остановки. Restore работает через migration role, повторно применяет
миграции и ждёт healthcheck. После ошибки проверьте БД и запускайте приложение вручную.

Перед обновлением всегда создавайте backup. Не запускайте старый image поверх новой схемы без
явно поддерживаемого downgrade.

`scripts/drill_production_data_path.sh` проверяет deploy, rollback, backup и restore только на
изолированном стенде. Его отчёты и дампы могут содержать чувствительные данные.

## Разработка

```bash
make install
make verify
make test-postgres
```

Изменение ORM требует новой Alembic-миграции и проверки fresh install и upgrade. Применённые
миграции не редактируются задним числом.

## Диагностика

| Проблема | Что проверить |
| --- | --- |
| приложение не запускается | `scripts/production-compose.sh logs`, `.env`, Telegram и БД |
| Telegram preflight | supergroup, Topics, присутствие и права бота, соответствие токена |
| сообщения не доставляются | health workers, delivery events и задания со статусом `failed` |
| `/ready` возвращает 503 | компонент из ответа: database, panel, delivery или notification worker |

### Remnawave: `unknown outcome`

Не повторяйте мутацию. Найдите `operator_action_id` и дождитесь `completed`, `not_applied` или
`inconclusive`; до этого новые мутации тикета заблокированы. Для `inconclusive` независимо
установите результат исходного вызова и выполните в той же теме:

```text
/resolvepanel <operator_action_uuid> applied
/resolvepanel <operator_action_uuid> not_applied
```

Текущего состояния Remnawave недостаточно: без независимого доказательства оставьте действие
заблокированным. Команда не повторяет мутацию и сохраняет решение в аудите.

## Production checklist

- `.env` имеет права `0600` и не хранится в Git;
- секреты уникальны, PostgreSQL использует три разных пароля;
- API опубликован только через HTTPS reverse proxy;
- настроены логи, метрики и alerts;
- backup хранится отдельно и restore регулярно проверяется;
- после обновления проверены readiness, логи и тестовый тикет.
