# Changelog

All notable changes to Sheriff are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Sheriff uses [Semantic Versioning](https://semver.org/). While Sheriff is in beta, a breaking change can still ship
within `v1`; its entry says so under **Breaking** with the steps to take. [RELEASING.md](RELEASING.md) describes how a
release is made.

## [Unreleased]

## [1.3.0] - 2026-11-25

### Added

- **`gh sheriff update`** brings an existing install to the current release: it opens the trigger-files pull request only
  when the templates changed, rewrites the `Sheriff` ruleset only when it differs, and, while work state is on, adds
  missing issue fields, board columns, views and open task issues and fills in variables a release needs. It never
  touches secrets, reviewer models, the plan, merge or updates modes, the board's own Status options or views, and
  never turns a feature on or off. `--work-state on [--project-name "<board>"]` turns work state on with a fresh
  `SHERIFF_WORK_STATE_SINCE`; `--work-state off` turns it off and keeps its data.
- **`--dry-run`** on `install` and `update` writes nothing (any write is refused) and prints every change: variables
  with old and new values, secret names, environments, the ruleset, the trigger-files pull request, issue fields, the
  board (created or reused, columns, views, Status options, items), and, when work state would be on, a preview of the
  first sync: how many tasks it would move, reopen and close.
- **Adopting work state is safe for existing history.** `install` and `update --work-state on` set `SHERIFF_WORK_STATE_SINCE`
  when they turn work state on, and the evidence audit (Verified without recorded evidence moves back and the issue reopens) covers only open
  issues and issues closed after it. `gh sheriff doctor` and a dry-run `gh sheriff task sync` say how many tasks the
  next sync would set back.
- `SHERIFF_PROJECT` names the work board. `install --project-name` saves it, and `task new`, `task board-status`,
  `doctor` and the Status mirror use it instead of `<repo> work`.
- `gh sheriff task ask <ID> --question "..." [--to <login>]` asks a person: it labels the issue `sheriff:needs-person`,
  @mentions and assigns them (default `SHERIFF_DECIDERS`), and the task leaves `ready` until `task ask <ID> --clear`.
- `gh sheriff task show <ID>`, `gh sheriff task drop <ID> --reason "..."` (closes as not planned and releases the claim),
  and `task new --blocked-by <ID> --blocks <ID>`.
- Items in `status --json` carry `priority` (from `priority:P*` labels), `updated`, `closed`, `asked`, `assignees` and
  each blocker's `title`. `ready` and `needs_you` are ordered by priority, then age; `ready` also leaves out tasks with an
  open pull request or an open question, and `needs_you` also lists issues labelled `sheriff:needs-decision` or
  `sheriff:needs-person`.
- The sync makes an open task issue without a Stage a Candidate (Verify by "Live check" if empty), and returns In
  progress work whose pull requests were all closed without merging to Candidate, releasing its claim. The state job
  also runs when an issue is opened or retitled.
- `task verify` finds the task's merge by searching for its ID, not only among the 200 most recent merged pull requests,
  and verifies work that no pull request names (its evidence comment carries `sha=none`).
- `SHERIFF_HEALTH_URL` also gates Released: no task becomes Released while it does not return 200. Outside CI, `task`
  commands, `doctor` and dry runs read `SHERIFF_VERSION_URL`, `SHERIFF_DEPLOY_WORKFLOW` and `SHERIFF_HEALTH_URL` from the
  repository variables.

### Changed

- Without `SHERIFF_WORK_STATE_SINCE`, closed issues are no longer audited for Verified evidence; open issues still are.
- Re-running `install` on an existing install no longer turns work state on; `gh sheriff update --work-state on` does.
- `install` rewrites the `Sheriff` ruleset only when it differs from the current rules.
- `gh sheriff doctor` and the work-state messages recommend `gh sheriff update` for what an existing install lacks.

### Upgrading

- Run `gh extension upgrade sheriff`, then `gh sheriff update` (add `--dry-run` first to see every change). It opens a
  pull request with the new `sheriff.yml`, whose state job runs when an issue is opened or retitled and passes
  `SHERIFF_HEALTH_URL`, `SHERIFF_PROJECT` and `SHERIFF_WORK_STATE_SINCE` to the sync, and on a repository whose work
  state is already on it sets `SHERIFF_WORK_STATE_SINCE`, so closed issues are audited again from then on. Until then
  the old file keeps working without those.

## [1.2.0] - 2026-11-25

### Added

- **Claims.** `gh sheriff task claim <ID>` records who works on a task in a new `Claimed by` issue field before any
  branch or pull request exists, so two agents no longer start the same task. It is refused when someone else holds
  the task and changes nothing for the holder; `gh sheriff task release <ID>` clears it (`--force` clears someone
  else's). The claimant is `--by`, else `SHERIFF_AGENT`, else the gh login. Claims don't expire and stay after the
  merge as a record of who did the work ([Picking work](README.md#picking-work)).
- `gh sheriff task status` lists **Ready** tasks: Candidates that are not blocked, not claimed and not labelled
  `sheriff:needs-decision`. With `--json` they are `ready`, and every item carries `claimed_by`, so a dispatcher can
  tell its own work from someone else's.

### Upgrading

- Run `gh sheriff install` to add the `Claimed by` issue field and its board column; `gh sheriff doctor` reports it
  missing until then. Without it `task claim` and `task release` stop and say so; everything else works as before.
- No new permission: a claim is an issue field, written with Issues: Read and write.

## [1.1.0] - 2026-11-25

### Added

- **Plans ask before work starts.** Every plan has a new second section, **Decision needed**: `None.`, or the question a
  person must answer before anyone builds, with one line per option and the planner's recommendation. The planner asks
  when the issue wants approval or a choice first, or when only someone responsible for the project can decide.
- When a plan asks, Sheriff labels the issue `sheriff:needs-decision` and posts a comment that @mentions the issue's
  author, or the logins in the new `SHERIFF_DECIDERS` variable, with the question and how to answer. Your agents can
  skip issues with that label.
- **A reply is the answer.** A reply from an admin, a maintainer or a login in `SHERIFF_DECIDERS` on a labelled issue
  writes the next plan from that answer; once no question is left, Sheriff removes the label. This needs the 1.1.0
  workflow file (see Upgrading); until then, comment `/sheriff replan <your answer>`.
- Once a newer plan settles a question, the earlier question comment is marked **Answered** (with who answered) or
  **Replaced**, and the superseded plan says its question is closed, so no old comment still reads as an open decision.
- **The planner sees pictures.** Images attached to the issue or to a trusted comment (GitHub attachments only, at most
  10, 5 MB each, PNG, JPEG, GIF or WebP) are downloaded for the planner, which looks at them. Pictures it could not
  read are reported in the plan.
- The review blocks a pull request that implements a plan whose question is still open.

### Changed

- Plans have eight sections. A plan without **Decision needed** fails the format check; the planner writes it.
- Plans and reviews never guess a person's gender, and plans @mention no one.

### Upgrading

- `@v1` repositories get everything at once except reply-triggered plans: run `gh sheriff install` to update
  `.github/workflows/sheriff.yml`. Pinned repositories get a Dependabot pull request; merge it, then run `install`.
- No new App permission: labels and comments use Issues: Read and write, which planning already needs.

## [1.0.2] - 2026-11-23

### Fixed

- After a pull request used its review budget, a maintainer's `/sheriff review` could not take effect on the same
  commit: the budget notice counted as a review of that commit, so the next run said "already reviewed". The notice no
  longer counts; the allowed review runs on the next push, or at once with
  `gh workflow run sheriff.yml -f pr=<n> -f review=true`. A commit already stopped by the budget gets no duplicate
  notices.

## [1.0.1] - 2026-11-23

### Fixed

- `gh sheriff install` on a personal account no longer stops at "You must authenticate with an access token
  authorized to a GitHub App": a gh login cannot list a personal account's App installations, so the first Sheriff run
  checks that the App can reach the repository.
- `--no-files` with `--updates pinned` stops when `.github/dependabot.yml` is missing instead of opening a pull
  request, and `install` says when it moves a pin.

## [1.0.0] - 2026-11-23

First versioned release. Sheriff is in beta: behaviour, settings and messages can still change.

### Added

- Versions: each release is an immutable `vX.Y.Z` tag with release notes; `v1` follows the latest `v1.x`.
- `gh sheriff install --updates auto|pinned`. `pinned` runs one exact release and adds `.github/dependabot.yml` for
  GitHub Actions, so newer releases arrive as pull requests. `auto` (the default) keeps following `v1`.
- `gh sheriff doctor` reports your CLI version, the update mode, the release your workflow runs and the latest release.
- Planning: a plain issue a trusted person opens is named (`WORDS-<number>: title`) and planned within a minute.
- Priorities: every plan starts with a Priority section (P0 to P3) and Sheriff labels the issue `priority:Pn`; a label
  a person set is never changed.
- Plans and reviews are written in plain language.

[Unreleased]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/nick-the-dev/gh-sheriff/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/nick-the-dev/gh-sheriff/releases/tag/v1.0.0
