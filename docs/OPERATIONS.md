# Эксплуатация

Краткое руководство по установке, обновлению и восстановлению. Архитектура описана в
[TECHNICAL.md](TECHNICAL.md), наблюдаемость — в [OBSERVABILITY.md](OBSERVABILITY.md).

## Требования и Telegram

- Linux, Docker Engine и Docker Compose v2;
- Telegram-бот и закрытая supergroup с включёнными Topics;
- числовые Telegram ID администраторов;
- HTTPS reverse proxy, если Operator API или Web API доступны извне.

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

Имена ролей фиксированы: `postgres`, `suppsystem_migrator` и `suppsystem_runtime`.

```dotenv
POSTGRES_DB=suppsystem
POSTGRES_ADMIN_PASSWORD=replace-with-random-password-1
POSTGRES_MIGRATION_PASSWORD=replace-with-random-password-2
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
| `DATA_DIR` | `./data` | SQLite, Web-фото и heartbeat-файлы |
| `DATABASE_URL` | SQLite в `DATA_DIR` | async SQLAlchemy URL |
| `MIGRATION_DATABASE_URL` | `DATABASE_URL` | отдельный URL для миграций |
| `MIGRATIONS_AT_STARTUP` | `true` | PostgreSQL Compose меняет на `false` |
| `LOG_LEVEL` | `INFO` | уровень логирования |
| `USER_MESSAGES_PER_MINUTE` | `30` | burst-лимит сообщений одного клиента |
| `USER_MESSAGES_PER_HOUR` | `200` | часовой лимит сообщений одного клиента |
| `API_REQUESTS_PER_MINUTE` | `6000` | минутная квота отдельно для каждого API token |

Пользовательский лимит одинаков для Telegram и Web: в Telegram клиент определяется по ID,
в Web — по выбранному каноническому идентификатору. Текст и фото считаются одним сообщением;
разные клиенты не делят квоту. API-квота применяется отдельно к каждому валидному API token.
Все лимиты process-local и сбрасываются при рестарте.

### Operator API

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `API_ENABLED` | `false` | включает API |
| `API_ADMIN_TOKEN` | пусто | `X-API-Token`, минимум 32 символа |

По умолчанию Compose публикует API на `127.0.0.1:8080`. `API_HOST`, `API_PUBLISH_HOST`,
`API_PORT` и `API_TRUSTED_PROXY_IPS` остаются расширенными настройками.

Пустой `ADMIN_TELEGRAM_IDS` допустим только при `API_ENABLED=true`: в этом случае работа
операторов идёт только через API. Оставляйте публикацию на loopback и используйте HTTPS reverse
proxy. Каждая мутация требует
`X-Idempotency-Key`: точный повтор возвращает сохранённый ответ, другой payload с тем же ключом —
`409 Conflict`. API-квота считается отдельно для Operator token; строгая защита от перебора
token остаётся по IP.

Пример reverse proxy: [`deploy/nginx/suppsystem-api.conf.example`](../deploy/nginx/suppsystem-api.conf.example).

### Web Support API

Web API предназначен только для backend сайта. Не передавайте token в браузер и не включайте
CORS. Он использует тот же bind/port, но отдельные credential и API-квоту:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `WEB_API_ENABLED` | `false` | включает `/api/v1/web` |
| `WEB_API_TOKEN` | пусто | отдельный `X-API-Token`, минимум 32 символа |
| `WEB_IDENTITY_MODE` | `external_id` | `external_id` или `email` |

`WEB_API_TOKEN` не может совпадать с `API_ADMIN_TOKEN`. Рекомендуемый режим `external_id`
использует стабильный ID аккаунта сайта, email остаётся изменяемым атрибутом. В режиме `email`
смена email создаёт нового клиента. После первого Web-обращения режим фиксируется в БД.

Все мутации требуют `X-Idempotency-Key`. Текст принимается как JSON (до 4096 символов); текст с
одним JPEG/PNG/WebP до 10 MiB — как multipart, при этом подпись ограничена 1024 символами.
Пример Nginx допускает 11 MiB на весь multipart-запрос. Минимальный запрос:

```bash
curl -X POST https://support.example.com/api/v1/web/messages \
  -H "X-API-Token: $WEB_API_TOKEN" \
  -H "X-Idempotency-Key: message-account-42-0001" \
  -H 'Content-Type: application/json' \
  -d '{"external_user_id":"account-42","email":"user@example.com","text":"Нужна помощь"}'
```

Backend сохраняет `conversation_id` и `next_cursor`, затем каждые 2–5 секунд читает:

```text
GET  /api/v1/web/conversations/{id}
GET  /api/v1/web/conversations/{id}/messages?after={cursor}
POST /api/v1/web/conversations/{id}/close
POST /api/v1/web/conversations/{id}/rating
POST /api/v1/web/conversations/{id}/block
POST /api/v1/web/conversations/{id}/unblock
GET  /api/v1/web/media/{media_id}
```

Повтор cursor безопасен; порядок стабилен по `(created_at, id)`. Блокировка не раскрывается
клиенту: входящие сообщения и оценки принимаются обычным ответом и остаются в клиентской истории,
но помечаются как подавленные. Они не видны в Operator API и статистике, не доставляются оператору
и не переоткрывают тикет. Ответы оператора, служебные сообщения и пользовательские Remnawave-
уведомления для заблокированного тикета также не ставятся в доставку; точный retry сохраняет этот
результат. В первой версии нет SSE, WebSocket, delivery webhook и browser widget. По принятому
privacy trade-off JSON-логи содержат email и полный текст Web-сообщений: ограничьте доступ и
retention.

### Remnawave и webhook

Поддерживается Remnawave 2.8.x. Обе интеграции опциональны.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `REMNAWAVE_ENABLED` | `false` | включает Remnawave |
| `REMNAWAVE_BASE_URL` | пусто | HTTPS origin панели без `/api` |
| `REMNAWAVE_API_TOKEN` | пусто | API token, минимум 32 символа |
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

### Готовые ответы

До запуска один раз включите Inline Mode через [@BotFather](https://t.me/BotFather):
`/setinline` → выбрать бота → задать placeholder, например `Выберите готовый ответ`. Без
включённого Inline Mode кнопки групп не смогут открыть список ответов; при старте приложение
запишет предупреждение `telegram_inline_mode_disabled`, но не остановит остальные функции.

При старте бот создаёт отдельную системную тему `📚 Готовые ответы`, базовую группу `Общее` и
общую закреплённую панель. В ней сразу показаны группы с количеством ответов:

- нажатие на группу открывает персональный inline-список Telegram только у нажавшего оператора;
- выбор ответа публикует в теме одно чистое сообщение с текстом, кнопкой удаления и повторным
  открытием той же группы; короткий текст дополнительно можно скопировать нативной кнопкой;
- маленький `➕` справа от группы сразу запускает добавление ответа в неё;
- `➕ Создать новую группу` запускает создание группы и первого ответа.

Промежуточное сообщение-каталог не создаётся. Открытый inline-список не виден другим операторам;
выбранный и отправленный в тему текст, как обычное сообщение группы, виден всем до удаления.

Добавление использует одну временную карточку конкретного администратора. Бот редактирует её на
шагах `группа → название → текст → предпросмотр`; введённые оператором сообщения удаляются после
обработки. Карточка позволяет сохранить, исправить или отменить ввод и автоматически удаляется
через 10 минут бездействия.

`Общее` располагается первой, остальные группы и ответы сортируются по названию. Панель выводит
по 8 групп на страницу, inline-список — по 20 ответов с нативной пагинацией Telegram. Название
группы и ответа ограничено 64 символами, текст — 3500. Названия групп уникальны; одинаковое
название ответа разрешено в разных группах, но не внутри одной группы. Ранее созданные ответы
автоматически находятся в `Общее`.

Inline-запросы обслуживаются только для `ADMIN_TELEGRAM_IDS`, помечаются персональными и не
кешируются Telegram. Команды из системной темы клиентам не отправляются. `/addgroup`,
`/addanswer` и `/answers` сохранены только как технический fallback; для обычной работы
используйте закреплённую панель.

Обычный ответ в теме отправляется клиенту и при необходимости заново открывает тикет.
В Web-теме поддерживаются текст и одно фото; ответ забирает backend сайта. `/block` и `/unblock`
работают в обоих каналах; для Web заблокированный клиент продолжает получать обычные API-ответы,
но его новые сообщения не попадают оператору. Remnawave-команды работают для обоих каналов при
однозначной привязке.
Первая принятая оценка автоматически создаёт системную тему `⭐ Оценки`; последующие оценки
публикуются там со ссылкой на исходный тикет.
В General topic хранится одно сообщение `📊 Статистика` с периодами сегодня, 7 и 30 дней; callback
доступен только `ADMIN_TELEGRAM_IDS`.

## Deploy и rollback

Значения `registry.example/...` ниже — placeholders. Deploy принимает tag или digest,
но сохраняет и запускает только фактически полученный RepoDigest.

```bash
DEPLOY_DIR=/opt/suppsystem \
  sh scripts/deploy.sh deploy registry.example/suppsystem:v3.0.0
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

## Health и логи

```bash
sh scripts/production-compose.sh ps
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/health
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/ready
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/metrics
```

HTTP endpoints доступны при включённом Operator API или Web API. `/health` проверяет процесс, `/ready` — базу и
настроенные компоненты, `/metrics` отдаёт Prometheus-метрики. Логи — JSON; основные поля:
`event`, `trace_id`, `ticket_id`, `delivery_id`, `operator_action_id`.

## Backup и восстановление

Храните зашифрованные копии вне Docker volumes и регулярно проверяйте restore на отдельном
стенде. Backup содержит пользовательские данные. При включённом Web API передавайте третий путь:
скрипт кратко остановит единственный writer и создаст согласованную пару БД + media archive.

### SQLite

Перед запуском экспортируйте `APP_IMAGE` и `SUPPSYSTEM_ENV_FILE` так же, как для Compose.

```bash
COMPOSE_FILE=compose.production.sqlite.yaml \
  ./scripts/backup.sh sqlite /srv/backups/support.db /srv/backups/support-media.tar.gz
CONFIRM_RESTORE=yes COMPOSE_FILE=compose.production.sqlite.yaml \
  ./scripts/restore.sh sqlite /srv/backups/support.db /srv/backups/support-media.tar.gz
```

Двухаргументный DB-only backup сохраняет прежнее online-поведение. Трёхаргументный backup
останавливает приложение на время согласованного снимка. Restore заранее проверяет оба архива.

### PostgreSQL

```bash
PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/suppsystem \
  sh scripts/backup.sh postgres /srv/backups/support.dump /srv/backups/support-media.tar.gz
CONFIRM_RESTORE=yes PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/suppsystem \
  sh scripts/restore.sh postgres /srv/backups/support.dump /srv/backups/support-media.tar.gz
```

Archive проверяется до остановки. Restore работает через migration role, повторно применяет
миграции и ждёт healthcheck. После ошибки проверьте БД и запускайте приложение вручную.

Перед обновлением всегда создавайте backup. Не запускайте старый image поверх новой схемы без
явно поддерживаемого downgrade.

Проверка orphan/temp Web-файлов безопасна по умолчанию; удаление требует явного флага:

```bash
docker compose exec suppsystem python -m suppsystem.media_cleanup
docker compose exec suppsystem python -m suppsystem.media_cleanup --apply
```

`scripts/drill_production_data_path.sh` проверяет deploy, rollback, backup и restore только на
изолированном стенде. Его отчёты и дампы могут содержать чувствительные данные.

## Production checklist

- `.env` имеет права `0600` и не хранится в Git;
- секреты уникальны, PostgreSQL использует три разных пароля;
- API опубликован только через HTTPS reverse proxy;
- настроены логи, метрики и alerts;
- backup хранится отдельно и restore регулярно проверяется;
- после обновления проверены readiness, логи и тестовый тикет.
