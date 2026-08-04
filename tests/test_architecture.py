from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/suppsystem")


def imported_modules(module: str) -> set[str]:
    tree = ast.parse((SOURCE_ROOT / f"{module}.py").read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_service_contracts_do_not_depend_on_persistence_or_transports() -> None:
    imports = imported_modules("service_types")

    assert not any(
        name.startswith(("sqlalchemy", "aiogram", "fastapi", "suppsystem.database"))
        for name in imports
    )


def test_telegram_formatting_is_independent_from_aiogram_and_adapter() -> None:
    imports = imported_modules("telegram_formatting")

    assert not any(name.startswith(("aiogram", "suppsystem.telegram_adapter")) for name in imports)


def test_application_services_do_not_import_transport_layers() -> None:
    for module in (
        "services",
        "ticket_service_base",
        "ticket_topic_service",
        "ticket_lifecycle_service",
        "ticket_message_service",
        "outbox_repository",
        "panel",
        "panel_types",
        "panel_action_service",
        "panel_reconciliation_service",
        "panel_persistence_service",
        "quick_replies",
    ):
        imports = imported_modules(module)
        assert not any(
            name.startswith(("aiogram", "fastapi", "suppsystem.telegram_adapter"))
            for name in imports
        ), module


def test_split_telegram_handlers_do_not_import_persistence_frameworks() -> None:
    for module in (
        "telegram_adapter",
        "telegram_user_handlers",
        "telegram_operator_handlers",
        "telegram_topic_manager",
        "telegram_message_utils",
        "telegram_quick_replies",
    ):
        imports = imported_modules(module)
        assert not any(name.startswith("sqlalchemy") for name in imports), module


def test_public_facades_only_compose_specialized_services() -> None:
    limits = {
        "services": 30,
        "panel": 40,
        "telegram_adapter": 100,
    }
    for module, maximum_lines in limits.items():
        line_count = len((SOURCE_ROOT / f"{module}.py").read_text().splitlines())
        assert line_count <= maximum_lines, module
