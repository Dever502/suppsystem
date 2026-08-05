from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_pytest_shard",
    ROOT / "scripts" / "select_pytest_shard.py",
)
assert SPEC is not None and SPEC.loader is not None
SHARDING = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SHARDING
SPEC.loader.exec_module(SHARDING)

ShardFile = SHARDING.ShardFile
build_shards = SHARDING.build_shards
discover_test_files = SHARDING.discover_test_files


def test_discovered_test_files_are_partitioned_exactly_once() -> None:
    files = discover_test_files()
    shards = build_shards(files, 2)
    discovered = {test_file.path for test_file in files}

    assert shards[0]
    assert shards[1]
    assert set(shards[0]).isdisjoint(shards[1])
    assert set().union(*map(set, shards)) == discovered


def test_discovery_excludes_module_level_postgres_marker(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_unit.py").write_text(
        "def test_unit():\n    pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_postgres.py").write_text(
        "import pytest\n\n"
        "pytestmark = pytest.mark.postgres\n\n"
        "def test_postgres_only():\n    pass\n",
        encoding="utf-8",
    )

    files = discover_test_files(tmp_path)

    assert [(test_file.path.as_posix(), test_file.test_count) for test_file in files] == [
        ("tests/test_unit.py", 1)
    ]


def test_discovery_estimates_literal_and_stacked_parametrize_cases(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_parametrized.py"
    test_file.write_text(
        "import pytest\n\n"
        "values = [1, 2, 3, 4]\n\n"
        "@pytest.mark.parametrize('value', [1, 2, pytest.param(3)])\n"
        "def test_literal(value):\n    pass\n\n"
        "@pytest.mark.parametrize('left', (1, 2))\n"
        "@pytest.mark.parametrize(argnames='right', argvalues=['a', 'b', 'c'])\n"
        "async def test_stacked(left, right):\n    pass\n\n"
        "@pytest.mark.parametrize('value', values)\n"
        "def test_dynamic_falls_back_to_one(value):\n    pass\n",
        encoding="utf-8",
    )

    files = discover_test_files(tmp_path)

    assert len(files) == 1
    assert files[0].path == Path("tests/test_parametrized.py")
    assert files[0].test_count == 10


def test_shard_assignment_is_deterministic_and_balanced() -> None:
    files = [
        ShardFile(Path("tests/test_large.py"), test_count=10, size=1_000),
        ShardFile(Path("tests/test_medium.py"), test_count=6, size=1_000),
        ShardFile(Path("tests/test_small_a.py"), test_count=2, size=1_000),
        ShardFile(Path("tests/test_small_b.py"), test_count=2, size=1_000),
    ]

    first = build_shards(files, 2)
    second = build_shards(list(reversed(files)), 2)

    assert first == second
    assert [sum(item.weight for item in files if item.path in shard) for shard in first] == [
        101_000,
        103_000,
    ]


@pytest.mark.parametrize("count", [0, 3])
def test_invalid_shard_count_is_rejected(count: int) -> None:
    files = [ShardFile(Path("tests/test_one.py"), test_count=1, size=1)]

    with pytest.raises(ValueError):
        build_shards(files, count)
