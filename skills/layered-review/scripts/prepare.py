#!/usr/bin/env python3
"""Prepare a layered review of a GitHub pull request.

Runs review-pr's fetch_pr.py for the verified diff and the PR discussion, then adds what the
layered review needs: review guidance from the base branch, the author's association with the
repository, CI checks, the head commit in a local clone, and the state of earlier rounds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

FETCH_PR_SCRIPT = Path(__file__).resolve().parents[2] / "review-pr" / "scripts" / "fetch_pr.py"
sys.path.insert(0, str(FETCH_PR_SCRIPT.parent))
import fetch_pr  # noqa: E402

# From this round on, review only the files whose changes moved since the previous round.
DIFF_ONLY_FROM_ROUND = 3

GUIDANCE_FILE_KINDS = {"CLAUDE.md": "claude_md", "REVIEW.md": "review_md"}
# GitHub uses the first of these that exists.
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
PULL_REQUEST_TEMPLATE_LOCATIONS = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
)

GITHUB_REMOTE_URL_PATTERN = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repository>[^/]+?)(?:\.git)?/?$", re.IGNORECASE
)
ROUND_RECORD_NAME_PATTERN = re.compile(r"^round-(\d+)\.json$")

CHECK_BUCKETS = ("pass", "fail", "pending", "skipping", "cancel")


def run_fetch_pr(arguments: argparse.Namespace) -> dict:
    fetch_arguments = [arguments.pull_request_url]
    for glob in arguments.exclude:
        fetch_arguments += ["--exclude", glob]
    if arguments.keep_editor_serialized:
        fetch_arguments.append("--keep-editor-serialized")
    completed_process = subprocess.run(
        [sys.executable, str(FETCH_PR_SCRIPT), *fetch_arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_process.returncode != 0:
        sys.stderr.write(completed_process.stderr)
        sys.exit(completed_process.returncode)
    return json.loads(completed_process.stdout)


def join_repository_path(directory: str, file_name: str) -> str:
    return f"{directory}/{file_name}" if directory else file_name


def ancestor_directories(path: str) -> list[str]:
    directory_parts = path.split("/")[:-1]
    return ["/".join(directory_parts[:depth]) for depth in range(len(directory_parts) + 1)]


def guidance_candidate_kinds(changed_paths: list[str]) -> dict[str, str]:
    candidate_kinds = {".claude/CLAUDE.md": "claude_md"}
    for changed_path in changed_paths:
        for directory in ancestor_directories(changed_path):
            for file_name, kind in GUIDANCE_FILE_KINDS.items():
                candidate_kinds[join_repository_path(directory, file_name)] = kind
    candidate_kinds.update({path: "codeowners" for path in CODEOWNERS_LOCATIONS})
    candidate_kinds.update({path: "pull_request_template" for path in PULL_REQUEST_TEMPLATE_LOCATIONS})
    return candidate_kinds


def fetch_file_at_commit(location: dict[str, str], path: str, commit: str) -> str | None:
    try:
        return fetch_pr.run_gh_with_stored_login(
            [
                "api",
                "repos/{owner}/{repository}/contents/".format(**location)
                + f"{quote(path)}?ref={commit}",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
        )
    except fetch_pr.PullRequestFetchError as fetch_error:
        if "HTTP 404" in str(fetch_error):
            return None
        raise


def fetch_existing_files(
    location: dict[str, str], commit: str, candidate_paths: list[str]
) -> tuple[dict[str, str], str]:
    """Contents of the candidate paths that exist at the commit, and how they were found."""
    tree = json.loads(
        fetch_pr.run_gh_with_stored_login(
            ["api", "repos/{owner}/{repository}/git/trees/".format(**location) + f"{commit}?recursive=1"]
        )
    )
    if tree["truncated"]:
        # The tree is too big to list, so ask for each candidate instead.
        fetched_files = {
            path: fetch_file_at_commit(location, path, commit) for path in candidate_paths
        }
        return {path: text for path, text in fetched_files.items() if text is not None}, "probed"
    blob_paths = {entry["path"] for entry in tree["tree"] if entry["type"] == "blob"}
    return {
        path: fetch_file_at_commit(location, path, commit)
        for path in candidate_paths
        if path in blob_paths
    }, "tree"


def fetch_guidance(
    location: dict[str, str], base_commit: str, changed_paths: list[str], output_directory: Path
) -> tuple[list[dict], str]:
    candidate_kinds = guidance_candidate_kinds(changed_paths)
    existing_files, lookup = fetch_existing_files(location, base_commit, list(candidate_kinds))
    first_of_kind_only = {"codeowners", "pull_request_template"}
    guidance_files = []
    for path, text in existing_files.items():
        kind = candidate_kinds[path]
        if kind in first_of_kind_only and any(file["kind"] == kind for file in guidance_files):
            continue
        local_path = output_directory / "guidance" / path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(text)
        guidance_files.append({"path": path, "kind": kind, "local_path": str(local_path)})
    return guidance_files, lookup


def is_guidance_path(path: str) -> bool:
    file_name = path.rsplit("/", 1)[-1]
    return (
        file_name in GUIDANCE_FILE_KINDS
        or path in CODEOWNERS_LOCATIONS
        or path in PULL_REQUEST_TEMPLATE_LOCATIONS
    )


def summarise_ci_checks(pull_request_url: str) -> dict:
    completed_process = subprocess.run(
        ["gh", "pr", "checks", pull_request_url, "--json", "name,bucket"],
        env={name: value for name, value in os.environ.items() if name != "GH_TOKEN"},
        capture_output=True,
        text=True,
        check=False,
    )
    # gh exits non-zero when a check failed or is pending, but still prints the JSON.
    if completed_process.stdout.strip():
        checks = json.loads(completed_process.stdout)
    elif "no checks reported" in completed_process.stderr:
        checks = []
    else:
        return {"error": completed_process.stderr.strip()}
    counts = dict.fromkeys(CHECK_BUCKETS, 0)
    for check in checks:
        counts[check["bucket"]] = counts.get(check["bucket"], 0) + 1
    return {
        "counts": counts,
        "not_passing": [check["name"] for check in checks if check["bucket"] in ("fail", "cancel")],
        "pending": [check["name"] for check in checks if check["bucket"] == "pending"],
    }


def run_git(git_arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *git_arguments],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )


def find_local_clone_remote(location: dict[str, str]) -> str | None:
    """The remote of the current directory's clone that points at the PR's repository."""
    remotes = run_git(["remote", "-v"])
    if remotes.returncode != 0:
        return None
    for remote_line in remotes.stdout.splitlines():
        remote_name, remote_url = remote_line.split()[:2]
        url_match = GITHUB_REMOTE_URL_PATTERN.search(remote_url)
        if url_match and (
            url_match["owner"].lower(),
            url_match["repository"].lower(),
        ) == (location["owner"].lower(), location["repository"].lower()):
            return remote_name
    return None


def make_head_commit_available(remote_name: str, number: str, head_commit: str) -> bool:
    def head_commit_exists() -> bool:
        return run_git(["cat-file", "-e", f"{head_commit}^{{commit}}"]).returncode == 0

    if head_commit_exists():
        return True
    # Fetching the pull ref creates no branch and leaves the checkout alone. It also covers fork PRs.
    run_git(["fetch", "--quiet", "--no-tags", remote_name, f"refs/pull/{number}/head"])
    return head_commit_exists()


def state_directory_for(location: dict[str, str]) -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (
        state_home
        / "tomqdev-layered-review"
        / location["owner"].lower()
        / location["repository"].lower()
        / location["number"]
    )


def load_latest_round(state_directory: Path) -> dict | None:
    if not state_directory.is_dir():
        return None
    round_numbers = [
        int(name_match.group(1))
        for name_match in (ROUND_RECORD_NAME_PATTERN.match(path.name) for path in state_directory.iterdir())
        if name_match
    ]
    if not round_numbers:
        return None
    return json.loads((state_directory / f"round-{max(round_numbers)}.json").read_text())


def file_sections_by_path(diff_text: str) -> dict[str, str]:
    if not diff_text.strip():
        return {}
    file_sections = fetch_pr.split_diff_into_file_sections(diff_text)
    per_file_stats = fetch_pr.git_numstat_per_file_section(diff_text)
    return {path: section for section, (_, _, path) in zip(file_sections, per_file_stats)}


def parse_arguments() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("pull_request_url")
    argument_parser.add_argument(
        "--depth",
        choices=("quick", "standard", "deep"),
        help="Review depth. Reported back as requested_depth; the skill decides what it means.",
    )
    argument_parser.add_argument(
        "--full",
        action="store_true",
        help="Review the whole diff even in rounds that would review only what changed.",
    )
    argument_parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="Passed to fetch_pr.py."
    )
    argument_parser.add_argument(
        "--keep-editor-serialized", action="store_true", help="Passed to fetch_pr.py."
    )
    return argument_parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    summary = run_fetch_pr(arguments)
    if summary["reviewed_file_count"] == 0:
        json.dump(summary, sys.stdout, indent=2)
        print()
        return

    location = fetch_pr.PULL_REQUEST_URL_PATTERN.match(summary["pull_request_url"]).groupdict()
    output_directory = Path(summary["metadata_path"]).parent
    metadata = json.loads(Path(summary["metadata_path"]).read_text())
    current_sections = file_sections_by_path(Path(summary["review_diff_path"]).read_text())
    changed_paths = [*current_sections, *summary["excluded_files"]]

    pull_request = json.loads(
        fetch_pr.run_gh_with_stored_login(
            [
                "api",
                "repos/{owner}/{repository}/pulls/{number}".format(**location),
                "--jq",
                "{author_association: .author_association, base_commit: .base.sha}",
            ]
        )
    )
    guidance_files, guidance_lookup = fetch_guidance(
        location, pull_request["base_commit"], changed_paths, output_directory
    )

    local_clone = None
    remote_name = find_local_clone_remote(location)
    if remote_name is not None:
        local_clone = {
            "remote": remote_name,
            "head_commit_available": make_head_commit_available(
                remote_name, location["number"], summary["head_commit"]
            ),
        }

    state_directory = state_directory_for(location)
    previous_round = load_latest_round(state_directory)
    same_head_as_previous_round = (
        previous_round is not None and previous_round["head_commit"] == summary["head_commit"]
    )
    if previous_round is None:
        round_number = 1
    elif same_head_as_previous_round:
        round_number = previous_round["round"]
    else:
        round_number = previous_round["round"] + 1

    files_changed_since_previous_round = None
    if previous_round is not None and not same_head_as_previous_round:
        previous_sections = file_sections_by_path(
            (state_directory / f"round-{previous_round['round']}.diff").read_text()
        )
        files_changed_since_previous_round = [
            path for path, section in current_sections.items() if previous_sections.get(path) != section
        ]

    review_scope = "full"
    scope_diff_path = summary["review_diff_path"]
    if (
        files_changed_since_previous_round is not None
        and round_number >= DIFF_ONLY_FROM_ROUND
        and not arguments.full
    ):
        review_scope = "changed_since_previous_round"
        scope_diff_path = str(output_directory / "changed-since-previous-round.diff")
        Path(scope_diff_path).write_text(
            "".join(current_sections[path] for path in files_changed_since_previous_round)
        )

    open_prior_findings = previous_round["open"] if previous_round is not None else []
    open_prior_findings_path = None
    if open_prior_findings:
        open_prior_findings_path = str(output_directory / "open-prior-findings.json")
        Path(open_prior_findings_path).write_text(json.dumps(open_prior_findings, indent=2))

    round_context_path = output_directory / "round-context.json"
    round_context_path.write_text(
        json.dumps(
            {
                "state_dir": str(state_directory),
                "round": round_number,
                "head_commit": summary["head_commit"],
                "review_diff_path": summary["review_diff_path"],
                "review_scope": review_scope,
            },
            indent=2,
        )
    )

    json.dump(
        {
            **summary,
            "requested_depth": arguments.depth,
            "owner": location["owner"],
            "repository": location["repository"],
            "number": location["number"],
            "author": metadata["author"]["login"],
            "author_is_bot": metadata["author"].get("is_bot", False),
            "author_association": pull_request["author_association"],
            "base_commit": pull_request["base_commit"],
            "guidance_files": guidance_files,
            "guidance_lookup": guidance_lookup,
            "guidance_files_changed_by_pr": [path for path in changed_paths if is_guidance_path(path)],
            "ci_checks": summarise_ci_checks(summary["pull_request_url"]),
            "local_clone": local_clone,
            "state_dir": str(state_directory),
            "round": round_number,
            "previous_round": None
            if previous_round is None
            else {
                "round": previous_round["round"],
                "head_commit": previous_round["head_commit"],
                "recorded_at": previous_round["recorded_at"],
            },
            "same_head_as_previous_round": same_head_as_previous_round,
            "files_changed_since_previous_round": files_changed_since_previous_round,
            "review_scope": review_scope,
            "scope_diff_path": scope_diff_path,
            "open_prior_findings_path": open_prior_findings_path,
            "open_prior_finding_count": len(open_prior_findings),
            "round_context_path": str(round_context_path),
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    try:
        main()
    except fetch_pr.PullRequestFetchError as fetch_error:
        sys.exit(f"ERROR: {fetch_error}")
