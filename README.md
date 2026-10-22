# claude-skills

Personal [Claude Code](https://claude.com/claude-code) skills, packaged as a plugin.

## Install

```
/plugin marketplace add tomqdev/claude-skills
/plugin install tomqdev@tomqdev
```

Update with `/plugin marketplace update tomqdev`.

## Skills

### review-pr

```
/review-pr https://github.com/<owner>/<repo>/pull/<number>
```

Reviews a GitHub pull request locally with several review agents in parallel. It never posts to GitHub.

- Fetches the PR diff once and checks its line counts against the PR metadata. Every agent reviews that one diff, so stacked PRs are reviewed against their real base, not `main`.
- Leaves Unity editor-serialized files (`.prefab`, `.unity`, `.asset`, `.meta` and similar) out of the review diff. Add more with `--exclude GLOB`, or keep them with `--keep-editor-serialized`.
- Runs `gh` with `GH_TOKEN` unset, so `gh` uses its stored login from `gh auth login`.
- Uses GitHub's REST API, so it also works in Claude Code cloud sessions, where GraphQL is blocked. Only whether review threads are resolved, which REST lacks, comes from the session's `ccr/review_threads` route, or from GraphQL elsewhere.
- Checks whether earlier review comments were dealt with, including threads marked resolved whose code never changed, and threads the PR author resolved without replying.
- Finds Jira ticket keys such as `ERD-1234` in the branch name, PR title or Jira links, and checks the PR against the ticket, its comments and linked issues. Needs the Atlassian plugin, logged in via `/mcp`.
- Checks each serious finding against the code before reporting it.

Requires `gh`, `git`, Python 3.8+, and these plugins: `pr-review-toolkit`, `superpowers` and `atlassian` from the official marketplace, and `ponytail` from the `DietrichGebert/ponytail` marketplace.

### layered-review

```
/layered-review https://github.com/<owner>/<repo>/pull/<number> [--depth quick|standard|deep] [--junior] [--full]
```

A merge-gate review based on [Solving code review at Anthropic](https://claude.dev/blog/solving-code-review-at-anthropic/). It reports only verified bugs and says how much human review the PR needs. It never posts to GitHub. Use `review-pr` for the broader style, simplification and comment review.

- Bug hunters with different lenses (intent, breakage, line-level, and at `deep` also state and tests) and a separate security hunter review the PR in parallel against the whole repository.
- Separate verifiers try to disprove every candidate and vote on it. Only candidates with enough votes are reported, tagged Important, Security, Nit or Pre-existing. Style and cleanup are dropped.
- Depth sets the number of hunters and verifiers: `quick` 1 and 1, `standard` 3 and 3, `deep` 5 and 5.
- `--junior` is for code from junior engineers. A craft hunter looks at the lines the PR changed for inefficient code, hand-rolled versions of existing helpers, unidiomatic or tangled code, misleading names, and departures from the repository's own conventions. Verifiers keep a suggestion only if it behaves the same, is clearly better rather than a matter of taste, and fits how the repository already does things. Each suggestion shows a before and after, ready to pass on, and the review lists habits that recur. Up to 10 suggestions are shown. They don't block the merge, but the risk tier is at least Medium.
- A risk classifier sets a tier from the change: Low needs 0 human approvals, Medium 1 and High 2. CI workflows, CODEOWNERS and paths `REVIEW.md` marks always need a person, and only owners, members and collaborators can get Low. It names required reviewers from `CODEOWNERS`, checks the PR description for Testing, Risk and How to revert, and points a human at the few places worth reading.
- The gate is Blocked, Waiting on CI or Ready for N approvals. It is blocked by Important and Security findings, open review threads and failing CI.
- Reads `CLAUDE.md`, `REVIEW.md`, `CODEOWNERS` and the PR template from the base branch, so a PR can't rewrite the rules it is reviewed against. `REVIEW.md` can say what failure looks like, which paths to skip, a nit cap, which paths always need a person, and the depth.
- Remembers each round in `~/.local/state/tomqdev-layered-review/` (or `$XDG_STATE_HOME`). Later runs check earlier findings instead of reporting them again. From round 3 on, they review only the files that changed since the previous round; `--full` reviews the whole diff instead. After 3 rounds that still have blockers, the summary says to rethink the approach. Delete the PR's state directory to start over.
- In round 1, checks the PR against its Jira ticket when the Atlassian plugin is logged in, using review-pr's Jira check.
- If the current directory is a clone of the PR's repository, fetches the head commit so agents can search it with `git grep`.
- Works in Claude Code cloud sessions, like review-pr. CI checks come from the REST check runs and commit status APIs.

Requires `gh`, `git`, Python 3.8+ and the `review-pr` skill from this plugin, whose fetch script it reuses. The Jira check needs the `atlassian` plugin.

### resume-crashed-sessions

```
/resume-crashed-sessions
```

After Claude Code crashes or is killed, lists the most recent sessions across all projects, newest first. Each one shows its working directory, a `claude --resume` command and the last thing you said. Sessions started within a few minutes of each other are the ones that were open at crash time.
