from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_publication.py"


def _repository(tmp_path: Path, content: str) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    workflow_directory = tmp_path / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  verify:\n    runs-on: ubuntu-24.04\n"
        "    steps:\n      - run: 'true'\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_publication_check_detects_high_confidence_secrets_and_private_infrastructure(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        "\n".join(
            (
                "token=glpat-abcdefghijklmnop",  # publication-check: allow-fixture
                "remote=https://gitlab.x/a",  # publication-check: allow-fixture
                "path=/home/developer/private/project",  # publication-check: allow-fixture
                "Author: Person <person@domain.dev>",  # publication-check: allow-fixture
            )
        ),
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    for expected in (
        "GitLab access token",
        "private GitLab hostname",
        "developer home path",
        "personal email / PII",
    ):
        assert expected in result.stderr


def test_publication_check_accepts_documented_placeholders(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        "security@example.com https://gitlab.com/group/project /opt/suppsystem/.env",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Publication snapshot check passed" in result.stdout


def test_publication_check_rejects_legacy_or_unsafe_ci(tmp_path: Path) -> None:
    root = _repository(tmp_path, "safe")
    (root / ".gitlab-ci.yml").write_text("verify: {script: ['true']}\n", encoding="utf-8")
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        "name: CI\non: [pull_request_target]\npermissions: write-all\n"
        "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
        "      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    for expected in (
        "legacy GitLab CI",
        "privileged pull-request trigger",
        "unbounded GitHub workflow permissions",
        "self-hosted runner",
        "GitHub Action is not SHA-pinned",
    ):
        assert expected in result.stderr
