#!/usr/bin/env python3
"""Record a finished layered-review round so the next run on the same PR can build on it.

Writes round-<N>.json with the round's findings and round-<N>.diff with the diff it reviewed into
the PR's state directory. Rerunning a round at the same head commit overwrites it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

FINDING_TAGS = ("important", "security", "nit", "suggestion", "pre-existing")
REQUIRED_FINDING_FIELDS = ("id", "tag", "file", "title")


class RoundRecordError(RuntimeError):
    pass


def validate_findings(findings: dict) -> None:
    for list_name in ("open", "fixed"):
        if not isinstance(findings.get(list_name), list):
            raise RoundRecordError(f"The findings file needs an {list_name!r} list.")
        for finding in findings[list_name]:
            missing_fields = [field for field in REQUIRED_FINDING_FIELDS if not finding.get(field)]
            if missing_fields:
                raise RoundRecordError(
                    f"A finding in {list_name!r} is missing {', '.join(missing_fields)}: {finding}"
                )
            if finding["tag"] not in FINDING_TAGS:
                raise RoundRecordError(
                    f"Finding {finding['id']} has tag {finding['tag']!r}; "
                    f"expected one of {', '.join(FINDING_TAGS)}."
                )


def parse_arguments() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("round_context_path", help="round_context_path from prepare.py.")
    argument_parser.add_argument(
        "findings_path", help='JSON with "depth", "junior", "open" and "fixed" findings.'
    )
    return argument_parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    round_context = json.loads(Path(arguments.round_context_path).read_text())
    findings = json.loads(Path(arguments.findings_path).read_text())
    validate_findings(findings)

    state_directory = Path(round_context["state_dir"])
    state_directory.mkdir(parents=True, exist_ok=True)
    round_number = round_context["round"]
    (state_directory / f"round-{round_number}.json").write_text(
        json.dumps(
            {
                "round": round_number,
                "head_commit": round_context["head_commit"],
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "depth": findings.get("depth"),
                "junior": findings.get("junior", False),
                "review_scope": round_context["review_scope"],
                "open": findings["open"],
                "fixed": findings["fixed"],
            },
            indent=2,
        )
    )
    shutil.copyfile(round_context["review_diff_path"], state_directory / f"round-{round_number}.diff")
    print(
        f"Recorded round {round_number}: {len(findings['open'])} open, "
        f"{len(findings['fixed'])} fixed, in {state_directory}"
    )


if __name__ == "__main__":
    try:
        main()
    except RoundRecordError as record_error:
        sys.exit(f"ERROR: {record_error}")
