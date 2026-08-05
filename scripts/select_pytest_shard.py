from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SAFE_TEST_PATH = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py")
_TEST_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class ShardFile:
    path: Path
    test_count: int
    size: int

    @property
    def weight(self) -> int:
        return self.test_count * 10_000 + self.size


def _is_pytest_mark(expression: ast.expr, name: str) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and expression.attr == name
        and isinstance(expression.value, ast.Attribute)
        and expression.value.attr == "mark"
        and isinstance(expression.value.value, ast.Name)
        and expression.value.value.id == "pytest"
    )


def _contains_postgres_mark(expression: ast.expr) -> bool:
    if _is_pytest_mark(expression, "postgres"):
        return True
    if isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
        return any(_contains_postgres_mark(element) for element in expression.elts)
    return False


def _is_postgres_only_module(tree: ast.Module) -> bool:
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            assigns_pytestmark = any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in statement.targets
            )
            if assigns_pytestmark and _contains_postgres_mark(statement.value):
                return True
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "pytestmark"
            and statement.value is not None
            and _contains_postgres_mark(statement.value)
        ):
            return True
    return False


def _parametrize_size(decorator: ast.expr) -> int:
    if not isinstance(decorator, ast.Call) or not _is_pytest_mark(decorator.func, "parametrize"):
        return 1

    values: ast.expr | None = decorator.args[1] if len(decorator.args) >= 2 else None
    if values is None:
        values = next(
            (keyword.value for keyword in decorator.keywords if keyword.arg == "argvalues"),
            None,
        )
    if not isinstance(values, (ast.List, ast.Set, ast.Tuple)):
        return 1
    return max(len(values.elts), 1)


def _estimated_test_count(tree: ast.Module) -> int:
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, _TEST_FUNCTION_TYPES) or not node.name.startswith("test_"):
            continue
        cases = 1
        for decorator in node.decorator_list:
            cases *= _parametrize_size(decorator)
        total += cases
    return total


def discover_test_files(root: Path = ROOT) -> list[ShardFile]:
    files: list[ShardFile] = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        relative_path = path.relative_to(root)
        if _SAFE_TEST_PATH.fullmatch(relative_path.as_posix()) is None:
            raise RuntimeError(f"Unsafe pytest path cannot be sharded: {relative_path}")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _is_postgres_only_module(tree):
            continue
        test_count = _estimated_test_count(tree)
        files.append(
            ShardFile(
                path=relative_path,
                test_count=max(test_count, 1),
                size=path.stat().st_size,
            )
        )
    if not files:
        raise RuntimeError("No pytest files were discovered")
    return files


def build_shards(files: list[ShardFile], count: int) -> list[list[Path]]:
    if count < 1:
        raise ValueError("Shard count must be positive")
    if count > len(files):
        raise ValueError("Shard count cannot exceed the number of test files")

    shards: list[list[Path]] = [[] for _ in range(count)]
    weights = [0] * count
    for test_file in sorted(files, key=lambda item: (-item.weight, item.path.as_posix())):
        shard_index = min(range(count), key=lambda index: (weights[index], index))
        shards[shard_index].append(test_file.path)
        weights[shard_index] += test_file.weight
    return [sorted(shard) for shard in shards]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a deterministic pytest file shard")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--index", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.index < 0 or args.index >= args.count:
        raise SystemExit("Shard index must be between zero and count minus one")
    shard = build_shards(discover_test_files(), args.count)[args.index]
    print("\n".join(path.as_posix() for path in shard))


if __name__ == "__main__":
    main()
