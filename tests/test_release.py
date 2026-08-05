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
    assert set(jobs) == {
        "quality",
        "tests",
        "coverage",
        "postgres_matrix",
        "image_candidate",
        "publish_image",
    }

    tests = jobs["tests"]
    assert tests["strategy"]["fail-fast"] is False
    assert tests["strategy"]["matrix"] == {"shard": [0, 1]}
    assert tests["env"]["PYTEST_WORKERS"] == "4"

    coverage = jobs["coverage"]
    assert coverage["needs"] == ["tests"]
    assert "needs" not in jobs["image_candidate"]
    assert jobs["image_candidate"]["if"] == "github.event_name == 'push'"
    assert jobs["image_candidate"].get("permissions", ci["permissions"]) == {"contents": "read"}
    assert set(jobs["publish_image"]["needs"]) == {
        "quality",
        "tests",
        "coverage",
        "postgres_matrix",
        "image_candidate",
    }
    assert jobs["publish_image"]["if"] == "github.event_name == 'push'"
    assert jobs["publish_image"]["permissions"] == {
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
        "packages": "write",
    }
    assert "docker/login-action@" not in str(jobs["image_candidate"])
    assert "docker push" not in str(jobs["image_candidate"])
    assert "docker/login-action@" in str(jobs["publish_image"])
    assert "docker push" in str(jobs["publish_image"])
    assert "/var/run/docker.sock" not in text
    assert "self-hosted" not in text
    assert "scripts/deploy.sh" not in text
    assert ".gitlab-ci.yml" not in text
    assert "cancel-in-progress" in ci["concurrency"]
    assert "python -m pip install" not in text
    assert text.count("astral-sh/setup-uv@") == 4
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
    candidate = ci["jobs"]["image_candidate"]
    publish = ci["jobs"]["publish_image"]
    candidate_steps = candidate["steps"]
    publish_steps = publish["steps"]
    rendered_candidate = "\n".join(str(step) for step in candidate_steps)
    rendered_publish = "\n".join(str(step) for step in publish_steps)
    candidate_step_names = [step["name"] for step in candidate_steps]
    build = next(step for step in candidate_steps if step["name"] == "Build candidate image")

    assert "docker/build-push-action@" in build["uses"]
    assert build["with"]["load"] is True
    assert build["with"]["push"] is False
    assert build["with"]["cache-from"] == "type=gha,scope=suppsystem-image"
    assert build["with"]["cache-to"] == "type=gha,mode=max,scope=suppsystem-image"
    candidate_upload = next(
        step
        for step in candidate_steps
        if "actions/upload-artifact@" in step.get("uses", "")
        and "suppsystem-image.tar" in step.get("with", {}).get("path", "")
    )
    assert candidate_step_names.index(
        "Enforce HIGH and CRITICAL vulnerability gate"
    ) < candidate_step_names.index(candidate_upload["name"])
    assert candidate_upload["with"]["compression-level"] == 0
    assert candidate_upload["with"]["if-no-files-found"] == "error"
    assert candidate_upload["with"]["retention-days"] == 1

    download = next(
        step for step in publish_steps if "actions/download-artifact@" in step.get("uses", "")
    )
    publish_step_names = [step["name"] for step in publish_steps]
    integrity = next(
        step for step in publish_steps if step["name"] == "Verify candidate integrity and revision"
    )
    login = next(step for step in publish_steps if "docker/login-action@" in step.get("uses", ""))
    image_push = next(step for step in publish_steps if step.get("id") == "publish")
    assert (
        publish_step_names.index(download["name"])
        < publish_step_names.index(integrity["name"])
        < publish_step_names.index(login["name"])
        < publish_step_names.index(image_push["name"])
    )
    assert "needs.image_candidate.outputs.image_sha256" in integrity["env"]["EXPECTED_IMAGE_SHA256"]
    assert (
        "needs.image_candidate.outputs.manifest_sha256"
        in integrity["env"]["EXPECTED_MANIFEST_SHA256"]
    )
    assert "sha256sum --check --strict candidate-checksums.txt" in integrity["run"]
    assert "org.opencontainers.image.revision" in integrity["run"]
    assert 'test "$revision" = "$GITHUB_SHA"' in integrity["run"]
    subprocess.run(["bash", "-n", "-c", integrity["run"]], check=True)

    assert download["with"]["name"] == candidate_upload["with"]["name"]
    assert "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in download["uses"]
    outputs = candidate["outputs"]
    assert set(outputs) == {"artifact_digest", "image_sha256", "manifest_sha256"}
    assert "steps.upload_candidate.outputs.artifact-digest" in outputs["artifact_digest"]
    assert "steps.candidate_checksums.outputs.image_sha256" in outputs["image_sha256"]
    assert "steps.candidate_checksums.outputs.manifest_sha256" in outputs["manifest_sha256"]
    assert "needs.image_candidate.outputs" in rendered_publish
    assert "sha256sum" in rendered_publish
    assert "sha256sum --check" in rendered_publish or "sha256sum -c" in rendered_publish
    assert "docker image load" in rendered_publish or "docker load" in rendered_publish
    assert "org.opencontainers.image.revision" in rendered_publish
    assert "GITHUB_SHA" in rendered_publish
    assert '[[ "$CANDIDATE_ARTIFACT_DIGEST" =~ ^[0-9a-f]{64}$ ]]' in rendered_publish
    assert "docker/build-push-action@" not in rendered_publish
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
    cache = next(step for step in candidate_steps if "actions/cache@" in step.get("uses", ""))
    assert cache["with"]["path"] == ".trivy-cache/db"
    assert "v0.70.0" in cache["with"]["key"]
    reports = next(
        step
        for step in candidate_steps
        if step["name"] == "Smoke-test and create security reports in parallel"
    )
    assert 'wait "$trivy_pid"' in reports["run"]
    assert 'wait "$syft_pid"' in reports["run"]
    assert "smoke_status != 0 || trivy_status != 0 || syft_status != 0" in reports["run"]
    assert reports["run"].count("--security-opt no-new-privileges") == 3
    assert reports["run"].count('--volume "$PWD:/work:ro"') == 2
    assert '--volume "$PWD/.trivy-cache:/cache"' in reports["run"]
    assert "--cache-dir /cache" in reports["run"]
    subprocess.run(["bash", "-n", "-c", reports["run"]], check=True)
    assert "suppsystem.migrations" in rendered_candidate
    license_digest = sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    assert "/app/LICENSE" in text and license_digest in text
    candidate_paths = set(candidate_upload["with"]["path"].splitlines())
    assert candidate_paths == {
        "candidate-checksums.txt",
        "sbom.cdx.json",
        "suppsystem-image.tar",
        "trivy.json",
    }
    evidence_upload = next(
        step
        for step in publish_steps
        if "actions/upload-artifact@" in step.get("uses", "")
        and step.get("with", {}).get("retention-days") == 90
    )
    artifact_paths = set(evidence_upload["with"]["path"].splitlines())
    assert {
        "image.env",
        "candidate/sbom.cdx.json",
        "candidate/trivy.json",
        "release-evidence.txt",
    } <= artifact_paths


def test_runtime_and_postgres_images_are_digest_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    compose_files = ("compose.production.postgres.yaml",)

    assert ".trivy-cache" in dockerignore
    assert all("@sha256:" in line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert "python:3.12.13-alpine3.24@sha256:" in dockerfile
    assert "python:3.12.13-slim-" not in dockerfile
    assert " AS builder" in dockerfile
    assert " AS runtime" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert "COPY src" not in runtime
    assert "pyproject.toml" not in runtime
    assert "uv.lock" not in runtime
    assert "README.md" not in runtime
    assert "uv sync" not in runtime
    assert "/usr/local/bin/uv" not in runtime
    assert "USER app" in runtime
    assert "COPY pyproject.toml uv.lock README.md LICENSE ./" in dockerfile
    assert 'org.opencontainers.image.version="3.5.0"' in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    for relative_path in compose_files:
        assert "postgres:16.14-alpine3.22@sha256:" in (ROOT / relative_path).read_text(
            encoding="utf-8"
        )


def test_public_start_script_is_transparent_and_pins_the_pulled_image() -> None:
    path = ROOT / "scripts" / "start.sh"
    text = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & 0o100
    assert "ghcr.io/dever502/suppsystem:v3.5.0" in text
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
    assert "> Версия: `v3.5.0`." in readme
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
    ci, _ = _ci()
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
    assert "PYTEST_SHARD_COUNT" in unit_tests
    assert "PYTEST_SHARD_INDEX" in unit_tests

    test_steps = ci["jobs"]["tests"]["steps"]
    coverage_steps = ci["jobs"]["coverage"]["steps"]
    test_rendered = "\n".join(str(step) for step in test_steps)
    coverage_rendered = "\n".join(str(step) for step in coverage_steps)
    assert "matrix.shard" in test_rendered
    expected_file = "coverage-shard-" + "$" + "{{ matrix.shard }}"
    assert ci["jobs"]["tests"]["env"]["COVERAGE_FILE"] == expected_file
    coverage_downloads = [
        step for step in coverage_steps if "actions/download-artifact@" in step.get("uses", "")
    ]
    assert len(coverage_downloads) == 1
    expected_pattern = "unit-coverage-" + "$" + "{{ github.sha }}-*"
    assert coverage_downloads[0]["with"]["pattern"] == expected_pattern
    assert coverage_downloads[0]["with"]["merge-multiple"] is True
    assert "coverage-data/coverage-shard-0" in coverage_rendered
    assert "coverage-data/coverage-shard-1" in coverage_rendered
    assert "find coverage-data -mindepth 1 -maxdepth 1 -type f" in coverage_rendered
    assert "coverage combine" in coverage_rendered
    assert "coverage report --show-missing --fail-under=75" in coverage_rendered
    assert coverage_rendered.count("--fail-under=75") == 1


def test_ci_postgres_parallelism_keeps_role_provisioning_serial() -> None:
    ci, _ = _ci()
    options = ci["jobs"]["postgres_matrix"]["services"]["postgres"]["options"]
    postgres_tests = (ROOT / "scripts" / "test_postgres.sh").read_text(encoding="utf-8")
    parallel, serial = postgres_tests.split(
        "# Role provisioning changes cluster-wide principals and must remain serial.", maxsplit=1
    )

    assert "--health-interval 1s" in options
    assert "--health-retries 60" in options
    assert ci["jobs"]["postgres_matrix"]["env"]["POSTGRES_PYTEST_WORKERS"] == "4"
    assert '-n "$workers" --dist load -m postgres' in parallel
    assert "tests/test_postgres_migrations.py" in parallel
    assert "tests/test_postgres_contracts.py" in parallel
    assert "tests/test_retention.py" in parallel
    assert "tests/test_postgres_roles.py" not in parallel
    assert "pytest -m postgres tests/test_postgres_roles.py" in serial
    assert '-n "$workers"' not in serial
