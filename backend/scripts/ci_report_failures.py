"""Turn a pytest JUnit report into one GitHub annotation listing every failure.

Annotations can be read without admin access to the run's logs, so anyone can see
what failed. Usage: python scripts/ci_report_failures.py results.xml
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ElementTree


def failures(path: str) -> tuple[int, list[str]]:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    found = []
    for case in cases:
        problem = case.find("failure") if case.find("failure") is not None else case.find("error")
        if problem is not None:
            message = (problem.get("message") or "").strip().splitlines()
            found.append(f"{case.get('classname', '')}::{case.get('name', '')} -- {message[0][:220] if message else ''}")
    return len(cases), found


def main() -> int:
    total, found = failures(sys.argv[1])
    print(f"{total} tests, {len(found)} failed")
    if found:
        body = "\n".join(found)[:60000].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title={len(found)} of {total} tests failed::{body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
