# Техническое устройство

Архитектура, данные и гарантии системы. Установка и обслуживание описаны в
[OPERATIONS.md](OPERATIONS.md).

## Назначение и границы

Система связывает личный чат клиента с темой закрытой Telegram Forum-группы. Operator API,
Remnawave и notification webhook опциональны. Поддерживается один процесс приложения; база —
SQLite или PostgreSQL.

```text
Telegram updates ──> Telegram adapter ──┐
                                       ├──> application services ──> SQL database
Operator REST API ──────────────────────┘              │
                                                      ├──> Remnawave API
                                                      └──> durable outbox
                                                               │
                                                        delivery workers
                                                          │         │
                                                     Telegram   webhook
```

Transport-слой не содержит бизнес-правил, а прикладные сервисы не зависят от aiogram или
FastAPI. Основные модули:

| Область | Модули |
| --- | --- |
| Telegram и тикеты | `telegram_*`, `ticket_*`, `services.py` |
| доставка | `outbox_repository.py`, `delivery.py`, `notification_webhook.py` |
| Remnawave | `panel*`, `remnawave.py` |
| API и runtime | `api*`, `__main__.py`, `runtime_health.py` |
| данные | `models.py`, `database.py`, Alembic migrations |

Границы проверяются architecture-тестами.

## Тикеты и сообщения

Тикет находится в состоянии `provisioning`, `open` или `closed`. У пользователя не более
одного тикета, `topic_id` уникален. Новое сообщение повторно открывает закрытый тикет; счётчик
циклов закрытия ограничивает оценку одной на каждый цикл.

Создание Forum-темы защищено provisioning token. Неизвестный результат Telegram-вызова остаётся
для ручной проверки: автоматический повтор мог бы создать вторую тему.

Сообщение сначала фиксируется в `ticket_messages`, затем попадает в `delivery_outbox`:

```text
waiting_topic ──> pending ──> processing ──> delivered
                     ▲              │
                     └──── retry ────┘
                                    └────> failed
```

Гарантии доставки:

- запись сообщения до внешней отправки;
- FIFO внутри тикета и ограниченные повторы с backoff;
- возврат зависших `processing` заданий;
- восстановление удалённой темы и перенаправление незавершённых сообщений;
- blocklist до создания тикета и перед исходящей доставкой;
- дедупликация Telegram updates и API-мутаций;
- персональный admission limit: 30 личных сообщений в минуту и 150 в час.

Доставка — **at-least-once**. Telegram не поддерживает idempotency key, поэтому сбой между
`send` и commit иногда создаёт дубль.

## Данные

| Группа таблиц | Содержимое |
| --- | --- |
| `users`, `user_identities` | пользователь и внешние идентификаторы |
| `tickets`, `ticket_messages` | тикеты, темы, сообщения, заметки и оценки |
| `delivery_outbox`, `notification_outbox` | очереди Telegram и webhook |
| `inbound_updates` | дедупликация входящих updates |
| `operator_actions`, `reconciliation_outbox` | аудит Remnawave и очередь сверки |
| `blocklist` | заблокированные Telegram-пользователи |

После успешной обработки сырой Telegram payload удаляется. Метаданные обработанных updates
хранятся 7 дней, завершённые outbox и reconciliation записи — 30 дней; failed-записи
сохраняются для диагностики. История тикетов этим процессом не удаляется.

Схема развивается только Alembic-миграциями. SQLite использует foreign keys, WAL,
`busy_timeout=5000` и ограниченные повторы конфликтов записи.

Production PostgreSQL разделяет bootstrap, migration и runtime credentials. Provisioning создаёт
least-privilege роли, `postgres-migrate` применяет Alembic, а приложение получает только
`CONNECT`, `USAGE`, DML и необходимые права sequences. Поддержка нескольких экземпляров
приложения отсутствует.

## Авторизация и Operator API

`ADMIN_TELEGRAM_IDS` содержит администраторов с доступом ко всем Telegram-командам, включая
`/resolvepanel`. Права в самой группе доступа к приложению не дают. Пустой список разрешён только
при включённом Operator API и означает API-only работу операторов.

Operator API включается отдельно. Все endpoints, включая `/docs` и `/openapi.json`, защищены
`X-API-Token`; мутации дополнительно требуют `X-Idempotency-Key`.

| Метод | Назначение |
| --- | --- |
| `GET /api/v1/tickets` | список тикетов |
| `GET /api/v1/tickets/{ticket_id}` | тикет |
| `GET /api/v1/tickets/{ticket_id}/messages` | история сообщений |
| `POST /api/v1/tickets/{ticket_id}/messages` | сообщение клиенту |
| `POST /api/v1/tickets/{ticket_id}/close` | закрытие |
| `POST /api/v1/tickets/{ticket_id}/reopen` | повторное открытие |

Точный retry API-мутации воспроизводит сохранённый ответ; другой payload с тем же ключом получает
`409`. Rate limit process-local, forwarded IP принимается только от доверенных proxy. Uvicorn и
Telegram polling работают в одном failure domain.

## Remnawave

Поддерживается API Remnawave 2.8.x. Поиск выполняется только по `telegramId`; требуется ровно
один пользователь. Перед каждой мутацией выполняется свежий lookup, Remnawave остаётся источником
истины, а для одного тикета допускается одна незавершённая мутация.

Действие и reconciliation job фиксируются до внешнего вызова. Timeout, HTTP 5xx, некорректный JSON
или недостоверный success считаются **unknown outcome** и не вызывают повторную мутацию. Worker
только читает состояние и проверяет результат:

- `/gift` — ожидаемую дату окончания;
- `/revokelink` — смену subscription URL;
- `/resetkey` — SHA-256 fingerprint credentials без хранения исходных ключей;
- `/resetdevices` — пустой список HWID-устройств.

Результат сверки: `completed`, `not_applied` или `inconclusive`. Последний блокирует новые
мутации. После независимой проверки администратор может выполнить
`/resolvepanel <action_uuid> applied|not_applied`. Команда не повторяет вызов, использует
first-writer-wins переход и сохраняет решение в аудите. Недоступные read-ответы повторяются с
backoff и после 20 неудач становятся `inconclusive`.

`/revokelink` заранее создаёт durable notification intent. После подтверждения новая ссылка
ставится в webhook-очередь и, по умолчанию, отправляется клиенту в Telegram. Флаг
`REMNAWAVE_REVOKE_LINK_TELEGRAM_NOTIFICATION=false` отключает только Telegram-сообщение.

## Notification webhook

`notification_outbox` доставляется с семантикой **at-least-once**. HMAC-SHA256 считается от
`<timestamp>.<raw-body>`; получатель обязан проверить подпись и атомарно дедуплицировать эффект
по `event_id` до ответа 2xx. Повторы учитывают тип ошибки и `Retry-After`.

## Наблюдаемость и безопасность

- JSON-логи содержат `event`, trace ID и идентификаторы сущностей;
- `/health` проверяет HTTP-процесс, `/ready` — базу и компоненты, `/metrics` — метрики;
- heartbeat-файлы используются Docker healthcheck даже при выключенном API;
- секреты интеграций должны содержать не менее 32 символов;
- внешние Remnawave/webhook URL требуют HTTPS и не могут содержать credentials или fragment;
- контейнер непривилегированный, root filesystem read-only, capabilities удалены;
- API использует один статический principal без scopes;
- multi-instance coordination отсутствует;
- редкий дубль Telegram-сообщения допустим из-за at-least-once.
