from __future__ import annotations

import os
import subprocess
from pathlib import Path

BASELINE_IMAGE = "registry.example/supportbot:" + "a" * 40
CANDIDATE_IMAGE = "registry.example/supportbot:" + "b" * 40


def deploy_environment(tmp_path: Path, *, fail_up: bool = False) -> tuple[dict[str, str], Path]:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / ".env").write_text("SUPPORT_BOT_TOKEN=test\n", encoding="utf-8")
    (deploy_dir / "deployment.env").write_text(
        f"APP_IMAGE={BASELINE_IMAGE}\n",
        encoding="utf-8",
    )
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log_path = tmp_path / "docker.log"
    docker = binary_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$*" in\n'
        "  *' config --format json'*) printf '{\"services\":{}}\\n'; exit 0 ;;\n"
        "  *' up --detach --wait'*) "
        'if [ "${FAKE_UP_FAIL:-}" = yes ]; then exit 42; fi ;;\n'
        "esac\n"
        "cat >/dev/null 2>&1 || true\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "DEPLOY_DIR": str(deploy_dir),
        **({"FAKE_UP_FAIL": "yes"} if fail_up else {}),
    }
    return environment, deploy_dir


def run_deploy(project_root: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "scripts/deploy.sh", "deploy", CANDIDATE_IMAGE],
        cwd=project_root,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )


def test_deploy_records_current_and_rollback_images_after_health(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment, deploy_dir = deploy_environment(tmp_path)

    result = run_deploy(project_root, environment)
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert f"APP_IMAGE={CANDIDATE_IMAGE}" in (deploy_dir / "deployment.env").read_text()
    assert f"APP_IMAGE={BASELINE_IMAGE}" in (deploy_dir / "rollback.env").read_text()
    assert "compose.production.sqlite.yaml" not in log
    assert "compose.production.postgres.yaml" in log
    assert "--remove-orphans" not in log


def test_failed_deploy_does_not_replace_last_healthy_state(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment, deploy_dir = deploy_environment(tmp_path, fail_up=True)

    result = run_deploy(project_root, environment)

    assert result.returncode != 0
    state = (deploy_dir / "deployment.env").read_text(encoding="utf-8")
    assert f"APP_IMAGE={BASELINE_IMAGE}" in state
    assert CANDIDATE_IMAGE not in state


def test_rollback_swaps_current_and_previous_healthy_images(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment, deploy_dir = deploy_environment(tmp_path)
    deployed = run_deploy(project_root, environment)
    assert deployed.returncode == 0

    rolled_back = subprocess.run(
        ["sh", "scripts/deploy.sh", "rollback"],
        cwd=project_root,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )

    assert rolled_back.returncode == 0
    assert f"APP_IMAGE={BASELINE_IMAGE}" in (deploy_dir / "deployment.env").read_text()
    assert f"APP_IMAGE={CANDIDATE_IMAGE}" in (deploy_dir / "rollback.env").read_text()


def test_deploy_refuses_an_existing_operation_lock_without_removing_it(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment, deploy_dir = deploy_environment(tmp_path)
    lock = deploy_dir / ".deployment-lock"
    lock.mkdir()

    result = run_deploy(project_root, environment)

    assert result.returncode != 0
    assert lock.is_dir()
    assert "Another deployment operation is active" in result.stderr
