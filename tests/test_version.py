from __future__ import annotations

import tomllib
from pathlib import Path

import resolvate
from resolvate.version import PROJECT_VERSION


def test_package_and_openapi_version_share_release_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert PROJECT_VERSION == project["version"]
    assert resolvate.__version__ == PROJECT_VERSION
