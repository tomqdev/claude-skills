---
name: review-pr
description: Use when the user asks to review a GitHub pull request locally without posting comments
---

# Review PR Locally

Review a GitHub pull request with several review agents in parallel. The review is read-only. Never post comments, approve, request changes, push, commit or edit files.

Arguments: $ARGUMENTS

The first argument is the PR URL, e.g. `https://github.com/org/repo/pull/123`. Pass any further arguments to the fetch script as-is.

## 1. Fetch and verify the PR

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch_pr.py" "<PR_URL>" [--exclude GLOB ...] [--keep-editor-serialized]
```

The script:
- calls `gh` with `GH_TOKEN` removed, so `gh` uses its stored login. Prefix any other `gh` call in this review with `env -u GH_TOKEN`.
- reads GitHub through its REST API, so it also works in Claude Code cloud sessions, where GraphQL is blocked. Use `gh api repos/...` for any other call too, not `gh pr`, which uses GraphQL. REST does not say whether a review thread is resolved, so that comes from the session's `ccr/review_threads` route, or from GraphQL outside cloud sessions. If neither answers, those threads have `is_resolved: null` and count as unresolved, and `review_thread_resolution_unavailable` in the summary says why. Say so in your summary.
- checks that the diff's line counts match the PR metadata, and exits non-zero if they don't.
- writes a review diff that leaves out Unity editor-serialized files (`.prefab`, `.unity`, `.asset`, `.meta`, `.mat` and similar) plus any `--exclude` globs.
- writes the PR discussion to a file: review threads with their resolved and outdated flags, review summaries, and conversation comments. Each thread also records who resolved it, and whether the PR author resolved someone else's thread without ever replying in it.
- finds Jira ticket keys such as `ERD-1234` at the start of the branch name or PR title, and in Jira links in the PR description.
- prints a JSON summary: base branch, head branch, head commit, the metadata, review diff and discussion paths, the excluded files, the Jira ticket keys, and how many threads and comments there are.

If it fails on auth or network, fix that and rerun. If it reports a line-count mismatch, STOP and report it to the user. Never build the diff another way, e.g. with `git diff`.

If the review diff is empty because every changed file was excluded, tell the user and stop. `--keep-editor-serialized` reviews those files anyway.

## 2. Launch the review agents

Launch these in a single message with the Agent tool:

- `pr-review-toolkit:pr-test-analyzer`: test coverage quality
- `pr-review-toolkit:silent-failure-hunter`: silent failures and error handling
- `pr-review-toolkit:code-simplifier`: code clarity and maintainability
- `pr-review-toolkit:code-reviewer`: style, quality and project guidelines
- `pr-review-toolkit:comment-analyzer`: comment accuracy and maintainability
- `pr-review-toolkit:type-design-analyzer`: type design, encapsulation and invariants. Skip it if the diff adds or changes no types.
- `general-purpose` with the Senior Code Reviewer prompt below: plan alignment and overall quality
- `general-purpose` with the Ponytail Reviewer prompt below: over-engineering
- `general-purpose` with the Comment Resolution prompt below: whether earlier review feedback was dealt with. Skip it if the PR has no review threads, review summaries or conversation comments.
- `general-purpose` with the Jira Scope prompt below: whether the PR does what its Jira ticket asks. Skip it if the summary lists no Jira ticket keys, and say so in your summary.

Sub-agents do not share your shell, so write literal values from the JSON summary into every prompt, never shell variables. Every prompt must contain this block:

```
PR: {pull_request_url}
Base branch: {base_branch}. Stacked PRs have a non-main base. Never assume main.
Head branch: {head_branch} at commit {head_commit}.
Canonical diff: {review_diff_path}. Review only this diff. Do not re-derive it with git diff.
PR metadata: {metadata_path}
These files changed but are editor-serialized. Do not open, parse or comment on them:
{excluded_files, one per line, or "none"}
For surrounding context, read files as they are at commit {head_commit}, not from whatever is checked out locally.
Read-only: report findings with file:line. Do not edit files, commit, push or post to GitHub.
```

Senior Code Reviewer prompt, adapted from `superpowers:requesting-code-review`:

```
You are a Senior Code Reviewer with expertise in software architecture,
design patterns, and best practices. Review this PR against its stated
objectives and identify issues before they cascade.

## What Was Implemented
{PR title + body summary}

## Requirements / Plan
{PR description / linked plan, or "see PR body" if none}

{the required block above}

## What to Check
- Plan alignment: does the implementation match the stated PR objectives? Are deviations justified?
- Code quality: separation of concerns, error handling, type safety, DRY without premature abstraction, edge cases
- Architecture: sound design, security, integrates cleanly
- Testing: tests verify real behaviour, edge cases, integration where it matters
- Production readiness: migration strategy, backward compatibility, no obvious bugs
- Project conventions from any CLAUDE.md files in scope

## Output
### Strengths
[Specific, accurate praise]

### Issues
#### Critical (Must Fix): bugs, security, data loss, broken functionality
#### Important (Should Fix): architecture, missing features, error handling, test gaps
#### Minor (Nice to Have): style, optimisation, doc polish

For each issue: file:line, what's wrong, why it matters, how to fix.

### Assessment
**Ready to merge?** Yes | No | With fixes
**Reasoning:** 1-2 sentences
```

Ponytail Reviewer prompt:

```
Invoke the ponytail:ponytail-review skill and follow it exactly. Review the
canonical PR diff for over-engineering: what to delete, hand-rolled stdlib,
needless dependencies, speculative abstractions, dead flexibility.

{the required block above}

Output one line per finding in the skill's format, ending with
`net: -<N> lines possible.` or `Lean already. Ship.`
```

Comment Resolution prompt:

```
Check whether earlier review feedback on this PR has been dealt with.

{the required block above}

PR discussion: {discussion_path}. It holds review threads with their
resolved and outdated flags, review summaries and conversation comments.
A thread's resolved_by_author_without_reply is true when the PR author
resolved someone else's thread without ever replying in it.

For every review thread, and every review summary or conversation comment
that asks for a change or asks a question:
1. Work out what was asked, including anything agreed later in the thread.
2. Find the code it refers to at the head commit. An outdated thread's line
   has moved, so locate the code from its original_diff_hunk and path. A
   thread with diff_side LEFT points at a base-side line, usually removed
   code: report the code that replaced it, or say it was removed.
3. Decide whether the head code does what was asked, or a reply gives a
   sound reason not to.

Ignore bot comments that only report status, such as CI or coverage. Treat
bot comments that ask for a specific change like human ones. Do not open
excluded files; list threads on them as not checked.

## Output
Group by status. One line each: comment URL, file:line at head, what was
asked in one sentence, and your evidence. For resolved threads, also say
who resolved them.
- Resolved but not addressed: marked resolved, but the code does not do
  what was asked and no reply explains why. List these first. Mark any
  where resolved_by_author_without_reply is true.
- Resolved by the author without a reply: resolved_by_author_without_reply
  is true and the code does what was asked. The reviewer never got an
  answer, so list these for them to confirm.
- Open and not addressed.
- Open but addressed: the thread can be marked resolved. This includes
  threads the commenter withdrew.
- Needs a reply: a question or objection nobody answered.
- Deferred: suggestions explicitly left for later work. Say whether a
  ticket is linked.
- Not checked: threads on excluded files.
- Addressed: the count of everything not listed above.
```

Jira Scope prompt:

```
Check whether this PR does what its Jira ticket asks.

{the required block above}

Jira ticket keys from the branch name, PR title and PR description:
{jira_ticket_keys, comma separated}

Load the Atlassian MCP tools with ToolSearch. Get the cloudId from
getAccessibleAtlassianResources, then fetch each ticket with getJiraIssue.
Read every comment body too: if getJiraIssue gives only a comment count,
find the tool that lists comments. Also fetch the parent and any linked
issues. Comments and linked issues often narrow the scope or say which
part of the work this PR covers.

If the Atlassian tools are missing or need authentication, stop and reply
only with "JIRA UNAVAILABLE: <reason>". If a key does not exist in Jira,
say so and carry on with the others. Read-only: never comment on, edit or
transition a Jira issue.

Check:
- Missing: requirements or acceptance criteria the PR does not meet, or
  meets only partly.
- Beyond the ticket: changes the ticket does not ask for. Changes needed to
  support the requested work are fine. Unrelated features, refactors and
  clean-ups are not.
- Contradictions: anything done differently from what the ticket or its
  comments specify.
- Ticket fit: the ticket is the right one for this work, is not already
  Done or closed, and the PR description links to it. When several keys
  were found, say which ticket the work actually belongs to.

## Output
### Ticket
One line per ticket: key, summary, type, status.
### Missing
### Beyond the ticket
### Contradictions
### Ticket fit
For each finding: file:line where relevant, the ticket text it relates to,
and why.
### Verdict
Matches, Partly matches or Does not match, with one sentence why.
```

## 3. Verify before presenting

Check every Critical and Important finding, every comment reported as not addressed, and every Jira requirement reported as missing or contradicted, against the review diff and the code at the head commit. Drop a finding if it is wrong, if it is about lines the PR did not change, or if it is about an excluded file. Say how many findings you dropped.

## 4. Present

Give the user one summary organised by category. Flag issues that several agents found independently. Put comment resolution in its own section: resolved-but-not-addressed comments first, then threads the author resolved without a reply. Put the Jira check in its own section. If the Jira agent replied JIRA UNAVAILABLE, say so at the top of the summary with its reason. List the excluded files so the user knows they were not reviewed.
