from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

TRIVY_GATE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "check_trivy_report.py")
)
blocking_vulnerabilities = TRIVY_GATE["blocking_vulnerabilities"]
main = TRIVY_GATE["main"]


def _report(vulnerabilities: list[dict[str, str]] | None) -> dict[str, Any]:
    return {
        "Results": [
            {
                "Target": "resolvate",
                "Vulnerabilities": vulnerabilities,
            }
        ]
    }


def test_trivy_gate_accepts_report_without_high_or_critical_findings() -> None:
    report = _report(
        [
            {
                "VulnerabilityID": "CVE-LOW",
                "Severity": "LOW",
                "PkgName": "example",
                "InstalledVersion": "1.0",
            },
            {
                "VulnerabilityID": "CVE-MEDIUM",
                "Severity": "MEDIUM",
                "PkgName": "example",
                "InstalledVersion": "1.0",
            },
        ]
    )

    assert blocking_vulnerabilities(report) == []
    assert blocking_vulnerabilities(_report(None)) == []


def test_trivy_gate_blocks_high_and_critical_findings() -> None:
    report = _report(
        [
            {
                "VulnerabilityID": "CVE-HIGH",
                "Severity": "HIGH",
                "PkgName": "high-package",
                "InstalledVersion": "1.0",
            },
            {
                "VulnerabilityID": "CVE-CRITICAL",
                "Severity": "CRITICAL",
                "PkgName": "critical-package",
                "InstalledVersion": "2.0",
            },
        ]
    )

    assert blocking_vulnerabilities(report) == [
        ("resolvate", "CVE-HIGH", "HIGH", "high-package", "1.0"),
        ("resolvate", "CVE-CRITICAL", "CRITICAL", "critical-package", "2.0"),
    ]


def test_trivy_gate_cli_returns_security_specific_exit_codes(
    tmp_path: Path,
) -> None:
    clean_report = tmp_path / "clean.json"
    clean_report.write_text(json.dumps(_report(None)), encoding="utf-8")
    assert main(["check_trivy_report.py", str(clean_report)]) == 0

    blocked_report = tmp_path / "blocked.json"
    blocked_report.write_text(
        json.dumps(
            _report(
                [
                    {
                        "VulnerabilityID": "CVE-HIGH",
                        "Severity": "HIGH",
                        "PkgName": "example",
                        "InstalledVersion": "1.0",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    assert main(["check_trivy_report.py", str(blocked_report)]) == 1

    invalid_report = tmp_path / "invalid.json"
    invalid_report.write_text("{}", encoding="utf-8")
    assert main(["check_trivy_report.py", str(invalid_report)]) == 2
