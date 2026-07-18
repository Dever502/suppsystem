# Техническое устройство

Документ описывает архитектуру, модель данных и гарантии Telegram Support Platform. Установка,
настройка и обслуживание описаны в [руководстве по эксплуатации](OPERATIONS.md).

## Назначение и границы

Система связывает личный чат клиента с отдельной темой закрытой Telegram Forum-группы. Telegram
служит клиентским интерфейсом и рабочим местом операторов. REST API — опциональное расширение;
Remnawave и notification webhook — поддерживаемые опциональные интеграции, выключенные по
умолчанию.

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

Основные модули в `src/supportbot`:

| Модуль | Ответственность |
| --- | --- |
| `__main__.py` | запуск, связывание компонентов и корректное завершение |
| `telegram_adapter.py` | facade и регистрация aiogram handlers |
| `telegram_user_handlers.py` | личные сообщения клиента и оценки |
| `telegram_operator_handlers.py` | ответы, команды операторов и администраторов |
| `telegram_topic_manager.py` | создание, восстановление и синхронизация Forum-тем |
| `telegram_constants.py` | тексты и наборы Telegram-команд |
| `telegram_message_utils.py` | разбор команд, metadata медиа и rating UI |
| `telegram_lifecycle.py` | запуск polling и упорядоченное завершение Telegram runtime |
| `telegram_locks.py` | ограниченные по времени per-ticket locks |
| `services.py` | ticket lifecycle, команды и стабильный публичный facade |
| `ticket_service_base.py` | общие identity/view/blocklist persistence primitives |
| `ticket_topic_service.py` | открытие тикета, topic binding, recovery и ticket queries |
| `ticket_lifecycle_service.py` | close/reopen, rating и blocklist use cases |
| `ticket_message_service.py` | атомарное сохранение сообщений и постановка в outbox |
| `outbox_repository.py` | claim-fenced state machines очередей |
| `delivery.py` | доставка сообщений из durable outbox |
| `panel.py` | стабильный публичный facade Remnawave use cases |
| `panel_types.py` | контракты панели и чистое преобразование ответов |
| `panel_action_service.py` | публичные lookup/mutation use cases |
| `panel_reconciliation_service.py` | unknown outcome и startup recovery |
| `panel_persistence_service.py` | reservation, audit и notification intent persistence |
| `remnawave.py` | HTTP-клиент панели |
| `api.py` | сборка API, authentication, middleware и error handling |
| `api_routes.py` | health, документация и operator endpoints |
| `api_schemas.py` | HTTP DTO и валидация входных данных |
| `notification_webhook.py` | доставка событий во внешний backend |
| `models.py` | ORM-модель данных |
| `database.py` | соединения и настройки базы |
| `runtime_health.py` | readiness runtime-компонентов |

Transport-слой преобразует вход и выход, но не владеет правилами тикетов. Прикладные сервисы не
должны зависеть от aiogram или FastAPI. Эти границы контролируются architecture-тестами.

## Жизненный цикл тикета

Тикет имеет три состояния:

- `provisioning` — запись создана, Forum-тема создаётся или требует восстановления;
- `open` — обращение открыто;
- `closed` — обращение закрыто, история и тема сохранены.

Для пользователя существует не более одного тикета, а `topic_id` уникален. Новое сообщение
клиента или ответ оператора в закрытом тикете повторно открывает его. Каждый цикл закрытия имеет
свой номер, поэтому оценку нельзя записать дважды в одном цикле.

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

Доставка имеет семантику **at-least-once**. Telegram не поддерживает клиентский idempotency key.
Если процесс завершится после успешного `send`, но до commit результата, возможен дубль. Это
сознательный выбор в пользу отсутствия намеренной потери сообщения.

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

Схема развивается миграциями Alembic. Для SQLite и обратной совместимости приложение по умолчанию
выполняет `upgrade head` при старте. Production PostgreSQL перед запуском приложения выполняет
миграции в отдельном one-shot `postgres-migrate`, а `MIGRATIONS_AT_STARTUP=false` не оставляет
migration credential в долгоживущем runtime-контейнере. Проверка миграций сравнивает конечную схему
с ORM metadata. `MIGRATION_DATABASE_URL` по умолчанию совпадает с `DATABASE_URL` вне production
Compose.

Production PostgreSQL использует три разные роли:

- bootstrap-admin существует только для инициализации кластера и one-shot provisioning service;
- migration role владеет схемой `public` и объектами Alembic, но не имеет cluster-superuser,
  `CREATEDB`, `CREATEROLE`, replication или bypass RLS; `CREATE` ограничен одной application DB и
  нужен для schema restore;
- runtime role имеет только `CONNECT`, `USAGE`, DML таблиц и необходимые права sequences без
  владения схемой и DDL.

Provisioning идемпотентно переносит существующие объекты legacy-инсталляции под migration-owner,
обновляет пароли/ограничения ролей и default privileges. Затем `postgres-migrate` применяет Alembic.
`supportbot` не запускается, пока обе one-shot операции не завершились успешно.

`last_activity_at` изменяется вместе с сообщениями, заметками, оценками и переходами
close/reopen. Списки тикетов сортируются по этому полю. Пользователь и его identities загружаются
через joined/select-in eager loading, поэтому число SQL-запросов listing не растёт вместе с числом
тикетов.

SQLite использует foreign keys, WAL и `busy_timeout=5000` на каждом соединении. Временная
конкуренция записи обрабатывается ограниченными повторными попытками.

## Роли и права

Один Telegram ID должен состоять ровно в одной роли.

| Роль | Возможности |
| --- | --- |
| `full_admin` | полный доступ ко всем операторским командам |
| `operator` | полный доступ ко всем операторским командам в текущей версии |
| `operator_ro` | только `/info` и `/subinfo` |

Сейчас full admin и operator имеют одинаковые права. Разделение ролей сохранено для будущего
ограничения операторских полномочий. Команды `/stopall`, `/synctopics`, `/block` и `/unblock`
требуют full access, которым сейчас обладают обе эти роли.

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

`/docs` и `/openapi.json` доступны при включённом API и защищены тем же токеном. Реализованы
process-local rate limit, защита авторизации, безопасный error envelope, trace ID и аудит мутаций.
Idempotency является durable: `(operation, resource, key)` связан с fingerprint смыслового payload
и сохранённым ответом. Точный retry воспроизводит ответ, несовпадающий payload получает `409`.
Forwarded client IP учитывается только от настроенных trusted proxies; цепочка разбирается справа
налево до ближайшего недоверенного hop, а edge Nginx отбрасывает присланную клиентом цепочку.

Uvicorn task находится под общей supervision с Telegram polling. Неожиданное завершение API не
теряется в фоне, а переводит API health в degraded и завершает общий процесс. Graceful shutdown
сначала закрывает ingress и дожидается workers, затем закрывает Telegram/HTTP sessions и БД.

## Remnawave

Поддерживается API-контракт Remnawave 2.8.0. Выключенная или ненастроенная интеграция не влияет на
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

После ограниченной серии сверок результат получает одну из трёх классификаций: точное ожидаемое
состояние даёт `completed`, доказанно неизменившееся состояние — `not_applied`, а конфликтующее или
недостаточное состояние — `inconclusive`. В последнем случае действие остаётся `unknown`, и новые
мутации тикета блокируются. Full admin может классифицировать его командой
`/resolvepanel <operator_action_uuid> applied|not_applied` только после независимой проверки.
Команда не повторяет мутацию, выполняет fenced first-writer-wins переход и сохраняет решение,
actor, время и command key в аудите. Только для подтверждённого `/revokelink applied` выполняется
read-only lookup: текущая ссылка нужна, чтобы завершить заранее созданный notification intent.

Если процесс завершился до фиксации задания сверки, startup recovery знает, что внешний вызов ещё
не начинался. Недоступность и некорректные read-ответы Remnawave повторяются с backoff; после 20
неудач действие становится `inconclusive` и оператор получает уведомление. Повреждённый локальный
payload сразу переводится в `inconclusive`. Ни один из этих случаев не трактуется как `not_applied`.

`/revokelink` заранее создаёт durable notification intent. Успех не финализируется, пока
уведомление не готово к доставке, поэтому рестарт не приводит к повторному revoke.

## Notification webhook

Worker доставляет события из `notification_outbox` во внешний backend. Запрос подписывается
HMAC-SHA256: подпись вычисляется для `<timestamp>.<raw-body>` и передаётся в
`X-Support-Signature` вместе с `X-Support-Timestamp` и `X-Support-Event-Id`. Повторы различают
временные и постоянные ошибки, учитывают `Retry-After` и сохраняют одинаковые body и `event_id`.

Доставка имеет семантику **at-least-once**: если backend принял событие, а приложение не успело
зафиксировать ответ, событие будет отправлено повторно. Получатель обязан атомарно
дедуплицировать побочный эффект по `event_id` и только после этого отвечать кодом 2xx.

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
