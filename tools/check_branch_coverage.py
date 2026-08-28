"""Fail CI unless every named module meets a pure branch-coverage threshold."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("modules", nargs="+")
    parser.add_argument("--threshold", type=float, default=90.0)
    args = parser.parse_args()
    report = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    failed = False
    for module in args.modules:
        suffix = f"/{module}.py"
        matches = [
            details
            for filename, details in report["files"].items()
            if filename.replace("\\", "/").endswith(suffix)
        ]
        if len(matches) != 1:
            raise SystemExit(f"expected exactly one coverage entry for {module}.py")
        summary = matches[0]["summary"]
        total = summary["num_branches"]
        covered = summary["covered_branches"]
        if total <= 0:
            raise SystemExit(f"{module}.py has no measurable branches")
        percent = covered * 100 / total
        print(f"{module}.py: {covered}/{total} pure branches = {percent:.2f}%")
        failed = failed or percent < args.threshold
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
