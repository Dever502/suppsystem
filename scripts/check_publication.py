from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    kind: str


CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("Telegram bot token", re.compile(r"(?<![\w-])\d{6,12}:[A-Za-z0-9_-]{35}(?![\w-])")),
    (
        "GitHub access token",
        re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[\w-]{20,})\b"),
    ),
    ("GitLab access token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack access token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)

PRIVATE_INFRASTRUCTURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private GitLab hostname", re.compile(r"https?://gitlab\.(?!com(?:[/:]|$))[^\s/]+")),
    (
        "private SSH Git remote",
        re.compile(r"ssh://" r"git@(?!(?:github\.com|gitlab\.com)[:/])[^\s]+"),
    ),
    ("developer home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/")),
    (
        "legacy deployment path",
        re.compile(r"/opt/" r"suppsystemtest(?:/|\b)"),
    ),
)

EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}))(?![\w.-])")
PUBLIC_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
ALLOW_MARKER_PREFIX = "publication-check: allow-"


def scan_text(
    text: str,
    *,
    source: str,
    include_private_infrastructure: bool = True,
    include_email_pii: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    patterns = CONTENT_PATTERNS + (
        PRIVATE_INFRASTRUCTURE_PATTERNS if include_private_infrastructure else ()
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER_PREFIX in line:
            continue
        for kind, pattern in patterns:
            if pattern.search(line):
                findings.append(Finding(source, line_number, kind))
        if include_email_pii:
            for match in EMAIL_PATTERN.finditer(line):
                domain = match.group(2).casefold()
                is_placeholder = (
                    domain in PUBLIC_EMAIL_DOMAINS
                    or domain.endswith(tuple(f".{item}" for item in PUBLIC_EMAIL_DOMAINS))
                    or domain.endswith((".example", ".invalid"))
                )
                if not is_placeholder and not domain.endswith(".users.noreply.github.com"):
                    findings.append(Finding(source, line_number, "personal email / PII"))
    return findings


def repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / raw_path.decode() for raw_path in result.stdout.split(b"\0") if raw_path]


def scan_snapshot(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in repository_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(content, source=str(path.relative_to(root))))

    legacy_ci_path = root / ".gitlab-ci.yml"
    if legacy_ci_path.exists():
        findings.append(Finding(".gitlab-ci.yml", 1, "legacy GitLab CI in public snapshot"))

    workflow_root = root / ".github" / "workflows"
    workflow_paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    if not workflow_paths:
        findings.append(Finding(".github/workflows", 1, "missing publishable GitHub workflow"))

    forbidden_workflow_fragments = {
        "/var/run/docker.sock": "Docker socket in publishable CI",
        "scripts/deploy.sh": "production deploy action in publishable CI",
        "ENABLE_PRODUCTION_DEPLOY": "production deploy switch in publishable CI",
        "DEPLOY_DIR=": "production deployment path in publishable CI",
        "runs-on: self-hosted": "self-hosted runner in publishable CI",
        "pull_request_target": "privileged pull-request trigger in publishable CI",
        "permissions: write-all": "unbounded GitHub workflow permissions",
    }
    action_reference = re.compile(r"\buses:\s*([^\s@]+)@([^\s#]+)")
    for workflow_path in workflow_paths:
        workflow_text = workflow_path.read_text(encoding="utf-8")
        source = str(workflow_path.relative_to(root))
        for line_number, line in enumerate(workflow_text.splitlines(), start=1):
            for fragment, kind in forbidden_workflow_fragments.items():
                if fragment in line:
                    findings.append(Finding(source, line_number, kind))
            match = action_reference.search(line)
            if match is not None and not re.fullmatch(r"[0-9a-f]{40}", match.group(2)):
                findings.append(Finding(source, line_number, "GitHub Action is not SHA-pinned"))
    return findings


def scan_history(root: Path) -> list[Finding]:
    result = subprocess.run(
        ["git", "log", "--all", "--format=fuller", "-p", "--no-ext-diff"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    first_by_kind: dict[str, Finding] = {}
    for finding in scan_text(result.stdout, source="git-history"):
        first_by_kind.setdefault(finding.kind, finding)
    return list(first_by_kind.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Check files prepared for public publication")
    parser.add_argument("--history", action="store_true", help="also inspect all current Git refs")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    findings = scan_snapshot(args.root)
    if args.history:
        findings.extend(scan_history(args.root))
    if findings:
        rendered = "\n".join(
            f"{finding.source}:{finding.line}: {finding.kind}" for finding in findings
        )
        raise SystemExit(f"Publication check failed:\n{rendered}")
    scope = "snapshot and history" if args.history else "snapshot"
    print(f"Publication {scope} check passed")


if __name__ == "__main__":
    main()
