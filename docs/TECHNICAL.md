# Техническое устройство

Архитектура, модель данных и гарантии системы. Установка и обслуживание описаны в
[руководстве по эксплуатации](OPERATIONS.md).

## Назначение и границы

Система связывает личный чат клиента с темой закрытой Telegram Forum-группы. Operator API,
Remnawave и notification webhook — поддерживаемые опциональные компоненты.

Архитектура рассчитана на один процесс приложения. PostgreSQL можно использовать вместо SQLite,
но несколько одновременно работающих экземпляров пока не поддерживаются.

## Архитектура

```text
Telegram updates ──> Telegram adapter ──┐
                                       ├──> Application services
Operator REST API ──────────────────────┘       │
                                      ┌────────┼───────────┐
                                      ▼        ▼           ▼
                                SQL database  outbox   Remnawave API
                                                │
                                                ▼
                                         delivery workers
                                                │
                                      ┌─────────┴─────────┐
                                      ▼                   ▼
                                   Telegram       product webhook
```

Основные части `src/supportbot`:

| Часть | Ответственность |
| --- | --- |
| `telegram_*` | handlers, Forum-темы и lifecycle Telegram runtime |
| `ticket_*`, `services.py` | тикеты, сообщения, авторизация и blocklist |
| `outbox_repository.py`, `delivery.py` | durable очереди и доставка |
| `panel*`, `remnawave.py` | Remnawave use cases, recovery и HTTP-клиент |
| `api*` | Operator API, DTO, middleware и endpoints |
| `notification_webhook.py` | доставка событий во внешний backend |
| `models.py`, `database.py` | ORM-модели и соединения с БД |
| `__main__.py`, `runtime_health.py` | сборка приложения, shutdown и readiness |

Transport-слой не владеет правилами тикетов, а прикладные сервисы не зависят от aiogram или
FastAPI. Границы контролируются architecture-тестами.

## Жизненный цикл тикета

Тикет имеет три состояния:

- `provisioning` — запись создана, Forum-тема создаётся или требует восстановления;
- `open` — обращение открыто;
- `closed` — обращение закрыто, история и тема сохранены.

У пользователя не более одного тикета, а `topic_id` уникален. Сообщение в закрытом тикете
открывает его снова. Номер цикла закрытия не позволяет поставить оценку дважды.

Создание темы защищено provisioning token. Если результат Telegram-вызова неизвестен, claim
сохраняется для ручной проверки: автоматический повтор мог бы создать вторую тему.

## Сообщения и outbox

Сообщение сначала сохраняется в `ticket_messages`, затем создаётся `delivery_outbox`. Worker
выбирает готовые задания, отправляет их и фиксирует Telegram message ID.

```text
waiting_topic ──> pending ──> processing ──> delivered
                     ▲              │
                     └──── retry ────┘
                                    └────> failed
```

Система гарантирует:

- фиксацию сообщения до внешней отправки;
- ограниченные повторы с backoff;
- FIFO внутри тикета, включая отложенный retry;
- возврат зависших `processing` заданий в очередь;
- восстановление удалённой темы и перенаправление незавершённых заданий;
- проверку blocklist до создания тикета и перед исходящей доставкой;
- дедупликацию Telegram updates и API-мутаций по уникальным ключам.

Доставка — **at-least-once**. Telegram не поддерживает idempotency key, поэтому сбой между `send`
и commit может дать дубль; это исключает намеренную потерю сообщения.

## Модель данных

| Таблица | Назначение |
| --- | --- |
| `users` | локальные данные пользователя |
| `user_identities` | внешние идентификаторы, включая Telegram ID |
| `tickets` | тикет, Forum-тема, статус, цикл закрытия и время последней активности |
| `ticket_messages` | сообщения, медиа, заметки и оценки |
| `delivery_outbox` | очередь Telegram-доставки |
| `notification_outbox` | очередь webhook-событий |
| `inbound_updates` | durable дедупликация входящих Telegram updates |
| `reconciliation_outbox` | очередь сверки неизвестных результатов внешних мутаций |
| `operator_actions` | аудит и состояние операторских действий |
| `blocklist` | заблокированные Telegram-пользователи |

Схема развивается миграциями Alembic. SQLite по умолчанию выполняет `upgrade head` при старте.
Production PostgreSQL использует отдельный one-shot `postgres-migrate`, поэтому migration
credential не попадает в runtime-контейнер. Вне production Compose `MIGRATION_DATABASE_URL` по
умолчанию совпадает с `DATABASE_URL`.

Production PostgreSQL разделяет роли:

- bootstrap-admin существует только для инициализации кластера и one-shot provisioning service;
- migration role владеет схемой и Alembic без cluster-level привилегий;
- runtime role имеет только `CONNECT`, `USAGE`, DML таблиц и необходимые права sequences без
  владения схемой и DDL.

Provisioning идемпотентно настраивает роли, ownership и default privileges; затем
`postgres-migrate` применяет Alembic. Приложение ждёт успешного завершения обеих операций.

`last_activity_at` обновляется сообщениями, заметками, оценками и переходами close/reopen и
используется для сортировки тикетов. Eager loading не даёт числу SQL-запросов расти вместе со
списком.

SQLite использует foreign keys, WAL и `busy_timeout=5000` на каждом соединении. Временная
конкуренция записи обрабатывается ограниченными повторными попытками.

## Администраторы

`ADMIN_TELEGRAM_IDS` содержит числовые Telegram ID администраторов. Каждый указанный
администратор может читать и отвечать в связанных Forum-темах, выполнять все команды
поддержки, Remnawave и восстановления, включая `/resolvepanel`. Права пользователя в
Telegram-группе сами по себе не дают доступ к приложению.

## Operator API

API включается отдельно, принимает административный токен в заголовке `X-API-Token`, а
мутации дополнительно требуют `X-Idempotency-Key`.

| Метод и путь | Назначение |
| --- | --- |
| `GET /api/v1/tickets` | список тикетов, фильтр статуса и пагинация |
| `GET /api/v1/tickets/{ticket_id}` | карточка тикета |
| `GET /api/v1/tickets/{ticket_id}/messages` | история сообщений |
| `POST /api/v1/tickets/{ticket_id}/messages` | сообщение клиенту |
| `POST /api/v1/tickets/{ticket_id}/close` | закрытие тикета |
| `POST /api/v1/tickets/{ticket_id}/reopen` | повторное открытие |

`/docs` и `/openapi.json` защищены тем же токеном. API имеет process-local rate limit, trace ID,
аудит и durable idempotency: точный retry воспроизводит сохранённый ответ, а другой payload с тем
же ключом получает `409`. Forwarded client IP принимается только от trusted proxies.

Uvicorn и Telegram polling работают под общей supervision. При graceful shutdown сначала
закрывается ingress и дренируются workers, затем — sessions и БД.

## Remnawave

Поддерживается API-контракт Remnawave 2.8.x. Выключенная или ненастроенная интеграция не влияет на
обычные сценарии поддержки.

- Telegram-тикет ищет пользователя только по `telegramId`.
- Допустим ровно один результат; 0 или несколько результатов дают fail-closed.
- Перед мутацией выполняется свежий lookup.
- `uuid` используется для мутаций, `username` — для отображения.
- Remnawave остаётся источником истины для ключей, сроков и устройств.
- Для одного тикета одновременно допускается одна незавершённая мутация.

Действие и задание сверки фиксируются в БД до внешней мутации. Timeout, HTTP 5xx, некорректный JSON
и недостоверный success считаются **unknown outcome**. Мутация автоматически не повторяется:
worker после задержки только читает текущее состояние Remnawave и подтверждает результат.

- `/gift` проверяет точную ожидаемую дату окончания;
- `/revokelink` проверяет смену subscription URL;
- `/resetkey` сравнивает SHA-256 fingerprint protocol credentials; исходные ключи в БД не
  сохраняются;
- `/resetdevices` проверяет, что список HWID-устройств пуст.

Сверка даёт `completed`, `not_applied` или `inconclusive`. В последнем случае действие остаётся
`unknown`, а новые мутации тикета блокируются. Full admin может классифицировать его командой
`/resolvepanel <operator_action_uuid> applied|not_applied` только после независимой проверки.
Команда не повторяет мутацию, выполняет fenced first-writer-wins переход и сохраняет решение,
actor, время и command key в аудите. Только для подтверждённого `/revokelink applied` выполняется
read-only lookup: текущая ссылка нужна, чтобы завершить заранее созданный notification intent.

Недоступные и некорректные read-ответы повторяются с backoff; после 20 неудач действие становится
`inconclusive`. Повреждённый локальный payload получает тот же статус. Эти случаи никогда не
трактуются как `not_applied`.

`/revokelink` заранее создаёт durable webhook intent. По умолчанию после подтверждения новой
ссылки независимо ставятся в очередь webhook и сообщение клиенту в Telegram. При
`REMNAWAVE_REVOKE_LINK_TELEGRAM_NOTIFICATION=false` создаётся только webhook. Успех не
финализируется, пока intents не готовы к доставке, поэтому рестарт не приводит к повторному
revoke.

## Notification webhook

Worker доставляет `notification_outbox` во внешний backend. HMAC-SHA256 от
`<timestamp>.<raw-body>` передаётся с timestamp и event ID. Повторы учитывают тип ошибки и
`Retry-After`, сохраняя body и `event_id`.

Доставка — **at-least-once**: получатель обязан атомарно дедуплицировать эффект по `event_id` до
ответа 2xx.

## Наблюдаемость

- JSON-логи содержат `event`, trace ID и контекст сущности;
- `/health` показывает, что HTTP-процесс жив;
- `/ready` проверяет базу и настроенные runtime-компоненты;
- `/metrics` отдаёт Prometheus-compatible метрики очередей, ошибок, latency и heartbeat;
- файловые heartbeat используются Docker healthcheck даже при выключенном API.

## Безопасность и известные ограничения

- секреты включённых интеграций — минимум 32 символа и не placeholder;
- внешние Remnawave/webhook URL требуют HTTPS, HTTP разрешён только для loopback;
- URL с credentials или fragment отклоняются;
- контейнер непривилегированный, root filesystem read-only, capabilities удалены;
- API пока использует общий статический principal без scopes, rotation и revocation;
- multi-instance coordination отсутствует;
- редкий дубль Telegram-сообщения допустим в рамках at-least-once.
