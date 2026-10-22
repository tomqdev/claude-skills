#!/usr/bin/env python3
"""Fetch a GitHub pull request's metadata, diff and discussion, and write a review diff.

The review diff omits Unity editor-serialized files. Their YAML is rewritten wholesale by the
editor on save, so reviewing it produces findings about serialization noise rather than code.

Everything comes from GitHub's REST API, because Claude Code cloud sessions block GraphQL. The
one exception is whether review threads are resolved, which REST does not expose: cloud
sessions serve it on a ccr/review_threads route, and elsewhere it comes from GraphQL.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

UNITY_EDITOR_SERIALIZED_GLOBS = (
    "*.anim",
    "*.asset",
    "*.brush",
    "*.controller",
    "*.cubemap",
    "*.flare",
    "*.fontsettings",
    "*.guiskin",
    "*.lighting",
    "*.mask",
    "*.mat",
    "*.meta",
    "*.mixer",
    "*.overrideController",
    "*.physicMaterial",
    "*.physicsMaterial2D",
    "*.playable",
    "*.prefab",
    "*.preset",
    "*.renderTexture",
    "*.shadervariants",
    "*.signal",
    "*.spriteatlas",
    "*.spriteatlasv2",
    "*.terrainlayer",
    "*.unity",
)

PULL_REQUEST_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repository>[^/]+)/pull/(?P<number>\d+)(?:[/?#].*)?$"
)

JIRA_TICKET_KEY = r"[A-Z][A-Z0-9]+-\d+"
BRANCH_JIRA_TICKET_KEY_PATTERN = re.compile(rf"^(?:[^/]+/)*({JIRA_TICKET_KEY})(?![A-Za-z0-9])")
TITLE_JIRA_TICKET_KEY_PATTERN = re.compile(rf"^\[?({JIRA_TICKET_KEY})(?![A-Za-z0-9])")
JIRA_BROWSE_LINK_PATTERN = re.compile(rf"/browse/({JIRA_TICKET_KEY})(?![A-Za-z0-9])")

REST_PAGE_SIZE = 100

# Not documented, so the review thread parser accepts GraphQL-style and REST-style field names.
CCR_REVIEW_THREADS_ROUTE = "repos/{owner}/{repository}/pulls/{number}/ccr/review_threads"
CCR_THREAD_LIST_KEYS = ("review_threads", "reviewThreads", "threads", "nodes")
CCR_THREAD_COMMENT_ID_KEYS = ("comment_id", "root_comment_id", "first_comment_id")
CCR_COMMENT_ID_KEYS = ("id", "database_id", "databaseId", "comment_id")

REVIEW_THREAD_RESOLUTION_QUERY = """
query($owner: String!, $repository: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repository) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved resolvedBy { login } comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""


class PullRequestFetchError(RuntimeError):
    pass


def run_gh_with_stored_login(gh_arguments: list[str]) -> str:
    environment_without_gh_token = {
        name: value for name, value in os.environ.items() if name != "GH_TOKEN"
    }
    completed_process = subprocess.run(
        ["gh", *gh_arguments],
        env=environment_without_gh_token,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_process.returncode != 0:
        raise PullRequestFetchError(
            f"`gh {' '.join(gh_arguments)}` failed with exit code "
            f"{completed_process.returncode}:\n{completed_process.stderr.strip()}"
        )
    return completed_process.stdout


def fetch_all_pages(api_path: str, items_key: str | None = None) -> list[dict]:
    """Every item of a paginated REST list. items_key names the list when the response wraps it."""
    separator = "&" if "?" in api_path else "?"
    items: list[dict] = []
    page_number = 1
    while True:
        response = json.loads(
            run_gh_with_stored_login(
                ["api", f"{api_path}{separator}per_page={REST_PAGE_SIZE}&page={page_number}"]
            )
        )
        page_items = response[items_key] if items_key else response
        items.extend(page_items)
        if len(page_items) < REST_PAGE_SIZE:
            return items
        page_number += 1


def describe_metadata_author(user: dict) -> dict:
    """The PR author as `gh pr view --json author` gives it. Cloud sessions can't read users/,
    so the name is unknown."""
    if user["type"] == "Bot":
        return {"is_bot": True, "login": user["login"]}
    return {"id": user["node_id"], "is_bot": False, "login": user["login"], "name": None}


def describe_commit(commit: dict) -> dict:
    """A PR commit as `gh pr view --json commits` gives it. REST names only the commit's author,
    not co-authors from Co-authored-by trailers."""
    headline, _, body = commit["commit"]["message"].partition("\n")
    github_user = commit["author"] or {}
    return {
        "authoredDate": commit["commit"]["author"]["date"],
        "authors": [
            {
                "email": commit["commit"]["author"]["email"],
                "id": github_user.get("node_id", ""),
                "login": github_user.get("login", ""),
                "name": commit["commit"]["author"]["name"],
            }
        ],
        "committedDate": commit["commit"]["committer"]["date"],
        "messageBody": body.strip(),
        "messageHeadline": headline,
        "oid": commit["sha"],
    }


def build_metadata(pull_request: dict, pull_request_path: str, canonical_url: str) -> dict:
    """The fields `gh pr view --json` gave this script, with the same names and shapes."""
    return {
        "additions": pull_request["additions"],
        "author": describe_metadata_author(pull_request["user"]),
        "baseRefName": pull_request["base"]["ref"],
        "body": pull_request["body"] or "",
        "commits": [
            describe_commit(commit) for commit in fetch_all_pages(f"{pull_request_path}/commits")
        ],
        "deletions": pull_request["deletions"],
        "files": [
            {
                "path": file["filename"],
                "additions": file["additions"],
                "deletions": file["deletions"],
            }
            for file in fetch_all_pages(f"{pull_request_path}/files")
        ],
        "headRefName": pull_request["head"]["ref"],
        "title": pull_request["title"],
        "url": canonical_url,
    }


def describe_author(node: dict) -> dict:
    # GitHub returns a null user for some deleted accounts and displays them as "ghost".
    if node["user"] is None:
        return {"author": "ghost", "author_is_bot": False}
    return {"author": node["user"]["login"], "author_is_bot": node["user"]["type"] == "Bot"}


def group_review_comments_into_threads(review_comments: list[dict]) -> list[list[dict]]:
    """Review comments grouped by thread, oldest first. A reply's in_reply_to_id is the comment
    it answers."""
    comments_by_id = {comment["id"]: comment for comment in review_comments}
    threads: dict[int, list[dict]] = {}
    for comment in sorted(review_comments, key=lambda comment: comment["id"]):
        root = comment
        while root.get("in_reply_to_id") in comments_by_id:
            root = comments_by_id[root["in_reply_to_id"]]
        threads.setdefault(root["id"], []).append(comment)
    return list(threads.values())


def login_of(user: object) -> str | None:
    if isinstance(user, dict):
        user = user.get("login")
    return user if isinstance(user, str) else None


def first_present(node: dict, keys: tuple[str, ...]) -> object:
    return next((node[key] for key in keys if key in node), None)


def integer_ids(node: dict, keys: tuple[str, ...]) -> set[int]:
    values = [node.get(key) for key in keys]
    return {
        int(value)
        for value in values
        if (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, str) and value.isdigit())
    }


def fetch_resolutions_from_ccr_route(pull_request_location: dict[str, str]) -> dict[int, dict]:
    response = json.loads(
        run_gh_with_stored_login(["api", CCR_REVIEW_THREADS_ROUTE.format(**pull_request_location)])
    )
    threads = (
        response if isinstance(response, list) else first_present(response, CCR_THREAD_LIST_KEYS)
    )
    if not isinstance(threads, list):
        raise PullRequestFetchError(f"unexpected response: {json.dumps(response)[:200]}")
    resolutions = {}
    for thread in threads:
        is_resolved = first_present(thread, ("is_resolved", "isResolved", "resolved"))
        if not isinstance(is_resolved, bool):
            raise PullRequestFetchError(f"no resolved flag in thread: {json.dumps(thread)[:200]}")
        resolution = {
            "is_resolved": is_resolved,
            "resolved_by": login_of(first_present(thread, ("resolved_by", "resolvedBy"))),
        }
        comments = thread.get("comments") or []
        if isinstance(comments, dict):
            comments = comments.get("nodes") or []
        comment_ids = integer_ids(thread, CCR_THREAD_COMMENT_ID_KEYS).union(
            *(integer_ids(comment, CCR_COMMENT_ID_KEYS) for comment in comments)
        )
        resolutions.update(dict.fromkeys(comment_ids, resolution))
    return resolutions


def fetch_resolutions_from_graphql(pull_request_location: dict[str, str]) -> dict[int, dict]:
    resolutions = {}
    page_cursor = None
    while True:
        gh_arguments = [
            "api",
            "graphql",
            "-f",
            f"query={REVIEW_THREAD_RESOLUTION_QUERY}",
            "-f",
            f"owner={pull_request_location['owner']}",
            "-f",
            f"repository={pull_request_location['repository']}",
            "-F",
            f"number={pull_request_location['number']}",
        ]
        if page_cursor is not None:
            gh_arguments += ["-f", f"cursor={page_cursor}"]
        response = json.loads(run_gh_with_stored_login(gh_arguments))
        connection = response["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            resolution = {
                "is_resolved": thread["isResolved"],
                "resolved_by": thread["resolvedBy"]["login"] if thread["resolvedBy"] else None,
            }
            for comment in thread["comments"]["nodes"]:
                resolutions[comment["databaseId"]] = resolution
        if not connection["pageInfo"]["hasNextPage"]:
            return resolutions
        page_cursor = connection["pageInfo"]["endCursor"]


RESOLUTION_SOURCES = (
    ("ccr/review_threads route", fetch_resolutions_from_ccr_route),
    ("GraphQL", fetch_resolutions_from_graphql),
)


def find_thread_resolution(
    thread_comments: list[dict], resolutions: dict[int, dict]
) -> dict | None:
    return next(
        (resolutions[comment["id"]] for comment in thread_comments if comment["id"] in resolutions),
        None,
    )


def fetch_review_thread_resolutions(
    pull_request_location: dict[str, str], threads: list[list[dict]]
) -> tuple[dict[int, dict], list[str]]:
    """Resolution of each thread, keyed by review comment id, and why some could not be found.

    Tries each source until every thread has an answer. Earlier sources win where both answer.
    """
    resolutions: dict[int, dict] = {}
    problems = []
    for source_name, fetch_resolutions in RESOLUTION_SOURCES:
        try:
            resolutions = {**fetch_resolutions(pull_request_location), **resolutions}
        except (PullRequestFetchError, LookupError, TypeError, ValueError) as fetch_error:
            # The last line is gh's own error, without the command that came before it.
            error_lines = str(fetch_error).splitlines() or [type(fetch_error).__name__]
            problems.append(f"{source_name}: {error_lines[-1]}")
            continue
        if all(find_thread_resolution(thread, resolutions) for thread in threads):
            return resolutions, []
        problems.append(f"{source_name}: did not cover every review thread")
    return resolutions, problems


def is_resolved_by_author_without_reply(review_thread: dict, pull_request_author: str) -> bool:
    thread_comment_authors = {comment["author"] for comment in review_thread["comments"]}
    return (
        review_thread["resolved_by"] == pull_request_author
        and pull_request_author not in thread_comment_authors
    )


def fetch_discussion(
    pull_request_location: dict[str, str], pull_request_path: str, pull_request_author: str
) -> tuple[dict, list[str]]:
    """The PR discussion, and why some review threads' resolved state is unknown, if it is.
    Their is_resolved is None."""
    threads = group_review_comments_into_threads(fetch_all_pages(f"{pull_request_path}/comments"))
    resolutions, resolution_problems = (
        fetch_review_thread_resolutions(pull_request_location, threads) if threads else ({}, [])
    )
    unknown_resolution = {"is_resolved": None, "resolved_by": None}
    review_threads = []
    for thread_comments in threads:
        root_comment = thread_comments[0]
        resolution = find_thread_resolution(thread_comments, resolutions) or unknown_resolution
        review_thread = {
            "path": root_comment["path"],
            "line": root_comment.get("line"),
            "original_line": root_comment.get("original_line"),
            "diff_side": root_comment.get("side"),
            "is_resolved": resolution["is_resolved"],
            "resolved_by": resolution["resolved_by"],
            # REST drops the position of a line comment whose code has changed since.
            "is_outdated": root_comment.get("position") is None
            and root_comment.get("subject_type") != "file",
            "original_diff_hunk": root_comment["diff_hunk"],
            "comments": [
                {
                    **describe_author(comment),
                    "body": comment["body"],
                    "created_at": comment["created_at"],
                    "url": comment["html_url"],
                }
                for comment in thread_comments
            ],
        }
        review_thread["resolved_by_author_without_reply"] = is_resolved_by_author_without_reply(
            review_thread, pull_request_author
        )
        review_threads.append(review_thread)
    review_summaries = [
        {
            **describe_author(review),
            "state": review["state"],
            "body": review["body"],
            "submitted_at": review["submitted_at"],
            "url": review["html_url"],
        }
        for review in fetch_all_pages(f"{pull_request_path}/reviews")
        if (review["body"] or "").strip()
    ]
    issue_path = "repos/{owner}/{repository}/issues/{number}".format(**pull_request_location)
    conversation_comments = [
        {
            **describe_author(comment),
            "body": comment["body"],
            "created_at": comment["created_at"],
            "url": comment["html_url"],
        }
        for comment in fetch_all_pages(f"{issue_path}/comments")
    ]
    return {
        "pull_request_author": pull_request_author,
        "review_threads": review_threads,
        "review_summaries": review_summaries,
        "conversation_comments": conversation_comments,
    }, resolution_problems


def find_jira_ticket_keys(metadata: dict) -> list[str]:
    ticket_keys = []
    for prefix_pattern, text in (
        (BRANCH_JIRA_TICKET_KEY_PATTERN, metadata["headRefName"]),
        (TITLE_JIRA_TICKET_KEY_PATTERN, metadata["title"]),
    ):
        prefix_match = prefix_pattern.match(text)
        if prefix_match:
            ticket_keys.append(prefix_match.group(1))
    ticket_keys += JIRA_BROWSE_LINK_PATTERN.findall(f"{metadata['title']}\n{metadata['body']}")
    return list(dict.fromkeys(ticket_keys))


def split_diff_into_file_sections(diff_text: str) -> list[str]:
    return [section for section in re.split(r"(?m)^(?=diff --git )", diff_text) if section]


def git_numstat_per_file_section(diff_text: str) -> list[tuple[int, int, str]]:
    """Per-file (added, deleted, new path), in diff order, parsed by git itself.

    Format of `git apply --numstat -z`: https://git-scm.com/docs/git-apply#Documentation/git-apply.txt--z
    """
    completed_process = subprocess.run(
        ["git", "apply", "--numstat", "-z", "-"],
        input=diff_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_process.returncode != 0:
        raise PullRequestFetchError(
            f"git could not parse the PR diff:\n{completed_process.stderr.strip()}"
        )
    numstat_tokens = completed_process.stdout.split("\0")
    per_file_stats = []
    token_index = 0
    while token_index < len(numstat_tokens) - 1:
        added_text, deleted_text, path = numstat_tokens[token_index].split("\t", 2)
        is_rename_or_copy = path == ""
        if is_rename_or_copy:
            path = numstat_tokens[token_index + 2]
            token_index += 3
        else:
            token_index += 1
        is_binary = added_text == "-"
        per_file_stats.append(
            (0 if is_binary else int(added_text), 0 if is_binary else int(deleted_text), path)
        )
    return per_file_stats


def is_excluded(path: str, excluded_globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in excluded_globs)


def parse_arguments() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("pull_request_url")
    argument_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Extra path glob to leave out of the review diff. Repeatable.",
    )
    argument_parser.add_argument(
        "--keep-editor-serialized",
        action="store_true",
        help="Keep Unity editor-serialized files in the review diff.",
    )
    return argument_parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    url_match = PULL_REQUEST_URL_PATTERN.match(arguments.pull_request_url)
    if url_match is None:
        raise PullRequestFetchError(
            f"Not a GitHub pull request URL: {arguments.pull_request_url!r}. "
            "Expected https://github.com/<owner>/<repo>/pull/<number>."
        )
    canonical_url = "https://github.com/{owner}/{repository}/pull/{number}".format(
        **url_match.groupdict()
    )

    pull_request_path = "repos/{owner}/{repository}/pulls/{number}".format(**url_match.groupdict())
    pull_request = json.loads(run_gh_with_stored_login(["api", pull_request_path]))
    head_commit = pull_request["head"]["sha"]
    metadata = build_metadata(pull_request, pull_request_path, canonical_url)
    full_diff_text = run_gh_with_stored_login(
        ["api", pull_request_path, "-H", "Accept: application/vnd.github.diff"]
    )

    file_sections = split_diff_into_file_sections(full_diff_text)
    per_file_stats = git_numstat_per_file_section(full_diff_text)
    if len(file_sections) != len(per_file_stats):
        raise PullRequestFetchError(
            f"Split the diff into {len(file_sections)} file sections but git parsed "
            f"{len(per_file_stats)} files."
        )

    total_added = sum(added for added, _, _ in per_file_stats)
    total_deleted = sum(deleted for _, deleted, _ in per_file_stats)
    if (total_added, total_deleted) != (metadata["additions"], metadata["deletions"]):
        raise PullRequestFetchError(
            f"Diff line counts (+{total_added} -{total_deleted}) do not match PR metadata "
            f"(+{metadata['additions']} -{metadata['deletions']}) for "
            f"{metadata['headRefName']} -> {metadata['baseRefName']}. The diff does not "
            "describe this PR, or the head moved between fetches. Do not review it."
        )

    excluded_globs = list(arguments.exclude)
    if not arguments.keep_editor_serialized:
        excluded_globs.extend(UNITY_EDITOR_SERIALIZED_GLOBS)
    review_sections = []
    excluded_paths = []
    for section, (_, _, path) in zip(file_sections, per_file_stats):
        if is_excluded(path, excluded_globs):
            excluded_paths.append(path)
        else:
            review_sections.append(section)

    output_directory = Path(
        tempfile.mkdtemp(prefix="review-pr-{owner}-{repository}-{number}-".format(
            **url_match.groupdict()
        ))
    )
    metadata_path = output_directory / "metadata.json"
    review_diff_path = output_directory / "review.diff"
    discussion_path = output_directory / "discussion.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))
    review_diff_path.write_text("".join(review_sections))
    discussion, resolution_problems = fetch_discussion(
        url_match.groupdict(), pull_request_path, metadata["author"]["login"]
    )
    discussion_path.write_text(json.dumps(discussion, indent=2))

    resolution_unavailable = (
        {"review_thread_resolution_unavailable": "; ".join(resolution_problems)}
        if resolution_problems
        else {}
    )
    json.dump(
        {
            "pull_request_url": canonical_url,
            "base_branch": metadata["baseRefName"],
            "head_branch": metadata["headRefName"],
            "head_commit": head_commit,
            "additions": metadata["additions"],
            "deletions": metadata["deletions"],
            "metadata_path": str(metadata_path),
            "review_diff_path": str(review_diff_path),
            "reviewed_file_count": len(review_sections),
            "excluded_files": excluded_paths,
            "jira_ticket_keys": find_jira_ticket_keys(metadata),
            "discussion_path": str(discussion_path),
            "review_thread_count": len(discussion["review_threads"]),
            # Threads whose resolved state is unknown count as unresolved.
            "unresolved_review_thread_count": sum(
                not thread["is_resolved"] for thread in discussion["review_threads"]
            ),
            **resolution_unavailable,
            "resolved_by_author_without_reply_count": sum(
                thread["resolved_by_author_without_reply"] for thread in discussion["review_threads"]
            ),
            "review_summary_count": len(discussion["review_summaries"]),
            "conversation_comment_count": len(discussion["conversation_comments"]),
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    try:
        main()
    except PullRequestFetchError as fetch_error:
        sys.exit(f"ERROR: {fetch_error}")
