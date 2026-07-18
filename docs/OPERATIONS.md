# Эксплуатация

Руководство по установке, настройке, работе, обновлению и восстановлению Telegram Support
Platform. Внутренняя реализация описана в [техническом документе](TECHNICAL.md).

## Требования

- Linux-сервер с Docker Engine и Docker Compose v2;
- Telegram-бот;
- закрытая Telegram supergroup с включёнными Topics;
- Telegram ID операторов;
- HTTPS reverse proxy, если operator API доступен извне.

Для разработки нужны Python 3.12+ и `uv`.

## Подготовка Telegram

1. Создайте бота через `@BotFather`.
2. Создайте приватную supergroup и включите Topics.
3. Добавьте бота администратором с правом управления темами.
4. Определите числовой ID группы и Telegram ID операторов.

`SUPPORT_GROUP_ID` у supergroup обычно начинается с `-100`. При старте приложение проверяет
тип группы, режим форума и права бота. Ошибка preflight останавливает запуск.

## Запуск с SQLite

```bash
cp .env.example .env
```

Минимальная конфигурация:

```dotenv
SUPPORT_BOT_TOKEN=replace-with-bot-token
SUPPORT_GROUP_ID=replace-with-forum-group-id
FULL_ADMIN_TELEGRAM_IDS=replace-with-admin-id
DATA_DIR=./data
```

```bash
docker build --tag suppsystem:local .
export APP_IMAGE=suppsystem:local
export SUPPORTBOT_ENV_FILE="$PWD/.env"
docker compose --env-file .env -f compose.production.sqlite.yaml up -d
docker compose -f compose.production.sqlite.yaml ps
```

SQLite-база и heartbeat-файлы хранятся в `DATA_DIR`. В Docker этот каталог подключён к
volume `support_data`: удаление контейнера не удаляет данные, удаление volume — удаляет.

## Запуск с PostgreSQL

```dotenv
POSTGRES_DB=supportbot
POSTGRES_ADMIN_USER=postgres
POSTGRES_ADMIN_PASSWORD=первый-url-safe-случайный-пароль
POSTGRES_MIGRATION_USER=supportbot_migrator
POSTGRES_MIGRATION_PASSWORD=второй-url-safe-случайный-пароль
POSTGRES_RUNTIME_USER=supportbot_runtime
POSTGRES_RUNTIME_PASSWORD=третий-url-safe-случайный-пароль
```

```bash
docker compose --env-file .env -f compose.production.postgres.yaml up -d
```

Используйте три разных значения длиной не менее 16 символов, например результаты
`openssl rand -hex 24`. Compose формирует runtime `DATABASE_URL` и отдельный
`MIGRATION_DATABASE_URL`. One-shot service `postgres-provision` выдаёт минимальные полномочия,
после него `postgres-migrate` применяет Alembic. Оба должны успешно завершиться до запуска
приложения; migration credential в `supportbot` не передаётся. PostgreSQL пока рассчитан на один
экземпляр.

При обновлении старого volume, созданного с `POSTGRES_USER=supportbot`, укажите
`POSTGRES_ADMIN_USER=supportbot` и старый пароль как `POSTGRES_ADMIN_PASSWORD`: именно эта legacy
роль является bootstrap-superuser существующего кластера. После успешного перехода приложение её
не использует.

## Конфигурация

### Telegram и база

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `SUPPORT_BOT_TOKEN` | обязательна | токен Telegram-бота |
| `SUPPORT_GROUP_ID` | обязательна | ID закрытой Forum-группы |
| `FULL_ADMIN_TELEGRAM_IDS` | пусто | ID администраторов через запятую |
| `OPERATOR_TELEGRAM_IDS` | пусто | ID операторов через запятую |
| `READONLY_OPERATOR_TELEGRAM_IDS` | пусто | ID операторов только для чтения |
| `ADMIN_TELEGRAM_IDS` | пусто | устаревший alias full admin |
| `DATA_DIR` | `./data` | каталог runtime-данных: SQLite и heartbeat-файлы |
| `DATABASE_URL` | SQLite в `DATA_DIR/support.db` | optional SQLAlchemy async URL override |
| `MIGRATION_DATABASE_URL` | значение `DATABASE_URL` | отдельный migration target; production Compose задаёт migration role |
| `MIGRATIONS_AT_STARTUP` | `true` | production PostgreSQL Compose ставит `false` и использует one-shot migration service |
| `LOG_LEVEL` | `INFO` | уровень логирования |

Один Telegram ID нельзя включать в несколько ролей.

### Доставка

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DELIVERY_POLL_INTERVAL_SECONDS` | `1` | интервал чтения очереди |
| `DELIVERY_MAX_ATTEMPTS` | `8` | предел попыток доставки |
| `TELEGRAM_MIN_REQUEST_INTERVAL_SECONDS` | `0.05` | интервал Telegram-запросов |

Уменьшать интервалы без измерений не следует: это повышает риск rate limit и конкуренции SQLite.

### Operator API

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `API_ENABLED` | `false` | включает API |
| `API_HOST` | `0.0.0.0` | bind внутри контейнера |
| `API_PUBLISH_HOST` | `127.0.0.1` | адрес публикации Docker-порта |
| `API_PORT` | `8080` | порт |
| `API_ADMIN_TOKEN` | пусто | токен заголовка `X-API-Token`, минимум 32 символа |
| `API_UNSAFE_DISABLE_AUTH` | `false` | отключает auth только при loopback bind |
| `API_OPERATOR_TELEGRAM_ID` | `0` | технический actor ID аудита API |
| `API_RATE_LIMIT_REQUESTS` | `120` | запросов на IP за окно |
| `API_RATE_LIMIT_WINDOW_SECONDS` | `60` | окно rate limit |
| `API_AUTH_FAILURE_LIMIT` | `10` | неудачных авторизаций на IP |
| `API_AUTH_FAILURE_WINDOW_SECONDS` | `60` | окно защиты авторизации |
| `API_TRUSTED_PROXY_IPS` | пусто | доверенные proxy IP/CIDR для `X-Forwarded-For` и `X-Real-IP` |

```dotenv
API_ENABLED=true
API_PUBLISH_HOST=127.0.0.1
API_PORT=8080
API_ADMIN_TOKEN=случайный-секрет-длиной-не-менее-32-символов
# Если API стоит за reverse proxy, укажите IP/CIDR proxy для корректного rate limit.
# API_TRUSTED_PROXY_IPS=127.0.0.1
```

Не публикуйте порт напрямую. Оставьте loopback и используйте HTTPS reverse proxy. Клиент передаёт
`X-API-Token: <token>`; Swagger UI доступен на `/docs`.

Rate limit и защита от перебора токена хранятся в памяти одного процесса: состояние сбрасывается
при рестарте и не координируется между экземплярами. В `v0.1.0` статический токен даёт полный
административный доступ, не имеет scopes, срока действия и отдельного механизма revocation. Для
rotation замените `API_ADMIN_TOKEN` в secret environment и перезапустите приложение.

Каждая мутация требует `X-Idempotency-Key`. Ключ scoped по операции и тикету, а в БД сохраняются
fingerprint смыслового payload и исходный ответ. Точный повтор, в том числе после рестарта,
возвращает прежний `changed`; повтор того же scoped key с другим payload получает `409 Conflict` и
не создаёт эффект. Используйте новый случайный ключ для каждой новой логической команды и тот же
ключ только для её безопасного retry.

Пример Nginx-конфига для TLS proxy находится в
[`deploy/nginx/supportbot-api.conf.example`](../deploy/nginx/supportbot-api.conf.example). В нём
нужно заменить `support.example.invalid` и пути сертификатов. Приложение при этом должно оставаться
опубликованным только на loopback через `API_PUBLISH_HOST=127.0.0.1`.
Nginx template намеренно заменяет, а не дополняет входящий `X-Forwarded-For`. Приложение принимает
forwarded headers только от `API_TRUSTED_PROXY_IPS`, проверяет всю цепочку справа налево и выбирает
ближайший недоверенный hop. Невалидная или чрезмерно длинная цепочка игнорируется.

Uvicorn и Telegram polling входят в один failure domain: неожиданная остановка API помечает
readiness как degraded, останавливает polling и завершает процесс для перезапуска внешним
supervisor. При штатном завершении новые HTTP/Telegram запросы прекращаются, workers дренируются,
и только затем закрываются используемые ими Telegram и HTTP sessions.

### Remnawave

Поддерживается контракт Remnawave 2.8.0. Интеграция опциональна и выключена по умолчанию.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `REMNAWAVE_ENABLED` | `false` | включает интеграцию |
| `REMNAWAVE_BASE_URL` | пусто | HTTPS URL панели |
| `REMNAWAVE_API_TOKEN` | пусто | token, минимум 32 символа |
| `REMNAWAVE_TIMEOUT_SECONDS` | `5` | HTTP timeout |
| `REMNAWAVE_RECONCILE_DELAY_SECONDS` | `10` | задержка перед сверкой результата |

Включение требует URL и token. Не задавайте reconcile delay равным нулю вне тестов. При
`unknown outcome` не повторяйте команду: durable worker сверит состояние без повторной мутации.
Если результат останется неоднозначным, действие сохранится как `inconclusive` и потребует
явного решения full admin.

### Notification webhook

Интеграция опциональна и выключена по умолчанию. Доставка имеет семантику at-least-once.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `NOTIFICATION_WEBHOOK_ENABLED` | `false` | включает отправку событий |
| `NOTIFICATION_WEBHOOK_URL` | пусто | HTTPS endpoint |
| `NOTIFICATION_WEBHOOK_SECRET` | пусто | HMAC secret, минимум 32 символа |
| `NOTIFICATION_WEBHOOK_TIMEOUT_SECONDS` | `5` | HTTP timeout |
| `NOTIFICATION_WEBHOOK_MAX_ATTEMPTS` | `8` | предел повторов |
| `NOTIFICATION_WEBHOOK_POLL_INTERVAL_SECONDS` | `1` | интервал чтения очереди |

Внешние URL требуют HTTPS. HTTP разрешён только для localhost/loopback. Credentials внутри URL и
fragments запрещены. Получатель должен проверить HMAC-SHA256 подпись raw body и атомарно
дедуплицировать события по `event_id` до выполнения побочного эффекта.

## Команды операторов

Команды выполняются внутри связанной Forum-темы.

| Команда | Доступ | Действие |
| --- | --- | --- |
| `/info` | все роли | локальная информация о тикете |
| `/subinfo` | все роли | свежие данные подписки Remnawave |
| `/note текст` | operator/full admin | внутренняя заметка |
| `/stop` | operator/full admin | закрыть и уведомить клиента |
| `/hidestop` | operator/full admin | закрыть без уведомления |
| `/gift N` | operator/full admin | продлить подписку на 1–9999 дней |
| `/resetkey` | operator/full admin | заменить protocol credentials |
| `/revokelink` | operator/full admin | заменить subscription URL и уведомить клиента |
| `/resetdevices` | operator/full admin | удалить HWID-устройства |
| `/resolvepanel UUID applied\|not_applied` | full admin | разрешить проверенный `inconclusive` |
| `/block` | full access | заблокировать клиента |
| `/unblock` | full access | снять блокировку |
| `/stopall` | full access | закрыть все открытые тикеты |
| `/synctopics` | full access | синхронизировать Forum-темы |

Обычный ответ в теме уходит клиенту. Неизвестная команда блокируется и не пересылается. Ответ в
закрытом тикете повторно открывает его.

## Production layout

Рекомендуемый single-host layout:

```text
/opt/supportbot/.env            # Compose/runtime configuration and secrets, mode 0600
/opt/supportbot/deployment.env  # current immutable APP_IMAGE, managed by deploy.sh
/opt/supportbot/rollback.env    # previous healthy immutable APP_IMAGE
Repository checkout                 # versioned Compose manifests and operation scripts
Docker volume                       # suppsystem_postgres_data with PostgreSQL data
Docker volume                       # suppsystem_support_data with heartbeat files
```

Контейнер запускается непривилегированным пользователем, с read-only root filesystem и writable
volume только для runtime data. Production PostgreSQL хранится в `suppsystem_postgres_data`, а
`support_data` содержит heartbeat-файлы. Bootstrap password принудительно очищается из environment
контейнера приложения и доступен только `postgres`/`postgres-provision`. Operator API не должен
слушать публичный интерфейс напрямую; оставьте
`API_PUBLISH_HOST=127.0.0.1` и публикуйте его через HTTPS reverse proxy.

Production deploy использует самостоятельный `compose.production.postgres.yaml`, рендерит
итоговую конфигурацию и проверяет PostgreSQL backend, разные роли, persistent volumes, dependencies
и immutable image. Текущий и предыдущий успешно запущенные image сохраняются в
`deployment.env` и `rollback.env`.

Первый и последующие deploy:

```bash
DEPLOY_DIR=/opt/supportbot \
  sh scripts/deploy.sh deploy registry.example/supportbot:<40-символьный-commit-sha>
DEPLOY_DIR=/opt/supportbot sh scripts/production-compose.sh ps
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/ready
```

Rollback разрешён только на ранее успешно запущенный immutable image:

```bash
DEPLOY_DIR=/opt/supportbot sh scripts/deploy.sh rollback
```

Перед rollback убедитесь, что миграции candidate совместимы со старым image. Скрипт не обещает
автоматический downgrade схемы и не подменяет операторское решение о совместимости.

## Повседневные команды

```bash
DEPLOY_DIR=/opt/supportbot sh scripts/production-compose.sh ps
DEPLOY_DIR=/opt/supportbot sh scripts/production-compose.sh logs -f --tail=200 supportbot
DEPLOY_DIR=/opt/supportbot sh scripts/production-compose.sh restart supportbot
```

Production wrapper использует самостоятельный manifest
`compose.production.postgres.yaml`.

## GitHub Actions и release artifacts

Публичный workflow выполняет полный verify и PostgreSQL matrix на pull request и push. После
успешных проверок push в `main` или release tag собирает image через Buildx, публикует его в GHCR,
выполняет migration/container smoke, Trivy HIGH/CRITICAL gate и создаёт CycloneDX SBOM. Все
внешние actions закреплены полными commit SHA.

Workflow не использует self-hosted runners, production secrets, Docker socket mount или deploy
команды. Production deployment остаётся явной операторской операцией через `scripts/deploy.sh` и
принимает только проверенный GHCR reference с immutable digest.

Commit-SHA tag служит читаемым идентификатором, но `image.env` и `release-evidence.txt` фиксируют
фактический `IMAGE_REFERENCE` по digest. При release tag тот же digest дополнительно получает
version tag. Trivy JSON, CycloneDX SBOM, checksums и evidence сохраняются как GitHub Actions
artifact одного job; после перевода repository в public workflow также публикует artifact
attestation в GHCR.

Перед deploy скачайте artifact одного workflow run и проверьте:

```bash
sha256sum --check artifact-checksums.txt
grep '^image_reference=.*@sha256:' release-evidence.txt
```

GitHub Actions отвечает только за verification и публикацию GHCR artifact; доступ к production
host в workflow отсутствует.

## Health, readiness и метрики

```bash
sh scripts/production-compose.sh ps
docker inspect --format '{{json .State.Health}}' "$(sh scripts/production-compose.sh ps -q supportbot)"
```

При включённом API:

```bash
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/health
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/ready
curl -H "X-API-Token: $API_ADMIN_TOKEN" http://127.0.0.1:8080/metrics
```

- `/health` подтверждает работу HTTP-процесса;
- `/ready` возвращает 503, если база или настроенный компонент не готовы;
- `/metrics` предназначен для Prometheus. Стартовые dashboard-панели, alerts и runbook описаны
  в [OBSERVABILITY.md](OBSERVABILITY.md).

Логи — структурированный JSON. Для поиска полезны `event`, `trace_id`, `ticket_id`,
`delivery_id` и `operator_action_id`.

## Обновление

Перед обновлением создайте backup. Production Compose сначала запускает migration service с
отдельным credential, затем запускает приложение только с runtime connections и урезанными
полномочиями.

```bash
PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/supportbot \
  sh scripts/backup.sh postgres /srv/backups/support-before-update.dump
DEPLOY_DIR=/opt/supportbot \
  sh scripts/deploy.sh deploy registry.example/supportbot:<immutable-sha-or-digest>
```

Не запускайте старую версию поверх обновлённой схемы без явно поддерживаемого downgrade.

## Backup и восстановление

Храните копии вне Docker volumes, шифруйте и регулярно проверяйте восстановлением. Backup содержит
пользовательские данные.

### SQLite

Перед командами экспортируйте `APP_IMAGE` и `SUPPORTBOT_ENV_FILE` так же, как при запуске SQLite.

```bash
COMPOSE_FILE=compose.production.sqlite.yaml ./scripts/backup.sh sqlite /srv/backups/support-$(date +%F-%H%M).db
CONFIRM_RESTORE=yes COMPOSE_FILE=compose.production.sqlite.yaml ./scripts/restore.sh sqlite /srv/backups/support-backup.db
```

Backup создаётся без остановки. Фактический путь берётся из `DATABASE_URL`, а не предполагается
равным `DATA_DIR/support.db`. Для сохранности volume файл обязан находиться внутри `DATA_DIR`.
Restore сначала проверяет `PRAGMA integrity_check`, затем останавливает приложение, создаёт
проверенную копию рядом с целевым файлом и атомарно заменяет базу. При любой ошибке после остановки
приложение остаётся остановленным.

### PostgreSQL

```bash
PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/supportbot \
  sh scripts/backup.sh postgres /srv/backups/support-$(date +%F-%H%M).dump
CONFIRM_RESTORE=yes PRODUCTION_DEPLOYMENT=yes DEPLOY_DIR=/opt/supportbot \
  sh scripts/restore.sh postgres /srv/backups/support-backup.dump
```

Archive проверяется через `pg_restore --list` до остановки. Restore выполняется migration role с
`--clean --if-exists --exit-on-error`; runtime role используется для `pg_dump`. Успешный restore
удаляет прежний successful `postgres-migrate`, повторно применяет миграции, запускает приложение
через полный production service set и ждёт container health. Неуспешный
restore никогда не запускает приложение автоматически: проверьте БД вручную и затем выполните
`scripts/production-compose.sh up --detach --wait supportbot`.

### Production data path drill

Drill выполняется только на отдельном стенде с реальными PostgreSQL, Telegram-конфигурацией и
immutable baseline/candidate images. Каталог должен содержать файл
`.supportbot-drill-environment` со строкой `isolated`. Сценарий делает clean deploy, upgrade,
rollback, backup, намеренно падающий restore после остановки, успешный restore и сравнение
контрольного fingerprint.

```bash
CONFIRM_PRODUCTION_DATA_PATH_DRILL=isolated \
CONFIRM_SCHEMA_COMPATIBLE_ROLLBACK=yes \
DEPLOY_DIR=/opt/suppsystem-drill \
  sh scripts/drill_production_data_path.sh \
  registry.example/supportbot:<baseline-sha> \
  registry.example/supportbot:<candidate-sha> \
  /srv/drill-reports/data-path-$(date +%F-%H%M).log
```

Сохраните report и `.postgres.dump` как закрытые release evidence: они относятся к production
операциям и могут раскрывать объёмы данных. Drill является разрушительным и не запускается на
боевой инсталляции.

## Разработка

```bash
make install          # locked dependencies
make run              # запуск
make migrate          # миграции
make test             # тесты
make lint             # Ruff
make format-check     # форматирование
make typecheck        # strict mypy
make migration-check  # соответствие ORM и Alembic
make license-check    # лицензии зависимостей
make verify           # все проверки
make test-postgres    # PostgreSQL migration/concurrency matrix в одноразовой test-only БД
make production-preflight # проверка уже записанного production deployment state
```

Изменение ORM требует новой Alembic-миграции и проверки fresh install и upgrade. Применённые
миграции задним числом не редактируются.

## Диагностика

### Приложение не запускается

Проверьте `sh scripts/production-compose.sh logs --tail=200 supportbot`, обязательные переменные, пересечения ролей,
длину секретов, доступность Telegram и базы.

Если лог начинается с `Configuration error`, исправьте `.env` и перезапустите сервис. Например,
ошибка `Telegram operator roles must not overlap` означает, что один Telegram ID указан сразу в
нескольких переменных ролей (`FULL_ADMIN_TELEGRAM_IDS`, `OPERATOR_TELEGRAM_IDS`,
`READONLY_OPERATOR_TELEGRAM_IDS` или устаревшей `ADMIN_TELEGRAM_IDS`). Оставьте ID только в одной
роли. В production файл обычно находится в `/opt/supportbot/.env`.

### Не проходит Telegram preflight

Группа должна быть supergroup с Topics. Бот должен находиться в ней, иметь право управления
темами, а токен должен принадлежать именно добавленному боту.

### Сообщения не доставляются

Проверьте health workers и delivery events. Временные ошибки повторяются. `failed` означает
исчерпание попыток или постоянную ошибку. После удаления темы приложение пытается восстановить её
и перенаправить незавершённые сообщения.

### Remnawave вернул unknown outcome

Не повторяйте мутацию вслепую: пока идёт автоматическая read-only сверка, следующая команда для
этого тикета заблокирована. Найдите `operator_action_id` и дождитесь `completed`, `not_applied` или
`inconclusive`. Недоступные и некорректные read-ответы повторяются с backoff; после 20 неудачных
попыток действие безопасно переходит в `inconclusive`.

При `inconclusive` уведомление содержит UUID действия. Full admin должен независимо установить
судьбу именно исходного вызова и выполнить в той же теме одну из команд:

```text
/resolvepanel <operator_action_uuid> applied
/resolvepanel <operator_action_uuid> not_applied
```

`applied` допустим только при доказательстве, что исходная мутация выполнилась, `not_applied` —
только при доказательстве обратного. Одного текущего состояния Remnawave недостаточно: оно могло
измениться конкурентно. Если доказательства нет, оставьте действие заблокированным. Команда не
повторяет мутацию и сохраняет actor, время, решение и idempotency key в аудите. Исключение —
`/revokelink ... applied`: выполняется read-only lookup новой ссылки для durable-уведомления
клиента. Если ссылка недоступна или не изменилась, решение не фиксируется.

### `/ready` возвращает 503

Ответ показывает проблемный компонент. Проверьте database, API, panel, delivery worker и
notification worker. Успешный `/health` подтверждает только живой HTTP-процесс.

## Production checklist

- `.env` защищён и не хранится в Git;
- секреты уникальны и длиннее 32 символов;
- PostgreSQL использует три разных стойких пароля;
- API опубликован только через HTTPS reverse proxy;
- настроены сбор логов, Prometheus и оповещения;
- backup уходит во внешнее защищённое хранилище;
- восстановление регулярно проверяется;
- после обновления проверяются readiness, логи и тестовый тикет.
