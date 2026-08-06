from __future__ import annotations

from copy import deepcopy

import pytest

from resolvate.production import is_immutable_image_reference, validate_production_compose


def production_config() -> dict[str, object]:
    image = "registry.example/resolvate@sha256:" + "a" * 64
    return {
        "services": {
            "resolvate": {
                "image": image,
                "environment": {
                    "DATABASE_URL": (
                        "postgresql+asyncpg://resolvate_runtime:runtime-password-123"
                        "@postgres:5432/resolvate"
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
                    "POSTGRES_DB": "resolvate",
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_MIGRATION_USER": "resolvate_migrator",
                    "POSTGRES_RUNTIME_USER": "resolvate_runtime",
                },
                "volumes": [
                    {
                        "type": "volume",
                        "source": "resolvate_postgres_data",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
            },
            "postgres-provision": {
                "image": image,
                "environment": {
                    "POSTGRES_DB": "resolvate",
                    "POSTGRES_ADMIN_USER": "postgres",
                    "POSTGRES_MIGRATION_USER": "resolvate_migrator",
                    "POSTGRES_RUNTIME_USER": "resolvate_runtime",
                },
            },
            "postgres-migrate": {
                "image": image,
                "environment": {
                    "MIGRATION_DATABASE_URL": (
                        "postgresql+asyncpg://resolvate_migrator:migration-password-123"
                        "@postgres:5432/resolvate"
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
        "registry.example/resolvate@sha256:" + "a" * 64,
    ],
)
def test_immutable_image_references_are_accepted(image: str) -> None:
    assert is_immutable_image_reference(image)


@pytest.mark.parametrize(
    "image",
    [
        "resolvate:latest",
        "resolvate:3.5.0",
        "registry.example/resolvate:" + "b" * 40,
        "resolvate@sha256:not-a-digest",
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
            lambda config: config["services"]["resolvate"].update({"image": "resolvate:latest"}),
            "immutable",
        ),
        (
            lambda config: config["services"]["resolvate"].update({"build": "."}),
            "published images",
        ),
        (
            lambda config: config["services"]["resolvate"]["environment"].update(
                {"DATABASE_URL": "sqlite+aiosqlite:////app/data/support.db"}
            ),
            r"postgresql\+asyncpg",
        ),
        (
            lambda config: config["services"]["resolvate"]["environment"].update(
                {
                    "DATABASE_URL": (
                        "postgresql+asyncpg://postgres:runtime-password-123@postgres:5432/resolvate"
                    )
                }
            ),
            "unexpected PostgreSQL role",
        ),
        (
            lambda config: config["services"]["resolvate"]["depends_on"].pop("postgres-provision"),
            "postgres-provision",
        ),
        (
            lambda config: config["services"]["resolvate"]["environment"].update(
                {"POSTGRES_ADMIN_PASSWORD": "bootstrap-secret"}
            ),
            "bootstrap password",
        ),
        (
            lambda config: config["services"]["resolvate"]["environment"].update(
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
