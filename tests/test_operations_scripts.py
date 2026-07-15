from __future__ import annotations

import os
import subprocess
from pathlib import Path


def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log_path = tmp_path / "docker.log"
    docker = binary_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$*" in\n'
        "  *'pg_restore --list'*) cat >/dev/null; exit 0 ;;\n"
        "  *'pg_restore --clean'*) cat >/dev/null; "
        'if [ "${FAKE_RESTORE_FAIL:-}" = yes ]; then exit 42; fi ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return binary_dir, log_path


def run_postgres_restore(
    project_root: Path,
    tmp_path: Path,
    *,
    fail_restore: bool,
    failure_injection: bool = False,
) -> subprocess.CompletedProcess[str]:
    binary_dir, log_path = fake_docker(tmp_path)
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"valid-looking-archive")
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "CONFIRM_RESTORE": "yes",
        **({"FAKE_RESTORE_FAIL": "yes"} if fail_restore else {}),
        **(
            {
                "SUPPORTBOT_RESTORE_FAILURE_INJECTION": "after_stop",
                "CONFIRM_RESTORE_FAILURE_INJECTION": "yes",
            }
            if failure_injection
            else {}
        ),
    }
    return subprocess.run(
        ["sh", "scripts/restore.sh", "postgres", str(backup)],
        cwd=project_root,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )


def test_failed_postgres_restore_leaves_application_stopped(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = run_postgres_restore(project_root, tmp_path, fail_restore=True)
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "compose stop supportbot" in log
    assert "compose up --detach --wait supportbot" not in log
    assert "supportbot remains stopped" in result.stderr


def test_successful_postgres_restore_starts_application_after_restore(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = run_postgres_restore(project_root, tmp_path, fail_restore=False)
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert log.index("compose stop supportbot") < log.index("pg_restore --clean")
    assert log.index("pg_restore --clean") < log.index("compose rm --force --stop postgres-migrate")
    assert log.index("compose rm --force --stop postgres-migrate") < log.index(
        "compose up --detach --wait supportbot"
    )


def test_failure_injection_proves_application_stays_stopped(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = run_postgres_restore(
        project_root,
        tmp_path,
        fail_restore=False,
        failure_injection=True,
    )
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert result.returncode == 97
    assert "compose stop supportbot" in log
    assert "pg_restore --clean" not in log
    assert "compose up --detach --wait supportbot" not in log
