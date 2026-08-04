from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _ci() -> tuple[dict[str, Any], str]:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def test_ci_keeps_verification_socketless_and_excludes_private_cd() -> None:
    ci, text = _ci()

    jobs = ci["jobs"]
    assert {"quality", "tests", "postgres_matrix", "build_image"} <= jobs.keys()
    assert jobs["build_image"]["needs"] == ["quality", "tests", "postgres_matrix"]
    assert jobs["build_image"]["if"] == "github.event_name == 'push'"
    assert jobs["build_image"]["permissions"] == {
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
        "packages": "write",
    }
    assert "/var/run/docker.sock" not in text
    assert "self-hosted" not in text
    assert "scripts/deploy.sh" not in text
    assert ".gitlab-ci.yml" not in text
    assert "cancel-in-progress" in ci["concurrency"]
    assert "python -m pip install" not in text
    assert text.count("astral-sh/setup-uv@") == 3
    assert ci["jobs"]["tests"]["env"]["PYTEST_WORKERS"] == "4"
    for job_name in ("quality", "tests", "postgres_matrix"):
        command_step = next(
            step
            for step in ci["jobs"][job_name]["steps"]
            if step.get("run", "").startswith("sh scripts/")
        )
        assert command_step["env"]["UV_NO_SYNC"] == "1"
    action_references = [
        line.split("@", 1)[1].split()[0]
        for line in text.splitlines()
        if "uses:" in line and "@" in line
    ]
    assert action_references
    assert all(len(reference) == 40 for reference in action_references)
    assert all(set(reference) <= set("0123456789abcdef") for reference in action_references)


def test_ci_build_scan_sbom_and_evidence_share_one_immutable_image() -> None:
    ci, text = _ci()
    trivy_gate = (ROOT / "scripts" / "check_trivy_report.py").read_text(encoding="utf-8")
    steps = ci["jobs"]["build_image"]["steps"]
    rendered_steps = "\n".join(str(step) for step in steps)
    step_names = [step["name"] for step in steps]
    build = next(step for step in steps if step["name"] == "Build candidate image")

    assert "docker/build-push-action@" in build["uses"]
    assert build["with"]["load"] is True
    assert build["with"]["push"] is False
    assert build["with"]["cache-from"] == "type=gha,scope=suppsystem-image"
    assert build["with"]["cache-to"] == "type=gha,mode=max,scope=suppsystem-image"
    assert step_names.index("Enforce HIGH and CRITICAL vulnerability gate") < step_names.index(
        "Publish verified image"
    )
    assert "ghcr.io/dever502/suppsystem" in text
    assert "IMAGE_REFERENCE=%s" in text
    assert "suppsystem-image.tar" in text
    assert "chmod 0644 suppsystem-image.tar" in text
    assert "--ignore-unfixed" not in text
    assert "aquasec/trivy:0.70.0@sha256:" in text
    assert text.count("aquasec/trivy:0.70.0@sha256:") == 1
    assert "scripts/check_trivy_report.py trivy.json" in text
    assert 'frozenset({"HIGH", "CRITICAL"})' in trivy_gate
    assert "anchore/syft:v1.44.0-debug@sha256:" in text
    assert "docker-archive:/work/suppsystem-image.tar" in text
    assert "SYFT_REGISTRY_AUTH_PASSWORD" not in text
    cache = next(step for step in steps if "actions/cache@" in step.get("uses", ""))
    assert cache["with"]["path"] == ".trivy-cache/db"
    assert "v0.70.0" in cache["with"]["key"]
    reports = next(
        step
        for step in steps
        if step["name"] == "Create Trivy report and CycloneDX SBOM in parallel"
    )
    assert 'wait "$trivy_pid"' in reports["run"]
    assert 'wait "$syft_pid"' in reports["run"]
    assert "trivy_status != 0 || syft_status != 0" in reports["run"]
    assert reports["run"].count("--security-opt no-new-privileges") == 2
    assert reports["run"].count('--volume "$PWD:/work:ro"') == 2
    assert '--volume "$PWD/.trivy-cache:/cache"' in reports["run"]
    assert "--cache-dir /cache" in reports["run"]
    subprocess.run(["bash", "-n", "-c", reports["run"]], check=True)
    assert "suppsystem.migrations" in rendered_steps
    license_digest = sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    assert "/app/LICENSE" in text and license_digest in text
    upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    artifact_paths = set(upload["with"]["path"].splitlines())
    assert {"image.env", "sbom.cdx.json", "trivy.json", "release-evidence.txt"} <= artifact_paths


def test_runtime_and_postgres_images_are_digest_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    compose_files = ("compose.production.postgres.yaml",)

    assert ".trivy-cache" in dockerignore
    assert all("@sha256:" in line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert "python:3.12.13-alpine3.24@sha256:" in dockerfile
    assert "python:3.12.13-slim-" not in dockerfile
    assert "COPY pyproject.toml uv.lock README.md LICENSE ./" in dockerfile
    assert 'org.opencontainers.image.version="3.0.0"' in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    for relative_path in compose_files:
        assert "postgres:16.14-alpine3.22@sha256:" in (ROOT / relative_path).read_text(
            encoding="utf-8"
        )


def test_public_start_script_is_transparent_and_pins_the_pulled_image() -> None:
    path = ROOT / "scripts" / "start.sh"
    text = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & 0o100
    assert "ghcr.io/dever502/suppsystem:v3.0.0" in text
    assert "docker pull" in text
    assert "RepoDigests" in text
    assert "config --format json" in text
    assert "python -m suppsystem.production" in text
    assert "up --detach --wait" in text
    for unsafe in ("sudo ", "curl ", "source ", "eval "):
        assert unsafe not in text


def test_public_documentation_is_curated() -> None:
    required = (
        "README.md",
        "docs/OBSERVABILITY.md",
        "docs/OPERATIONS.md",
        "docs/TECHNICAL.md",
    )
    for relative_path in required:
        assert (ROOT / relative_path).is_file()

    private_only = ("SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "docs/RELEASE.md")
    for relative_path in private_only:
        assert not (ROOT / relative_path).exists()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "> Версия: `v3.0.0`." in readme
    assert "./scripts/start.sh sqlite" in readme
    assert "## Ограничения" not in readme
    assert "## Разработка" not in readme
    assert "Официальный способ поставки — container image" in readme
    assert "полноценная интеграция с Remnawave 2.8.x" in readme
    assert "notification webhook с durable at-least-once доставкой" in readme
    assert "Remnawave 2.8.x" in readme
    assert "@sha256:<digest>" in readme

    public_markdown = "\n".join(
        (ROOT / relative_path).read_text(encoding="utf-8") for relative_path in required
    )
    for private_phrase in ("docs/RELEASE.md", "GitLab", "code freeze", "release gate", "15 июля"):
        assert private_phrase not in public_markdown


def test_alerts_cover_recorded_remnawave_failure_outcomes() -> None:
    alerts = yaml.safe_load(
        (ROOT / "deploy/prometheus/suppsystem-alerts.yml").read_text(encoding="utf-8")
    )
    rules = alerts["groups"][0]["rules"]
    external_failures = next(
        rule for rule in rules if rule["alert"] == "suppsystemExternalRequestFailures"
    )

    assert "http_5xx" in external_failures["expr"]
    assert "request_error" in external_failures["expr"]


def test_verification_enforces_coverage_threshold() -> None:
    verify = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
    unit_tests = (ROOT / "scripts/test_unit.sh").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "pytest-cov" in pyproject
    assert "pytest-xdist" in pyproject
    assert "scripts/check_quality.sh" in verify
    assert "scripts/test_unit.sh" in verify
    assert '-n "$workers"' in unit_tests
    assert "--dist load" in unit_tests
    assert '-m "not postgres"' in unit_tests
    assert "--cov=suppsystem" in unit_tests
    assert "--cov-fail-under=" in unit_tests
