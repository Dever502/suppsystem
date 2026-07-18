from __future__ import annotations

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
    assert {"verify", "postgres_matrix", "build_image"} <= jobs.keys()
    assert jobs["build_image"]["needs"] == ["verify", "postgres_matrix"]
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
    steps = ci["jobs"]["build_image"]["steps"]
    rendered_steps = "\n".join(str(step) for step in steps)

    assert any("docker/build-push-action@" in step.get("uses", "") for step in steps)
    assert "ghcr.io/dever502/suppsystem" in text
    assert "IMAGE_REFERENCE=%s" in text
    assert "HIGH,CRITICAL" in rendered_steps
    assert "--ignore-unfixed" not in text
    assert "aquasec/trivy:0.70.0@sha256:" in text
    assert "anchore/syft:v1.44.0-debug@sha256:" in text
    assert "SYFT_REGISTRY_AUTH_AUTHORITY: ghcr.io" in text
    assert "SYFT_REGISTRY_AUTH_PASSWORD: ${{ secrets.GITHUB_TOKEN }}" in text
    assert "--env SYFT_REGISTRY_AUTH_PASSWORD" in text
    assert '"$IMAGE_REFERENCE" --from registry -o cyclonedx-json > sbom.cdx.json' in text
    assert "supportbot.migrations" in rendered_steps
    license_digest = sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    assert "/app/LICENSE" in text and license_digest in text
    upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    artifact_paths = set(upload["with"]["path"].splitlines())
    assert {"image.env", "sbom.cdx.json", "trivy.json", "release-evidence.txt"} <= artifact_paths


def test_runtime_and_postgres_images_are_digest_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose_files = ("compose.production.postgres.yaml",)

    assert all("@sha256:" in line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert "python:3.12.13-alpine3.24@sha256:" in dockerfile
    assert "python:3.12.13-slim-" not in dockerfile
    assert "COPY pyproject.toml uv.lock README.md LICENSE ./" in dockerfile
    assert 'org.opencontainers.image.version="1.0.0"' in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    for relative_path in compose_files:
        assert "postgres:16.14-alpine3.22@sha256:" in (ROOT / relative_path).read_text(
            encoding="utf-8"
        )


def test_public_start_script_is_transparent_and_pins_the_pulled_image() -> None:
    path = ROOT / "scripts" / "start.sh"
    text = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & 0o100
    assert "ghcr.io/dever502/suppsystem:v1.0.0" in text
    assert "docker pull" in text
    assert "RepoDigests" in text
    assert "config --format json" in text
    assert "python -m supportbot.production" in text
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
    assert "> Версия: `v1.0.0`." in readme
    assert "./scripts/start.sh sqlite" in readme
    assert "## Ограничения" not in readme
    assert "## Разработка" not in readme
    assert "Официальный способ поставки — container image" in readme
    assert "полноценная интеграция с Remnawave 2.8.0" in readme
    assert "notification webhook с durable at-least-once доставкой" in readme
    assert "Remnawave 2.8.0" in readme
    assert "@sha256:<digest>" in readme

    public_markdown = "\n".join(
        (ROOT / relative_path).read_text(encoding="utf-8") for relative_path in required
    )
    for private_phrase in ("docs/RELEASE.md", "GitLab", "code freeze", "release gate", "15 июля"):
        assert private_phrase not in public_markdown
