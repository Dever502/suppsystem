from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BLOCKING_SEVERITIES = frozenset({"HIGH", "CRITICAL"})


def blocking_vulnerabilities(report: Any) -> list[tuple[str, str, str, str, str]]:
    if not isinstance(report, dict) or not isinstance(report.get("Results"), list):
        raise ValueError("Trivy report must contain a Results list")

    blocked: list[tuple[str, str, str, str, str]] = []
    for result in report["Results"]:
        if not isinstance(result, dict):
            raise ValueError("Each Trivy result must be an object")
        target = str(result.get("Target", "unknown target"))
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            continue
        if not isinstance(vulnerabilities, list):
            raise ValueError("Trivy Vulnerabilities must be a list or null")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ValueError("Each Trivy vulnerability must be an object")
            severity = vulnerability.get("Severity")
            if not isinstance(severity, str):
                raise ValueError("Each Trivy vulnerability must have a severity")
            severity = severity.upper()
            if severity in BLOCKING_SEVERITIES:
                blocked.append(
                    (
                        target,
                        str(vulnerability.get("VulnerabilityID", "unknown vulnerability")),
                        severity,
                        str(vulnerability.get("PkgName", "unknown package")),
                        str(vulnerability.get("InstalledVersion", "unknown version")),
                    )
                )
    return blocked


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} TRIVY_REPORT", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        blocked = blocking_vulnerabilities(report)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid Trivy report {path}: {error}", file=sys.stderr)
        return 2

    if blocked:
        for target, vulnerability_id, severity, package, version in blocked:
            print(
                f"{target}: {vulnerability_id} [{severity}] {package} {version}",
                file=sys.stderr,
            )
        print(
            f"Blocked {len(blocked)} HIGH/CRITICAL vulnerabilities",
            file=sys.stderr,
        )
        return 1

    print("No HIGH/CRITICAL vulnerabilities found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
