# Sheriff

**Agents code. The Sheriff decides what merges.**

Sheriff is a GitHub-native rule layer for teams where AI agents (any harness: Claude Code, Codex, LobeHub, a
person) open pull requests. It works the same whatever runs the agents, because everything it does lives in GitHub:

- **Independent review, cheap first.** Each new head of a pull request into the default branch gets a cheap
  first-pass review (your pick: an OpenCode model on your OpenCode key, or a cheaper Claude model). Only when it is
  clean does the final reviewer (Claude Opus by default) look; docs-only changes stop at the cheap pass. After 3 cheap
  rounds on a pull request only the final reviewer runs, and after 3 final reviews a person decides (a `/sheriff
  review` comment allows one more). Reviewers run in CI with read-only tools, no shell, and the project's rules from
  the default branch; a commit is never reviewed twice against the same base.
- **Every round counts, finished or not.** Before any model runs, the gate App reserves the round with a pull request
  review on the head; the verdict later replaces that review in place. A round that is cancelled, times out, crashes or
  ends without a readable verdict still counts against the budget, and so does a reserved round whose head moved on: a
  head pushed seconds after a reservation costs that round. When the budget is used, or an earlier Sheriff review was
  edited by anyone but the App, the blocked verdict is published at once and no reviewer runs.
- **The verdict is the gate App's `Review` check run.** Only that App can create or change it, it is bound to the
  head and its merge base, and it records which model ran and how many tokens it used.
- **`Merge gate` status.** Green only when every comment from a person has been answered by an agent (a bot, or a
  login in `SHERIFF_ACK_AUTHORS`) and, if configured, production serves the default branch, is healthy, and its last
  deploy succeeded.
- **One ruleset, no bypass.** GitHub refuses any merge, an admin's included, without `Review` and `Merge gate` from
  the gate App. Auto-merge merges a pull request the moment it is green, so agents never need an admin login.
- **Two Apps per account.** A worker App for agents (code, pull requests, issues; never statuses or checks) and a gate
  App whose key lives only in each repository's `sheriff` environment, restricted to the default branch.
- **Usage you can see.** `gh sheriff stats` shows how much of the review work the final (smart) model did, with and
  without cache reads, and warns above a target (10% by default).

Status: in development. `gh sheriff install` sets a repository up (an organisation or a personal account);
`gh sheriff models`, `gh sheriff stats` and `gh sheriff doctor` manage it, and `gh sheriff task` records the work.

## How a repository uses it

Two small files in `.github/workflows/` route events to Sheriff's shared workflows at `@v1`: `sheriff.yml` and
`sheriff-relay.yml` (see `templates/`). Fixes released under `v1` reach every repository with no pull request.

Settings (repository variables, all optional): `SHERIFF_FIRST_PASS` (cheap reviewer as `provider:model`, for example
`opencode:opencode-go/deepseek-v4.1-flash` or `claude:claude-haiku-5-5`; empty for none), `SHERIFF_REVIEW_MODEL` (final
reviewer, default `claude:claude-opus-5-5`), `SHERIFF_FIRST_PASS_ROUNDS` and `SHERIFF_REVIEW_ROUNDS` (default 3 each),
`SHERIFF_ACK_AUTHORS`, `SHERIFF_VERSION_URL`, `SHERIFF_HEALTH_URL`, `SHERIFF_DEPLOY_WORKFLOW`, `SHERIFF_OVERRIDE` (a pull
request number that skips the production part once), `SHERIFF_SMART_SHARE_LIMIT`, `SHERIFF_RUNS_ON` (keep the default
GitHub-hosted runners: never point it at a persistent runner that pull request workflows also use).

Two environments, both limited to the default branch: `sheriff` holds the gate App (`SHERIFF_APP_ID`,
`SHERIFF_APP_KEY_B64`); `sheriff-review` holds only the keys your reviewers need: `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`; Claude reviews count against your Claude plan) and/or `OPENCODE_API_KEY`. Repository variables
`SHERIFF_APP_ID` and `SHERIFF_APP_SLUG` name the gate App; `SHERIFF_UNLOCK_AUTHORS` lists logins, besides
maintainers and admins, whose `/sheriff review` allows one more final review (never the pull request's author; an
edited comment never counts).

## Work state

Each piece of work is one issue whose title starts with its task id, `GATE-STATUSES: post the gate statuses`. A pull
request names its task by ending its title with `(GATE-STATUSES)` or by a branch whose last part is the id
(`ai/GATE-STATUSES`). Three issue fields, defined once for the account, hold the state:

| Field | Values | Set by |
|---|---|---|
| `Stage` | Candidate, In progress, Merged, Released, Verified, Parked | Sheriff, from facts; people and agents set Parked or Candidate and record Verified with `task verify` |
| `Verify by` | Release only, Live check, Awaits event | whoever opens the work |
| `Next action` | one line: who acts next and what | whoever touched the work last |

The `state` job of `sheriff.yml` moves `Stage` on the schedule, after pull request activity and after each deploy that
dispatches `sheriff.yml`, with the job token alone (no App key, no environment):

| Fact | Stage |
|---|---|
| An open pull request from this repository names the task | In progress |
| Its latest merge into the default branch | Merged |
| That merge is released | Released |
| Released, and `Verify by` is Release only | Verified, with a comment, and the issue closes |

Released means production serves a commit that contains the merge (`SHERIFF_VERSION_URL` answers JSON with `sha`); without
a version URL, that the newest run of `SHERIFF_DEPLOY_WORKFLOW` on a commit containing the merge succeeded; with neither,
the merge itself. A stage never moves backwards by itself (a follow-up pull request or a rollback leaves it), an open pull
request holds work at In progress until it merges, Parked work stays parked until a pull request opens, a merge older than
the issue counts only if the issue already says Merged or Released, and Decision issues never move. Issues closed as not
planned are left alone; issues closed as completed (for example by `Fixes #12`) keep moving. Pull requests from forks
never mark work In progress. Runs are idempotent and print nothing when nothing changes.

Commands (your `gh` login, or `GH_TOKEN`; a refused command prints `{"error": ...}` and exits 1):

| Command | Use |
|---|---|
| `gh sheriff task new "<title>" [--id ID] [--type Task\|Bug\|Feature\|Decision] [--verify V] [--body-file F]` | Opens the issue at Candidate, `Verify by` Live check unless given; the id is made from the title unless given. A Decision uses the Decision issue type, or the `decision` label where the account has no such type |
| `gh sheriff task set <ID\|#n> [--stage S] [--next "text"] [--verify V]` | What facts cannot show. Verified is refused (use `verify`), and so are In progress, Merged and Released while a pull request is open. `--next ""` clears |
| `gh sheriff task verify <ID\|#n> --evidence-file F` | Released work only: posts the evidence (what was exercised in production, what was observed), sets Verified, closes the issue |
| `gh sheriff task status [--json]` | Open work by stage, decisions, blocked work, and issues where a person commented last |
| `gh sheriff task reply <ID\|#n> --body-file F` | Answers a person's comment; the reply ends with the agent marker, so the issue leaves "a person commented last" |
| `gh sheriff task sync [--apply]` | What the job runs; without `--apply` it only reports |

`gh sheriff install` creates the three issue fields for the account (an organisation needs `gh auth refresh -s admin:org`,
a personal account `gh auth refresh -s user`) and a project board, `--project-name` (default "Main project", needs
`-s project`), with the fields and the views Needs you, Blocked, Ready, In flight, Awaiting verification and All open by
stage. Switch on the board's "Auto-add to project" workflow once in the browser (GitHub has no API for it); install prints
the link. `--no-work-state` skips this step, and `gh sheriff doctor` checks the fields and the board.

## Acknowledging a comment

A comment from a person holds `Merge gate` until an agent (a bot, or a login in `SHERIFF_ACK_AUTHORS`) answers it with
a comment whose last line is:

    <!-- sheriff-ack -->

`gh sheriff task reply` adds it for you.

## Limits

The review budget counts rounds per pull request, so a new pull request from the same branch starts a new budget. One gate App serves every repository of an account, and any repository's copy of its key can mint tokens for the
others: in an organisation where different people administer different repositories, an admin of one could forge the
statuses of another. Give such repositories separate gate Apps. A writer can still delete a person's comment, which removes its hold on `Merge gate`. An AI reviewer reads text written by the pull request's author; it is told to treat it as data, but a crafted change
could still try to talk it into a clean verdict. Agents that run as your own operating-system user can read whatever
that user can, including an admin `gh` login; Sheriff closes the sanctioned paths, and full isolation needs agents
that run as another user or in a sandbox.

## License

MIT, see [LICENSE](LICENSE).
