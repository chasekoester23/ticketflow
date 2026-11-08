"""Turn a pytest JUnit report into GitHub annotations, one per kind of failure.

Annotations can be read without admin access to the run's logs, so anyone can see
what failed. GitHub keeps ten of each level and about 4 KB of each message, so
failures are grouped by cause, biggest first.

Usage: python scripts/ci_report_failures.py results.xml
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import defaultdict

MESSAGE_LIMIT = 3500


def failures(path: str) -> tuple[int, dict[str, list[str]]]:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    groups: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        problem = case.find("failure") if case.find("failure") is not None else case.find("error")
        if problem is None:
            continue
        lines = (problem.get("message") or "").strip().splitlines()
        cause = re.sub(r"\d+", "N", lines[0] if lines else "no message")[:170]
        groups[cause].append(f"{case.get('classname', '').replace('tests.', '')}::{case.get('name', '')}")
    return len(cases), groups


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    total, groups = failures(sys.argv[1])
    failed = sum(len(names) for names in groups.values())
    print(f"{total} tests, {failed} failed, {len(groups)} different causes")
    ranked = sorted(groups.items(), key=lambda item: -len(item[1]))
    for index, (cause, names) in enumerate(ranked[:30]):
        level = "error" if index < 10 else "warning" if index < 20 else "notice"
        body = "\n".join(names)[:MESSAGE_LIMIT - len(cause)]
        print(f"::{level} title={len(names)} of {failed} failures::{_escape(cause + chr(10) + body)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
