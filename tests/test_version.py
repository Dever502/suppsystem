from __future__ import annotations

import tomllib
from pathlib import Path

import suppsystem
from suppsystem.version import PROJECT_VERSION


def test_package_and_openapi_version_share_release_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert PROJECT_VERSION == project["version"]
    assert suppsystem.__version__ == PROJECT_VERSION
