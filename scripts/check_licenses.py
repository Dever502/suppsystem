from __future__ import annotations

import re
from importlib import metadata

FORBIDDEN_LICENSE = re.compile(r"(?<!L)(?:AGPL|GPL|SSPL)(?:[- v]?[123][^A-Za-z]*)?", re.IGNORECASE)


def distribution_license(distribution: metadata.Distribution) -> str:
    expression = distribution.metadata.get("License-Expression")
    if expression:
        return expression
    license_value = distribution.metadata.get("License")
    if license_value and license_value.casefold() != "unknown":
        return license_value
    classifiers = distribution.metadata.get_all("Classifier", [])
    licenses = [
        item.removeprefix("License :: ") for item in classifiers if item.startswith("License :: ")
    ]
    return "; ".join(licenses) if licenses else "UNKNOWN"


def main() -> None:
    violations: list[str] = []
    unknown: list[str] = []
    for distribution in sorted(
        metadata.distributions(), key=lambda item: (item.metadata.get("Name") or "").casefold()
    ):
        name = distribution.metadata.get("Name") or "unnamed-distribution"
        license_value = distribution_license(distribution)
        if license_value == "UNKNOWN":
            unknown.append(name)
        elif FORBIDDEN_LICENSE.search(license_value):
            violations.append(f"{name}: {license_value}")
    if unknown:
        print("Packages without machine-readable license metadata: " + ", ".join(unknown))
    if violations:
        raise SystemExit("Forbidden dependency licenses:\n" + "\n".join(violations))
    print("Dependency license policy passed")


if __name__ == "__main__":
    main()
