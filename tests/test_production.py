from __future__ import annotations

from copy import deepcopy

import pytest

from supportbot.production import is_immutable_image_reference, validate_production_compose


def production_config() -> dict[str, object]:
    image = "registry.example/supportbot@sha256:" + "a" * 64
    return {
        "services": {
            "supportbot": {
                "image": image,
                "environment": {
                    "DATABASE_URL": (
                        "postgresql+asyncpg://supportbot_runtime:runtime-password-123"
                        "@postgres:5432/supportbot"
                    ),
                    "MIGRATION_DATABASE_URL": (""),
                    "MIGRATIONS_AT_STARTUP": "false",
                },
                "depends_on": {
                    "postgres": {"condition": "service_healthy"},
                    "postgres-provision": {"condition": "service_completed_successfully"},
                    "postgres-migrate": {"condition": "service_completed_successfully"},
                },
            },
            "postgres": {
                "environment": {
                    "POSTGRES_DB": "supportbot",
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_MIGRATION_USER": "supportbot_migrator",
                    "POSTGRES_RUNTIME_USER": "supportbot_runtime",
                },
                "volumes": [
                    {
                        "type": "volume",
                        "source": "suppsystem_postgres_data",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
            },
            "postgres-provision": {
                "image": image,
                "environment": {
                    "POSTGRES_DB": "supportbot",
                    "POSTGRES_ADMIN_USER": "postgres",
                    "POSTGRES_MIGRATION_USER": "supportbot_migrator",
                    "POSTGRES_RUNTIME_USER": "supportbot_runtime",
                },
            },
            "postgres-migrate": {
                "image": image,
                "environment": {
                    "MIGRATION_DATABASE_URL": (
                        "postgresql+asyncpg://supportbot_migrator:migration-password-123"
                        "@postgres:5432/supportbot"
                    )
                },
                "depends_on": {
                    "postgres-provision": {"condition": "service_completed_successfully"}
                },
            },
        }
    }


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/supportbot@sha256:" + "a" * 64,
    ],
)
def test_immutable_image_references_are_accepted(image: str) -> None:
    assert is_immutable_image_reference(image)


@pytest.mark.parametrize(
    "image",
    [
        "supportbot:latest",
        "supportbot:1.0.0",
        "registry.example/supportbot:" + "b" * 40,
        "supportbot@sha256:not-a-digest",
    ],
)
def test_mutable_image_references_are_rejected(image: str) -> None:
    assert not is_immutable_image_reference(image)


def test_production_compose_accepts_safe_postgres_service_set() -> None:
    validate_production_compose(production_config())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda config: config["services"].pop("postgres"),
            "missing required services",
        ),
        (
            lambda config: config["services"]["supportbot"].update({"image": "supportbot:latest"}),
            "immutable",
        ),
        (
            lambda config: config["services"]["supportbot"].update({"build": "."}),
            "published images",
        ),
        (
            lambda config: config["services"]["supportbot"]["environment"].update(
                {"DATABASE_URL": "sqlite+aiosqlite:////app/data/support.db"}
            ),
            r"postgresql\+asyncpg",
        ),
        (
            lambda config: config["services"]["supportbot"]["environment"].update(
                {
                    "DATABASE_URL": (
                        "postgresql+asyncpg://postgres:runtime-password-123"
                        "@postgres:5432/supportbot"
                    )
                }
            ),
            "unexpected PostgreSQL role",
        ),
        (
            lambda config: config["services"]["supportbot"]["depends_on"].pop("postgres-provision"),
            "postgres-provision",
        ),
        (
            lambda config: config["services"]["supportbot"]["environment"].update(
                {"POSTGRES_ADMIN_PASSWORD": "bootstrap-secret"}
            ),
            "bootstrap password",
        ),
        (
            lambda config: config["services"]["supportbot"]["environment"].update(
                {"MIGRATION_DATABASE_URL": "postgresql+asyncpg://migrator:secret@postgres/db"}
            ),
            "migration credential",
        ),
    ],
)
def test_production_compose_rejects_unsafe_layouts(mutation: object, message: str) -> None:
    config = deepcopy(production_config())
    mutation(config)  # type: ignore[operator]

    with pytest.raises(ValueError, match=message):
        validate_production_compose(config)
