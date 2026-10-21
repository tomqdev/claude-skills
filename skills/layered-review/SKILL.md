---
name: layered-review
description: Use when the user asks for a merge-gate review of a GitHub pull request locally: only verified bugs, a separate security pass, and how many humans need to approve it. With --junior, also suggests better ways to write the code, for PRs from junior engineers. Never posts comments.
---

# Layered PR review

Review a GitHub pull request the way the Claude Code team describes in [Solving code review at Anthropic](https://claude.dev/blog/solving-code-review-at-anthropic/). Bug hunters work in parallel. Separate verifiers vote on every finding, and only findings that pass are reported. A separate pass looks for security holes, and a risk tier says how many people need to approve the PR. Leave out style, cleanup and coverage nags: `review-pr` covers those.

`--junior` is for code from junior engineers. It adds a craft hunter that finds code that works but could be written better, such as inefficient functions, hand-rolled helpers, and unidiomatic or tangled code. Each suggestion is verified and shows the better version.

The review is read-only. Never post comments, approve, request changes, push, commit or edit files. The only writes allowed are the review's temp files, its round state, and fetching the PR's head commit into a local clone.

Arguments: $ARGUMENTS

The first argument is the PR URL, e.g. `https://github.com/org/repo/pull/123`. Pass every argument to the prepare script as-is.

## 1. Prepare

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/prepare.py" "<PR_URL>" [--depth quick|standard|deep] [--junior] [--full] [--exclude GLOB ...] [--keep-editor-serialized]
```

The script runs review-pr's `fetch_pr.py`, so everything review-pr says about the fetch holds here. `gh` runs with `GH_TOKEN` removed, so prefix any other `gh` call in this review with `env -u GH_TOKEN`. The diff's line counts are checked against the PR metadata. Unity editor-serialized files and any `--exclude` globs are left out of the review diff. The PR discussion is written to a file, and Jira ticket keys are found. Then the script adds:
- review guidance from the base branch: every `CLAUDE.md` and `REVIEW.md` at the root or in a directory the PR touches, plus `CODEOWNERS` and the PR template. They come from the base so a PR cannot rewrite the rules it is reviewed against. Guidance files the PR changes are listed in `guidance_files_changed_by_pr`.
- the author's association with the repository, such as `MEMBER`, `CONTRIBUTOR` or `NONE`, and whether they are a bot.
- CI checks on the head commit, counted by result.
- the head commit, fetched into the current directory's clone if it is a clone of the PR's repository. The fetch creates no branch and leaves the checkout alone. `local_clone.head_commit_available` says whether it worked.
- the round. Each finished run on this PR is saved in `state_dir`. The summary gives the round number and the findings still open from the previous round. From round 3 on, `scope_diff_path` holds only the files whose changes moved since the previous round. `--full` reviews the whole diff anyway. A rerun at an unchanged head commit repeats the same round.

If it fails on auth or network, fix that and rerun. If it reports a line-count mismatch, STOP and report it to the user. Never build the diff another way, e.g. with `git diff`. If the review diff is empty because every changed file was excluded, tell the user and stop.

## 2. Read the guidance and set the depth

Read every file in `guidance_files`. From `REVIEW.md` files, note:
- what failure looks like in this repository or area
- paths to skip
- a cap on nits. The default is 5.
- a cap on suggestions in junior mode. The default is 10.
- paths that always need a person
- a review depth
- anything else it tells the reviewer to do or not do

A `REVIEW.md` in a subdirectory applies to that directory, on top of the ones above it. From `CLAUDE.md` files, note the conventions, goals and non-goals they state.

Use the base versions even when the PR changes them. The PR's changes to guidance files are reviewed like any other code.

The depth comes from `requested_depth` if it is set, then from a depth that `REVIEW.md` asks for, otherwise `standard`.

| Depth | Bug hunters | Verifiers | REAL votes to keep |
|---|---|---|---|
| quick | 1: intent, breakage and line-level together | 1 | 1 |
| standard | 3: intent, breakage, line-level | 3 | 2 |
| deep | 5: standard plus state, tests | 5 | 3 |

Every depth also runs the security hunter and the risk classifier. When `junior` is true, every depth also runs the craft hunter. When the PR touches billing, auth, data migrations, release or update machinery, or CI workflows and was not reviewed at `deep`, say in the summary that a deep review is recommended.

## 3. Hunt

Launch these in a single message with the Agent tool, all `general-purpose`:
- the bug hunters for the depth, one per lens
- the security hunter
- the craft hunter, if `junior` is true
- the risk classifier
- the prior-findings checker, if `open_prior_finding_count` is not zero and `same_head_as_previous_round` is false. At an unchanged head, earlier findings stay open as they were.
- the Jira checker, in round 1 only, if `jira_ticket_keys` is not empty and ToolSearch finds the Atlassian `getJiraIssue` tool. Use the Jira Scope prompt from `${CLAUDE_SKILL_DIR}/../review-pr/SKILL.md`, with this skill's required block in place of review-pr's. If the tool is missing, skip the checker and say so in the summary. Later rounds skip it too.

If `review_scope` is `changed_since_previous_round` and `files_changed_since_previous_round` is empty, skip the bug, security and craft hunters, because nothing they would review has changed.

Sub-agents do not share your shell, so write literal values from the JSON summary into every prompt, never shell variables. Every prompt must contain this block:

```
PR: {pull_request_url}
Base branch: {base_branch}. Stacked PRs have a non-main base. Never assume main.
Head branch: {head_branch} at commit {head_commit}.
Canonical diff: {review_diff_path}. Do not re-derive it with git diff.
PR metadata: {metadata_path}
PR discussion: {discussion_path}
Do not open, parse or comment on these paths. They are excluded or REVIEW.md skips them:
{excluded_files and REVIEW.md skip paths, one per line, or "none"}
Review guidance, from the base branch. Read it first:
{each guidance file: local_path, and what it is, e.g. "REVIEW.md for src/app/"}
{how to read code, below}
Read-only: do not edit files, commit, push or post to GitHub or anywhere else.
```

For how to read code, use this when `local_clone.head_commit_available` is true:

```
Read code as it is at commit {head_commit}, never from the working tree:
`git show {head_commit}:<path>` to read a file, `git grep -n <pattern> {head_commit} -- <paths>` to search.
```

Otherwise:

```
Read code as it is at commit {head_commit}, never from the local checkout:
env -u GH_TOKEN gh api "repos/{owner}/{repository}/contents/<path>?ref={head_commit}" -H "Accept: application/vnd.github.raw"
You cannot search the whole repository. Say so in your output when that limits a finding.
```

The bug, security and craft hunters also get:

```
Review scope: {scope_diff_path}.
{when review_scope is changed_since_previous_round: "This is round {round}. Only these files changed since the previous round: {files_changed_since_previous_round}. Review only their hunks. Read the canonical diff for context."}
Already reported. Do not report these again, or anything a review thread in the PR discussion already raised:
{open prior findings, one line each: id, file:line, title, or "none"}
```

Bug hunter prompt:

```
You are hunting for bugs in a pull request. Find real defects that this
change introduces or exposes: code that will behave wrongly, crash, lose or
corrupt data, break a caller, or fail to do what the PR says it does. Do not
report style, naming, formatting, refactors, missing comments, test coverage
or code you would merely write differently. Other agents will try to
disprove every finding you report, so report only what you can show.

## What the PR is for
{PR title, and the body summarised in a few sentences}

{the required block, scope and already-reported list above}

## Your lens
{lens}

## Rules
- Review the change against the whole repository, not just the diff. Open
  the callers, implementations, config and tests that the changed code
  touches.
- Every finding needs evidence: a file:line you read, and the concrete
  inputs or state that trigger it. Never infer behaviour from a name.
- When you find a bug, name its class and look for every other instance in
  the diff and in code the diff calls. Report the class once, with every
  location.
- Report a bug in code the PR did not change only if the change makes it
  reachable, makes it worse, or sits right next to it. Mark it pre-existing.
- Weigh most heavily what REVIEW.md says failure looks like here.
- You may enforce a rule that CLAUDE.md or REVIEW.md states. Quote it. A
  CLAUDE.md line this change makes untrue is a finding.
- If you find nothing, say so. An empty report is a good outcome.

## Output
One block per finding:
### <short title>
- Location: file:line, and every other instance
- Class: the bug class in a few words
- Trigger: the inputs or state that make it go wrong
- Impact: what goes wrong, and for whom
- Pre-existing: yes or no
- Fix: one or two sentences
```

Lenses:
- Intent: Does the change do what the PR says, completely? For a bug fix, does it fix the root cause or only the symptom that was reported, and is every place with the same bug fixed? For a feature, does it handle the inputs and states it will meet in real use? Is anything the description promises missing?
- Breakage: What else does the change affect? Check callers of changed functions, implementations of changed interfaces, consumers of changed data, config, schemas, APIs and CLI flags, files and state already on disk or deployed, and backward compatibility. Check the conventions that CLAUDE.md and REVIEW.md state, and CLAUDE.md lines the change makes untrue.
- Line-level: Look for logic errors in the changed lines: wrong conditions, off-by-one, null or empty handling, wrong units or types, error handling that hides failures or leaves state half-written, leaked resources, wrong defaults and copy-paste mistakes.
- State (deep only): Look at concurrency, ordering, retries and idempotency, caching and invalidation, lifecycle (start, stop, reload), partial failure, and what happens when the code runs twice or not at all.
- Tests (deep only): Do the tests that the PR adds or changes exercise the behaviour it changes? Would they fail if the change were reverted? Tests that pass whatever the code does, assert the wrong thing, or were changed to match a bug are findings. Missing coverage on its own is not.

Craft hunter prompt, junior mode only:

```
You are reviewing a pull request from a junior engineer, to help them write
better code. Find code this PR adds or changes that works but could be
written better, and show the better way. Other agents hunt for bugs; if you
spot one, report it too. Other agents will also check every suggestion, so
suggest only what you can show is better.

## What the PR is for
{PR title, and the body summarised in a few sentences}

{the required block, scope and already-reported list above}

## Look for
- Efficiency: repeated work inside loops, linear searches where a set or
  dict lookup would do, I/O, queries or network calls that could be batched
  or hoisted, building or reading more data than needed, needless copies.
- Reuse: hand-rolled code for something that the standard library, a
  dependency the project already has, or an existing helper in this
  repository already does. Name the function and where it lives.
- Idiom: constructs that fight the language or framework when an idiomatic
  form is shorter and clearer.
- Simplicity: deep nesting, functions doing several jobs, flag arguments,
  duplicated blocks, dead code, and abstraction or configurability that
  nothing needs.
- Clarity: names that mislead, magic numbers, and comments that repeat or
  contradict the code.
- Consistency: departures from how the surrounding code, CLAUDE.md,
  REVIEW.md and the linter config do the same thing.

## Rules
- Only lines this PR added or changed. Leave older code alone.
- Match this codebase, not your own taste. Read nearby files first. If the
  repository consistently does something one way, do not push another.
- Skip anything that a formatter or linter run by CI already enforces.
- Every suggestion shows the better version, and the better version
  behaves the same. If it would not, say exactly how it differs.
- One suggestion per habit. If the same thing happens in several places,
  report it once with every location.
- Put efficiency and reuse before idiom, simplicity and clarity.
- Explain why in a sentence or two that a junior engineer would learn from.
  Be specific and kind, not preachy.
- If the code is already good, say so.

## Output
One block per suggestion:
### <short title>
- Location: file:line, and every other instance
- Category: efficiency, reuse, idiom, simplicity, clarity or consistency
- Why: one or two sentences
- Before: the current code, a few lines at most
- After: the suggested code
- Behaviour: same, or how it differs
```

Security hunter prompt:

```
You are a security reviewer. Find vulnerabilities that this pull request
introduces or exposes. Each needs a concrete path from an untrusted input
or party to the harm. Do not report hardening ideas, best-practice gaps
without an exploit path, or general code quality. Other agents will try to
disprove every finding you report.

{the required block, scope and already-reported list above}

Look for: injection (SQL, shell, template, path traversal); missing or wrong
authentication and authorisation checks; secrets or tokens in code, config
or logs; unsafe deserialisation; SSRF; open redirects; XSS; crypto misuse;
sensitive data written to logs or sent to third parties; new dependencies
from unexpected sources; and CI workflow changes such as pull_request_target
with a checkout of PR code, untrusted input like titles or branch names in
run steps, widened token permissions, and unpinned third-party actions.

Every finding needs a file:line you read and the concrete attacker and
input. If you find nothing, say so.

## Output
One block per finding:
### <short title>
- Location: file:line, and every other instance
- Class: the vulnerability class
- Trigger: who the attacker is, the input, and the path it takes
- Impact: what they gain
- Pre-existing: yes or no
- Fix: one or two sentences
```

Risk classifier prompt. It always reviews the canonical diff, whatever the round:

```
Decide how much human review this pull request needs. Judge the change,
not the author. The author's association only limits how low the tier can
go.

{the required block above}

Author: {author}, association {author_association}{", a bot" if author_is_bot}
CODEOWNERS: {its local_path, or "none"}
PR template: {its local_path, or "none"}
Paths REVIEW.md says always need a person: {list, or "none"}

## Tiers
- Low, 0 approvals: docs, copy, comments, tests only, or code that cannot
  run unless a feature flag that is off by default is turned on.
- Medium, 1 approval: ordinary product changes, and sensitive areas such as
  caching, update mechanisms, prompts, config, build and packaging.
- High, 2 approvals: billing and payments, authentication and permissions,
  security-sensitive code, data migrations or deletion, release and update
  machinery, public API or schema changes, and architecture that sets a
  pattern others will copy.

## Rules
- Some paths always need a person, so the tier is at least Medium: CI
  workflow and pipeline files (.github/workflows/, .gitlab-ci.yml,
  Jenkinsfile, .circleci/ and similar), CODEOWNERS, repository settings
  files, and the paths REVIEW.md lists.
- Only an OWNER, MEMBER or COLLABORATOR can get Low. Anyone else, and any
  bot, gets at least Medium.
{when junior is true: "- The author is a junior engineer, so the tier is at least Medium."}
- Ask separately whether this change needs a person even though its tier
  says it does not, e.g. it is hard to revert, it touches shared
  infrastructure, or it makes a product decision.
- Decide whether a plain revert undoes the change. Data migrations, writes
  to persistent stores, published packages and calls to external services
  often cannot be undone that way.
- Check that the PR description says how the change was tested, what its
  risk is and how to revert it, plus any other section the PR template
  asks for.
- Use CODEOWNERS to name who must approve each changed path. The last
  matching pattern wins.

## Output
### Tier
Low, Medium or High, with its approval count, and one or two sentences why.
### Always-a-person paths
The ones the PR touches, or "none".
### Needs a person anyway
Yes or no, and why.
### Required reviewers
Each owner and the changed paths they own.
### Reversibility
Clean revert, or Needs care and why.
### PR description
For Testing, Risk, How to revert and any other template section: present,
thin or missing.
### Where a human should look
Up to five file:line ranges, each with what to check there. Leave out
anything routine.
### Decisions for a human
Questions the code cannot answer, such as whether this should be built this
way, or patterns it sets.
```

Prior-findings checker prompt:

```
Earlier rounds of this review reported findings that were still open. Check
whether each one is fixed at the head commit.

{the required block above}

Open findings from earlier rounds: {open_prior_findings_path}. Each has an
id, tag, file, line, title and class.
Files changed since the previous round: {files_changed_since_previous_round}

For each finding, find the code it refers to at {head_commit}. The line may
have moved. Check every instance of its class, not only the first.

## Output
One line per finding: id | Fixed, Still present or Gone (the code was
removed) | file:line at head | your evidence in one sentence.
```

## 4. Verify

Collect the candidates from the bug and security hunters, and from the craft hunter in junior mode. Mark the craft hunter's suggestions as suggestions; a bug it reports is an ordinary candidate. Merge candidates that describe the same defect, or the same class, into one candidate with every location. Before voting, drop:
- candidates on an excluded or skipped path
- candidates matching an open prior finding, since the checker handles those
- candidates that a review thread in the PR discussion already raised. Keep the thread's URL for the summary.

Number the rest C1, C2 and so on. If none are left, go to step 5.

Launch the verifiers for the depth in a single message, all `general-purpose`, each with every candidate. If there are more than 12 candidates, split them into groups of up to 12 and give each group its own verifiers. Verifiers must not see each other's votes.

Verifier prompt:

```
You are checking candidate findings from a code review. Try to disprove
each one. A finding is real only if you can confirm it yourself from the
code.

{the required block above}

## Candidates
{each candidate: id, title, every location, class, trigger, impact,
pre-existing, and whether it came from the bug or the security hunter.
For a suggestion: id, title, every location, category, why, before, after
and behaviour.}

For each candidate:
1. Read the code at every location at {head_commit}. Read enough around it
   to see the guards, callers and tests that could make it a non-issue.
2. Follow the trigger through the code. If an earlier check, the types, the
   only callers or the configuration rule it out, it is not real.
3. Style, naming, refactoring, cleanup, diagnostics or logging preferences,
   missing tests on their own, and anything argued from a name rather than
   from the code are also not real.
4. If it is real, tag it:
   - important: wrong behaviour, a crash, data loss or corruption, a broken
     caller, build or test, or the PR not doing what it says, in realistic
     use.
   - security: a real exploit path from an untrusted input to harm.
   - nit: real but minor. Cheap to fix, and little harm if left.
   - pre-existing: real, but not in lines this PR added or changed, and the
     PR does not make it newly reachable or worse.
5. Check the locations. Drop any that do not have the defect. Add any other
   instance of the same class that you find.

{when junior is true:
"Candidates marked as suggestions are judged differently, and step 3 does
not apply to them. A suggestion is real only if all of these hold:
- the code it targets is in lines this PR added or changed
- the suggested version behaves the same, or the difference is spelled out
  and harmless
- it is clearly better: faster at realistic sizes, reuses something that
  already exists, or is shorter and clearer. A different taste is not
  better.
- it fits how this repository already writes the same thing
- no formatter or linter run by CI already covers it
Tag a real suggestion `suggestion`. If the inefficiency would cause a real
problem at realistic sizes, tag it `important` instead. Check its locations
as in step 5."}

## Output
One line per candidate:
C<n> | REAL or NOT REAL | tag if real | file:line evidence | one sentence why
```

Keep a candidate if it gets at least the depth's number of REAL votes. Its tag is the most common tag among the REAL votes. On a tie, take the more severe tag, in the order security, important, nit, suggestion, pre-existing. Keep the locations that most of the REAL votes confirm.

Apply the nit cap, and in junior mode the suggestion cap. Keep the ones with the most REAL votes, putting efficiency and reuse suggestions first when votes tie, and count the ones left out.

If the Jira checker ran, check each Missing and Contradictions item against the review diff and the code at the head commit yourself. Drop the ones that are wrong.

## 5. Decide the gate

These block the merge:
- Important and Security findings, both new ones and ones the checker found still present
- open review threads on the PR (`unresolved_review_thread_count`). Every finding is either fixed or answered with a reason before merge.
- failing or cancelled CI checks (`ci_checks.not_passing`)

These do not block, but each needs a fix or a one-line reason: nits, suggestions, pre-existing findings, and threads the author resolved without replying (`resolved_by_author_without_reply_count`). For the last, suggest running `/review-pr` to check whether those threads were really dealt with.

The verdict:
- Blocked: anything blocks.
- Waiting on CI: nothing blocks, but checks are pending, or `ci_checks` has an `error` so their state is unknown. If no checks ran at all, say so.
- Ready for N approvals: nothing blocks and CI passes. N comes from the tier, and is at least 1 when the classifier says the change needs a person anyway. At Low with N = 0, say no human approval is required by the tier.

If `round` is greater than 3 and anything still blocks, open the summary by saying that the PR has been through {round} rounds without converging, and that the author should step back and rethink the approach before another round of fixes. Name the bug classes that keep coming back.

## 6. Present

Open with the count. If nothing needs fixing, say so plainly in the heading.

```
## {i + s} to fix ({i} important, {s} security) · {n} nits · {p} pre-existing
or
## Nothing needs fixing
In junior mode, add " · {g} suggestions" to either heading.

**Gate:** Blocked | Waiting on CI | Ready for {N} approvals. One line per reason.
**Risk:** {tier}, {approvals}. Why, in one sentence. Required reviewers.
**Round {round}**, {full review | only files changed since round {previous}}. {x} earlier findings fixed, {y} still open.
```

Then these sections, leaving out empty ones:
- **Important** and **Security**: each `file:line`, its title, then the trigger and impact in one sentence, the fix, `votes k/V`, and other instances of its class.
- **Still open from earlier rounds**: Important and Security findings in full. Give only counts for the rest.
- **Nits**: shown as "{shown} of {total}".
- **Suggestions**, in junior mode: shown as "{shown} of {total}", efficiency and reuse first. Each gets its `file:line` and other locations, a title, why it is better, a short before and after snippet, and `votes k/V`. Write them so the user can pass them straight on to the author.
- **Patterns to work on**, in junior mode: up to three habits that recur across the suggestions, each in one sentence. Leave this out if nothing recurs.
- **Pre-existing**: marked as not blocking.
- **Already raised on the PR**: thread URLs of candidates that were dropped as repeats.
- **Jira**: the ticket line and verdict, plus verified Missing, Beyond the ticket and Contradictions items. Scope is a person's call, so it does not block the gate. If the checker replied JIRA UNAVAILABLE, or was skipped, say why.
- **Where a human should look** and **Decisions for a human**: from the classifier.
- **PR description**: the sections that are missing or thin.
- **Not reviewed**: excluded files, REVIEW.md skips, and whether agents could not search the whole repository.

End with one line: how many candidates the verifiers dropped out of how many, and the depth with its agent counts.

## 7. Record the round

Write the findings to `findings.json` next to `round_context_path`:

```json
{
  "depth": "standard",
  "junior": false,
  "open": [
    {"id": "R2-1", "tag": "important", "file": "src/a.py", "line": 42, "title": "Retry loop never stops", "class": "unbounded retry"}
  ],
  "fixed": [
    {"id": "R1-3", "tag": "nit", "file": "src/b.py", "line": 7, "title": "Wrong log level", "class": "log level"}
  ]
}
```

`open` holds every finding reported this round, of every tag including suggestions, plus earlier findings that are still present. At an unchanged head, earlier findings stay in `open` as they were. `fixed` holds earlier findings that the checker found fixed or gone. New findings get ids `R{round}-1`, `R{round}-2` and so on. Earlier findings keep their ids. Then run:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/record_round.py" "<round_context_path>" "<findings.json>"
```

Tell the user that the next run on this PR builds on this round, and that deleting `state_dir` starts over at round 1.
