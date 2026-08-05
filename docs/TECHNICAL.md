# Техническое устройство

Архитектура, данные и гарантии системы. Установка и обслуживание описаны в
[OPERATIONS.md](OPERATIONS.md).

## Назначение и границы

Система связывает Telegram-чат или backend сайта с темой закрытой Telegram Forum-группы. Web API,
Operator API, Remnawave и notification webhook включаются независимо. Поддерживается один процесс;
база — SQLite или PostgreSQL.

```text
Telegram updates ──> Telegram adapter ──┐
Web Support API ────> Web adapter ──────┼──> application services ──> SQL database
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
| Telegram и тикеты | `telegram_*`, `ticket_*`, `services.py`, `quick_replies.py` |
| доставка | `outbox_repository.py`, `delivery.py`, `notification_webhook.py` |
| Remnawave | `panel*`, `remnawave.py` |
| API и runtime | `api*`, `web_api*`, `__main__.py`, `runtime_health.py` |
| данные | `models.py`, `database.py`, Alembic migrations |

Границы проверяются architecture-тестами.

## Тикеты и сообщения

Тикет находится в состоянии `provisioning`, `open` или `closed`, имеет канал `telegram|web`, а
`topic_id` уникален. Telegram и Web identities автоматически не объединяются. На одну Web identity
существует один постоянный тикет/topic. Новое сообщение открывает следующий close cycle, в котором
допустима одна оценка.

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
- общая блокировка тикета перед ingress и ответом оператора; legacy blocklist сохраняет
  совместимость Telegram-блокировки до создания тикета;
- дедупликация Telegram updates и API-мутаций;
- персональный admission limit: 30 личных сообщений в минуту и 200 в час.

Доставка — **at-least-once**. Telegram не поддерживает idempotency key, поэтому сбой между
`send` и commit иногда создаёт дубль.

## Данные

| Группа таблиц | Содержимое |
| --- | --- |
| `users`, `user_identities` | пользователь и внешние идентификаторы |
| `tickets`, `ticket_messages` | тикеты, темы, сообщения, заметки и оценки |
| `media_assets`, `ticket_lifecycle_events` | Web-фото и append-only события статистики |
| `system_settings`, `operator_dashboard_state` | identity mode, системные темы и ID постоянных Telegram-панелей |
| `quick_responses` | быстрые ответы, хештеги, авторство, источник, публикация и отложенная валидация |
| `delivery_outbox`, `notification_outbox` | очереди Telegram и webhook |
| `inbound_updates` | дедупликация входящих updates |
| `operator_actions`, `reconciliation_outbox` | аудит Remnawave и очередь сверки |
| `support_blocks`, `blocklist` | блокировки тикетов и совместимость Telegram pre-ticket blocklist |

После успешной обработки сырой Telegram payload удаляется. Метаданные обработанных updates
хранятся 7 дней, завершённые outbox и reconciliation записи — 30 дней; failed-записи
сохраняются для диагностики. История тикетов этим процессом не удаляется.

Схема развивается только Alembic-миграциями. SQLite использует foreign keys, WAL,
`busy_timeout=5000` и ограниченные повторы конфликтов записи.

Production PostgreSQL разделяет bootstrap, migration и runtime credentials. Provisioning создаёт
least-privilege роли, `postgres-migrate` применяет Alembic, а приложение получает только
`CONNECT`, `USAGE`, DML и необходимые права sequences. Поддержка нескольких экземпляров
приложения отсутствует. Внутри единственного процесса независимые тикеты обрабатываются ограниченно
параллельно: доставка — до 8 заданий, webhook и reconciliation — до 4. Порядок заданий одного
тикета защищён очередями БД; Telegram ingress остаётся последовательным. PostgreSQL engine использует
ограниченный пул: 10 постоянных и до 10 временных соединений.

## Быстрые ответы

При старте приложение создаёт или переименовывает системную Forum-тему `⚡ Быстрые ответы`,
создаёт короткую инструкцию и закрепляет её. ID темы и инструкции хранятся в
`system_settings`. Worker раз в минуту редактированием инструкции проверяет существование темы.
При удалении инструкции создаётся новая; при подтверждённом удалении темы создаётся замена и все
валидные ответы из БД публикуются в ней обычными текстовыми сообщениями.

Авторизованный оператор создаёт ответ обычным `message` в системной теме. Backend извлекает
Telegram entities типа `hashtag`; семантического анализа, словарей и ML нет. При 0–4 хештегах
исходный текст и список тегов записываются в `quick_responses`. Затем бот публикует одно
каноническое сообщение `исходный текст + два перевода строки + [SAVE]`, фиксирует его ID и
добавляет кнопку мгновенного удаления. Только после успешной публикации бот удаляет исходник
оператора.
Уникальная пара `source_chat_id/source_message_id` делает повторную доставку update и
`edited_message` идемпотентными.

При количестве тегов больше четырёх запись получает состояние `pending_deletion`, фиксированный
`invalid_until` и ID статичного reply-предупреждения. In-memory задача удаляет исходное сообщение
и предупреждение через пять минут; при рестарте задачи восстанавливаются из БД. Корректный
`edited_message` до дедлайна переводит ту же запись в `valid`, отменяет задачу, публикует
каноническое сообщение и удаляет исходник с предупреждением. Повторные невалидные правки не
продлевают исходный дедлайн.

Поиск и переход к ответу выполняет нативный поиск Telegram по исходному тексту или хештегу.
Кнопка удаления доступна только `ADMIN_TELEGRAM_IDS`: запись сначала атомарно переводится в
`deleted` с ID оператора и временем, затем удаляется Telegram-сообщение. Tombstone остаётся в
БД и не восстанавливается. Если активное сообщение удалили вручную, при следующем запуске бот
обнаруживает отсутствие и публикует его заново.
Inline query handlers, callback-каталог, группы и команды управления ответами отсутствуют.
Startup preflight требует у бота права управления темами и удаления сообщений.
После миграции старые отдельные `[SAVE]` удаляются, а сохранённые ответы приводятся к
каноническому формату один раз; версия формата хранится в `publication_format_version`.

## Авторизация и HTTP API

`ADMIN_TELEGRAM_IDS` содержит администраторов с доступом ко всем Telegram-командам, включая
`/resolvepanel`. Права в самой группе доступа к приложению не дают. Пустой список разрешён только
при включённом Operator API и означает API-only работу операторов.

Operator API включается отдельно. Все endpoints, включая `/docs` и `/openapi.json`, защищены
`X-API-Token`; общие endpoints принимают любой включённый API token, а пространства `/tickets` и
`/web` — только свой. Мутации дополнительно требуют `X-Idempotency-Key`.

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

Web API `/api/v1/web` использует независимый token и только server-to-server модель. Identity mode
`external_id` или `email` фиксируется при первом ingress. Мутации idempotent; polling использует
непрозрачный cursor `(created_at, id)`. Фото проверяется по общему размеру запроса, MIME и
сигнатуре, сохраняется под generated path в `DATA_DIR/web-media`; подпись ограничена лимитом
Telegram, URL клиента и исходное имя не определяют путь. Ответ оператора Web-клиенту фиксируется в
БД без Telegram delivery job. Для заблокированного тикета входящие сообщения и оценки сохраняются
с `ticket_messages.suppressed=true` и возвращаются в polling как обычные, но исключаются из
Operator API, delivery/lifecycle work и статистики. Исходящие и служебные уведомления подавляются;
точный retry сохраняет исходный скрытый результат. Block/unblock доступны из Forum-темы и через
идемпотентные Web API endpoints; состояние блокировки в клиентский контракт не включается.

## Remnawave

Поддерживается API Remnawave 2.8.x. Telegram ticket ищется по `telegramId`. Web ticket сначала
использует сохранённый UUID, затем точный email lookup; stale UUID очищается. Найденный UUID
сохраняется, но не объединяет Telegram/Web tickets. Перед мутацией выполняется свежий lookup, а
его фактическая identity атомарно обновляется в action и notification intent. Для одного тикета
допускается одна незавершённая мутация.

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
- Web audit по явному решению владельца содержит email и полный текст сообщений;
- `/health` проверяет HTTP-процесс, `/ready` — базу и компоненты, `/metrics` — метрики;
- heartbeat-файлы используются Docker healthcheck даже при выключенном API;
- секреты интеграций должны содержать не менее 32 символов;
- внешние Remnawave/webhook URL требуют HTTPS и не могут содержать credentials или fragment;
- контейнер непривилегированный, root filesystem read-only, capabilities удалены;
- API использует один статический principal без scopes;
- multi-instance coordination отсутствует;
- редкий дубль Telegram-сообщения допустим из-за at-least-once.
