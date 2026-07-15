from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from typing import Any

from sqlalchemy import make_url

IMMUTABLE_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")
IMMUTABLE_TAG_PATTERN = re.compile(r":[0-9a-f]{40,64}$")


def is_immutable_image_reference(image: str) -> bool:
    return bool(IMMUTABLE_DIGEST_PATTERN.search(image) or IMMUTABLE_TAG_PATTERN.search(image))


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Production Compose {name} must be a mapping")
    return value


def _environment(service: Mapping[str, Any], *, service_name: str) -> Mapping[str, Any]:
    return _mapping(
        service.get("environment"),
        name=f"service {service_name!r} environment",
    )


def _validate_database_url(
    value: object,
    *,
    variable_name: str,
    expected_user: object,
    expected_database: object,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{variable_name} is missing from rendered production Compose")
    try:
        parsed = make_url(value)
    except Exception as error:
        raise ValueError(f"{variable_name} must be a valid SQLAlchemy URL") from error
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError(f"{variable_name} must use postgresql+asyncpg")
    if parsed.host != "postgres" or parsed.port not in {None, 5432}:
        raise ValueError(f"{variable_name} must target the Compose postgres service")
    if not isinstance(expected_user, str) or parsed.username != expected_user:
        raise ValueError(f"{variable_name} uses an unexpected PostgreSQL role")
    if not isinstance(expected_database, str) or parsed.database != expected_database:
        raise ValueError(f"{variable_name} targets an unexpected PostgreSQL database")
    if not parsed.password:
        raise ValueError(f"{variable_name} must include a password")


def validate_production_compose(config: Mapping[str, Any]) -> None:
    services = _mapping(config.get("services"), name="services")
    required_services = {"supportbot", "postgres", "postgres-provision", "postgres-migrate"}
    missing_services = required_services - set(services)
    if missing_services:
        raise ValueError(
            "Production Compose is missing required services: "
            + ", ".join(sorted(missing_services))
        )

    supportbot = _mapping(services["supportbot"], name="supportbot service")
    postgres = _mapping(services["postgres"], name="postgres service")
    provision = _mapping(services["postgres-provision"], name="postgres-provision service")
    migrate = _mapping(services["postgres-migrate"], name="postgres-migrate service")
    supportbot_environment = _environment(supportbot, service_name="supportbot")
    postgres_environment = _environment(postgres, service_name="postgres")
    provision_environment = _environment(provision, service_name="postgres-provision")
    migrate_environment = _environment(migrate, service_name="postgres-migrate")

    supportbot_image = supportbot.get("image")
    provision_image = provision.get("image")
    migrate_image = migrate.get("image")
    if any(service.get("build") is not None for service in (supportbot, provision, migrate)):
        raise ValueError("Production services must use published images, not local builds")
    if not isinstance(supportbot_image, str) or not is_immutable_image_reference(supportbot_image):
        raise ValueError("Production supportbot image must use an immutable SHA tag or digest")
    if provision_image != supportbot_image or migrate_image != supportbot_image:
        raise ValueError("supportbot and PostgreSQL one-shot services must use the same image")

    database_name = postgres_environment.get("POSTGRES_DB")
    admin_user = postgres_environment.get("POSTGRES_USER")
    migration_user = postgres_environment.get("POSTGRES_MIGRATION_USER")
    runtime_user = postgres_environment.get("POSTGRES_RUNTIME_USER")
    if not all(isinstance(item, str) and item for item in (database_name, admin_user)):
        raise ValueError("Rendered PostgreSQL bootstrap identity is incomplete")
    if len({admin_user, migration_user, runtime_user}) != 3:
        raise ValueError("PostgreSQL admin, migration and runtime roles must be distinct")
    if supportbot_environment.get("POSTGRES_ADMIN_PASSWORD") not in {None, ""}:
        raise ValueError("supportbot must not receive the PostgreSQL bootstrap password")
    if supportbot_environment.get("MIGRATION_DATABASE_URL") not in {None, ""}:
        raise ValueError("supportbot must not receive the PostgreSQL migration credential")
    if str(supportbot_environment.get("MIGRATIONS_AT_STARTUP", "")).casefold() != "false":
        raise ValueError("production supportbot must delegate migrations to postgres-migrate")
    for variable_name, expected_value in (
        ("POSTGRES_DB", database_name),
        ("POSTGRES_ADMIN_USER", admin_user),
        ("POSTGRES_MIGRATION_USER", migration_user),
        ("POSTGRES_RUNTIME_USER", runtime_user),
    ):
        if provision_environment.get(variable_name) != expected_value:
            raise ValueError(
                f"postgres-provision {variable_name} does not match the postgres service"
            )

    _validate_database_url(
        supportbot_environment.get("DATABASE_URL"),
        variable_name="DATABASE_URL",
        expected_user=runtime_user,
        expected_database=database_name,
    )
    _validate_database_url(
        migrate_environment.get("MIGRATION_DATABASE_URL"),
        variable_name="MIGRATION_DATABASE_URL",
        expected_user=migration_user,
        expected_database=database_name,
    )

    dependencies = _mapping(supportbot.get("depends_on"), name="supportbot depends_on")
    postgres_dependency = _mapping(
        dependencies.get("postgres"), name="supportbot postgres dependency"
    )
    provision_dependency = _mapping(
        dependencies.get("postgres-provision"),
        name="supportbot postgres-provision dependency",
    )
    migrate_dependency = _mapping(
        dependencies.get("postgres-migrate"),
        name="supportbot postgres-migrate dependency",
    )
    if postgres_dependency.get("condition") != "service_healthy":
        raise ValueError("supportbot must wait for a healthy postgres service")
    if provision_dependency.get("condition") != "service_completed_successfully":
        raise ValueError("supportbot must wait for successful PostgreSQL role provisioning")
    if migrate_dependency.get("condition") != "service_completed_successfully":
        raise ValueError("supportbot must wait for successful database migrations")

    migrate_dependencies = _mapping(migrate.get("depends_on"), name="postgres-migrate depends_on")
    migrate_provision_dependency = _mapping(
        migrate_dependencies.get("postgres-provision"),
        name="postgres-migrate postgres-provision dependency",
    )
    if migrate_provision_dependency.get("condition") != "service_completed_successfully":
        raise ValueError("postgres-migrate must wait for successful role provisioning")

    volumes = postgres.get("volumes")
    if not isinstance(volumes, list) or not any(
        isinstance(volume, Mapping)
        and volume.get("target") == "/var/lib/postgresql/data"
        and volume.get("type") == "volume"
        for volume in volumes
    ):
        raise ValueError("postgres must use a persistent named data volume")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("rendered Compose configuration must be a JSON object")
        validate_production_compose(payload)
    except (ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Production Compose preflight failed: {error}") from error
    print("Production Compose preflight passed")


if __name__ == "__main__":
    main()
