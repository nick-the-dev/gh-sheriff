#!/usr/bin/env python3
"""Sheriff review: reserve a round as the gate App, prepare a pull request for the reviewer, read its verdict, and
publish it as the gate App.

The verdict that counts is the App's `Review` check run on the head (only that App can change it); the pull request
review that reserves the round and then carries the verdict is for people and agent tools to read."""
import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(CLEAN|BLOCKED)\s*$")
DIFF_LIMIT = 800_000
BODY_LIMIT = 8_000
COMMENT_LIMIT = 60_000
CHECK_NAME = "Review"
SHA = re.compile(r"[0-9a-f]{40}")
DEFAULT_FINAL = ("claude", "claude-opus-5-5")
MARKER = re.compile(r"<!--\s*sheriff-review\s+sha=([0-9a-f]{40})\s+base=([0-9a-f]{40})(?:\s+patch=[0-9a-f]{40})?\s+stage=(first|final|budget|reuse)\s+model=(\S+)\s+verdict=(clean|blocked)\s*-->")
START = re.compile(r"<!--\s*sheriff-start\s+sha=([0-9a-f]{40})\s+base=([0-9a-f]{40})\s+plan=(first|final)\s*-->")
UNLOCK = re.compile(r"^\s*/sheriff review\s*$", re.M)
DOCS_SUFFIXES = (".md", ".mdx", ".rst")
RULE_FILES = ("agents.md", "claude.md", "gemini.md", "copilot-instructions.md")
REVIEWS_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){
  author{login} reviews(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{author{login} body lastEditedAt editor{login}}}}}}"""
_SPEC = importlib.util.spec_from_file_location("sheriff_reviewers", pathlib.Path(__file__).with_name("reviewers.py"))
reviewers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reviewers)
_PLAN_SPEC = importlib.util.spec_from_file_location("sheriff_plan", pathlib.Path(__file__).with_name("plan.py"))
planner = importlib.util.module_from_spec(_PLAN_SPEC)
_PLAN_SPEC.loader.exec_module(planner)


class ReviewError(Exception):
    pass


def run(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ReviewError(" ".join(args[:3]) + " failed: " + (result.stderr or result.stdout).strip()[:300])
    return result.stdout


def gh_json(args):
    try:
        return json.loads(run(["gh"] + args))
    except ValueError as exc:
        raise ReviewError("gh returned invalid JSON") from exc


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(key + "=" + str(value) + "\n" for key, value in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    print(json.dumps({key: value for key, value in values.items() if key not in ("verdict", "stage")}), flush=True)


def bare(login):
    return str(login or "").removesuffix("[bot]")


def reviewed(head, key, slug):
    try:
        data = gh_json(["api", "repos/{owner}/{repo}/commits/" + head + "/check-runs?check_name=" + CHECK_NAME + "&per_page=100"])
    except ReviewError as exc:
        print("::warning title=Sheriff review::cannot read earlier Review check runs (" + str(exc)[:160] + "); reviewing anyway", flush=True)
        return False
    for check in (data or {}).get("check_runs", []):
        app = check.get("app") or {}
        if app.get("slug") == slug and check.get("status") == "completed" and check.get("external_id") == key:
            return True
    return False


def pr_plan(view, slug):
    issue = planner.task_issue(view, os.environ["GH_REPO"], gh_json)
    found = issue and planner.current_plan(issue["comments"], slug)
    return (issue["number"], found["version"]) if found else (0, 0)


def prepare(number, checkout, out, default_branch, head, base_sha, plan_issue=0, plan_version=0, app=""):
    if run(["git", "rev-parse", "HEAD"], cwd=checkout).strip() != head or \
            run(["git", "merge-base", "origin/" + default_branch, "HEAD"], cwd=checkout).strip() != base_sha:
        return output(skip="head moved", pr=number, head=head, base=base_sha)
    current = None
    if plan_issue:
        issue = planner.read_issue(plan_issue, gh_json)
        current = issue and planner.current_plan(issue["comments"], app)
        if not current or current["version"] != plan_version:
            return output(skip="plan moved", pr=number, head=head, base=base_sha)
    view = gh_json(["pr", "view", str(number), "--json", "title,body,url"])
    diff = run(["git", "diff", "--no-color", "--stat", "--patch", base_sha + "..HEAD"], cwd=checkout)
    out.mkdir(parents=True, exist_ok=True)
    truncated = len(diff) > DIFF_LIMIT
    if current:
        (out / "plan.md").write_text(current["body"])
    (out / "diff.patch").write_text(diff[:DIFF_LIMIT] + ("\n[diff truncated by Sheriff]\n" if truncated else ""))
    (out / "pr.json").write_text(json.dumps({"number": number, "url": view.get("url"), "base": default_branch, "base_sha": base_sha, "head": head,
                                             "title": str(view.get("title") or "")[:300],
                                             "body": str(view.get("body") or "")[:BODY_LIMIT]}, indent=2))
    return output(skip="", pr=number, head=head, base=base_sha)


def is_bot(row):
    user = row.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login") or "").endswith("[bot]")


def flat(pages):
    return [row for page in (pages or []) for row in (page if isinstance(page, list) else [page]) if isinstance(row, dict)]


def app_reviews(number, slug):
    owner, name = os.environ["GH_REPO"].split("/", 1)
    rows, cursor, author = [], None, ""
    while True:
        args = ["api", "graphql", "-f", "query=" + REVIEWS_QUERY, "-F", "owner=" + owner, "-F", "name=" + name, "-F", "number=" + str(number)]
        data = gh_json(args + (["-f", "cursor=" + cursor] if cursor else []))
        pull = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        author = ((pull.get("author") or {}).get("login")) or author
        page = pull.get("reviews") or {}
        rows += [node for node in page.get("nodes") or [] if bare((node.get("author") or {}).get("login")) == slug]
        if not (page.get("pageInfo") or {}).get("hasNextPage"):
            return author, rows
        cursor = page["pageInfo"]["endCursor"]


def unlocker(login, author, listed):
    if not login or login == author or login.endswith("[bot]"):
        return False
    if login.lower() in listed:
        return True
    try:
        role = gh_json(["api", "repos/{owner}/{repo}/collaborators/" + login + "/permission"]).get("role_name")
    except ReviewError:
        return False
    return role in ("admin", "maintain")


def history(number, slug, listed=()):
    author, reviews = app_reviews(number, slug)
    counts = {"first": 0, "final": 0, "unlocks": 0, "edited": False}
    for review in reviews:
        if review.get("lastEditedAt") and bare((review.get("editor") or {}).get("login")) != slug:
            counts["edited"] = True
            counts["final"] += 1
            continue
        lines = str(review.get("body") or "").rstrip().splitlines()
        match = (MARKER.fullmatch(lines[-1].strip()) or START.fullmatch(lines[-1].strip())) if lines else None
        if match and match.group(3) in ("first", "final"):
            counts[match.group(3)] += 1
    comments = flat(gh_json(["api", "--paginate", "--slurp", "repos/{owner}/{repo}/issues/" + str(number) + "/comments"]))
    counts["unlocks"] = sum(1 for row in comments if UNLOCK.search(str(row.get("body") or ""))
                            and row.get("created_at") and row.get("updated_at") == row.get("created_at")
                            and unlocker(str((row.get("user") or {}).get("login") or ""), author, listed))
    return counts


def docs_only(name_status):
    entries = [line.split("\t") for line in name_status.splitlines() if line.strip()]
    if not entries:
        return False
    for entry in entries:
        status, path = entry[0], entry[-1]
        lowered = path.lower()
        if status not in ("A", "M") or not lowered.endswith(DOCS_SUFFIXES) or lowered.rsplit("/", 1)[-1] in RULE_FILES:
            return False
        if lowered.startswith((".github/", ".sheriff/", ".claude/", ".cursor/")):
            return False
    return True


def plan(counts, first_spec, docs, first_rounds, final_rounds):
    first = first_spec is not None and counts["first"] < first_rounds
    final = counts["final"] < final_rounds + counts["unlocks"]
    return {"first": first, "final": final, "docs": docs}


def verdict_of(text):
    lines = text.rstrip().splitlines()
    match = VERDICT_LINE.match(lines[-1]) if lines else None
    if not match:
        raise ReviewError("the reviewer's last line is not 'VERDICT: CLEAN' or 'VERDICT: BLOCKED'")
    return match.group(1).lower(), "\n".join(lines[:-1]).strip()[:COMMENT_LIMIT]


def review_run(pr, head, base_sha, workdir, prompt, first_spec, final_spec, first_rounds, final_rounds, counts, docs, runner=None, plan_version=0):
    runner = runner or reviewers.run
    steps = plan(counts, first_spec, docs, first_rounds, final_rounds)
    record = {"pr": int(pr), "head": head, "base": base_sha, "usage": [], "docs_only": docs, "plan": plan_version}
    if counts.get("edited"):
        return dict(record, stage="budget", model="none", verdict="blocked",
                    body="An earlier Sheriff review on this pull request was edited, so the rounds already used cannot be counted. "
                         "A maintainer decides: comment `/sheriff review` to allow a fresh review, then push.")
    if steps["first"]:
        result = runner(first_spec, prompt, workdir)
        verdict, body = verdict_of(result["text"])
        record["usage"].append({"stage": "first", "provider": result["provider"], "model": result["model"], "usage": result["usage"]})
        if verdict == "blocked" or docs:
            return dict(record, stage="first", model=result["model"], verdict=verdict, body=body)
    if steps["final"]:
        result = runner(final_spec, prompt, workdir)
        verdict, body = verdict_of(result["text"])
        record["usage"].append({"stage": "final", "provider": result["provider"], "model": result["model"], "usage": result["usage"]})
        return dict(record, stage="final", model=result["model"], verdict=verdict, body=body)
    return dict(record, stage="budget", model="none", verdict="blocked",
                body="Review budget used: " + str(counts["first"]) + " cheap and " + str(counts["final"]) + " final reviews on this "
                     "pull request. A person decides now: comment `/sheriff review` to allow one more final review, then push.")


def reserve(args):
    login = os.environ.get("GH_ACTOR_LOGIN", "").strip()
    if not login:
        raise ReviewError("GH_ACTOR_LOGIN is not set: run review.py reserve through app-token.py as the gate App")
    slug, planning = bare(login), args.plan == "on"
    empty = {"head": "", "base": "", "review_id": "", "counts": "", "plan_issue": 0, "plan_version": 0, "app": slug}
    view = gh_json(["pr", "view", str(args.pr), "--json", "state,isDraft,baseRefName,headRefOid" + (",title,closingIssuesReferences" if planning else "")])
    if str(view.get("state") or "").upper() != "OPEN":
        return output(skip="not open", **empty)
    if view.get("isDraft"):
        return output(skip="draft", **empty)
    if not args.default_branch or view.get("baseRefName") != args.default_branch:
        return output(skip="not into the default branch", **empty)
    head = str(view.get("headRefOid") or "")
    if not SHA.fullmatch(head):
        raise ReviewError("the pull request head is malformed")
    compare = gh_json(["api", "repos/{owner}/{repo}/compare/" + args.default_branch + "..." + head]) or {}
    base_sha = str((compare.get("merge_base_commit") or {}).get("sha") or "")
    if not SHA.fullmatch(base_sha):
        raise ReviewError("the merge base is malformed")
    plan_issue, plan_version = 0, 0
    if planning:
        try:
            plan_issue, plan_version = pr_plan(view, slug)
        except planner.PlanError:
            known = planner.closing(view.get("closingIssuesReferences"), os.environ["GH_REPO"])
            return output(skip="could not read the plan on #" + str(known[0] if known else args.pr), **dict(empty, head=head, base=base_sha))
    empty = dict(empty, head=head, base=base_sha, plan_issue=plan_issue, plan_version=plan_version)
    if reviewed(head, planner.review_key(base_sha, plan_version), slug):
        return output(skip="already reviewed at this head and base", **empty)
    listed = tuple(name.strip().lower() for name in (args.unlock_authors or "").split(",") if name.strip())
    counts = history(args.pr, slug, listed)
    if counts["edited"] and counts["unlocks"]:
        counts["edited"] = False
    first_spec, final_spec = reviewers.parse_spec(args.first_pass), reviewers.parse_spec(args.final) or DEFAULT_FINAL
    steps = plan(counts, first_spec, False, args.first_rounds, args.final_rounds)
    packed = json.dumps(counts, separators=(",", ":"))
    if counts["edited"] or not (steps["first"] or steps["final"]):
        record = review_run(args.pr, head, base_sha, "", "", first_spec, final_spec, args.first_rounds, args.final_rounds, counts, False,
                            plan_version=plan_version)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "verdict.json"
            path.write_text(json.dumps(record))
            post(path, args.default_branch)
        return output(skip="decided", **dict(empty, counts=packed))
    stage = "first" if steps["first"] else "final"
    server, repo, run_id = (os.environ.get(name, "").strip() for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    link = " ([workflow run](" + server + "/" + repo + "/actions/runs/" + run_id + "))" if server and repo and run_id else ""
    body = ("Sheriff review in progress at head `" + head[:12] + "`" + link
            + "\n\n<!-- sheriff-start sha=" + head + " base=" + base_sha + " plan=" + stage + " -->")
    created = gh_json(["api", "-X", "POST", "repos/{owner}/{repo}/pulls/" + str(args.pr) + "/reviews",
                       "-f", "event=COMMENT", "-f", "commit_id=" + head, "-f", "body=" + body]) or {}
    review_id = str(created.get("id") or "")
    if not review_id.isdigit():
        raise ReviewError("GitHub returned no id for the reservation review")
    return output(skip="", **dict(empty, review_id=review_id, counts=packed))


def round_counts(text):
    try:
        counts = json.loads(text)
    except ValueError:
        counts = None
    if not isinstance(counts, dict) or not all(type(counts.get(key)) is int for key in ("first", "final", "unlocks")):
        raise ReviewError("the round counts from the reservation are malformed")
    return counts


def run_reviews(args):
    record = review_run(args.pr, args.head, args.base, args.workdir, pathlib.Path(args.prompt).read_text(),
                        reviewers.parse_spec(args.first_pass), reviewers.parse_spec(args.final) or DEFAULT_FINAL,
                        args.first_rounds, args.final_rounds, round_counts(args.counts),
                        docs_only(run(["git", "diff", "--name-status", "--no-renames", args.base + "..HEAD"], cwd=args.checkout)),
                        plan_version=args.plan_version)
    pathlib.Path(args.out).write_text(json.dumps(record))
    return output(verdict=record["verdict"], stage=record["stage"], pr=args.pr, head=args.head)


def comment(record):
    head = record["head"]
    return ("**Sheriff review** (" + record["stage"] + ", `" + record["model"] + "`, head `" + head[:12] + "`): **" + record["verdict"] + "**\n\n"
            + (record["body"] or "No findings.").replace("<!--", "&lt;!--")[:COMMENT_LIMIT] + "\n\n"
            + "<!-- sheriff-review sha=" + head + " base=" + record["base"]
            + " stage=" + record["stage"] + " model=" + record["model"]
            + " verdict=" + record["verdict"] + " -->")


def post(path, default_branch, review_id=""):
    record = json.loads(pathlib.Path(path).read_text())
    if not all(re.fullmatch(r"[0-9a-f]{40}", str(record.get(key) or "")) for key in ("head", "base")) or record.get("verdict") not in ("clean", "blocked"):
        raise ReviewError("the verdict file is malformed")
    if record.get("stage") not in ("first", "final", "budget"):
        raise ReviewError("the review stage is malformed")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", str(record.get("model") or "")):
        raise ReviewError("the model name is malformed")
    if review_id and not re.fullmatch(r"[0-9]{1,20}", str(review_id)):
        raise ReviewError("the review id is malformed")
    plan_version = record.get("plan", 0)
    if type(plan_version) is not int or plan_version < 0:
        raise ReviewError("the plan version is malformed")
    slug = bare(os.environ.get("GH_ACTOR_LOGIN", "").strip())
    if not slug:
        raise ReviewError("GH_ACTOR_LOGIN is not set: run review.py post through app-token.py as the gate App")
    reviews = "repos/{owner}/{repo}/pulls/" + str(record["pr"]) + "/reviews"
    if review_id:
        held = gh_json(["api", reviews + "/" + str(review_id)]) or {}
        lines = str(held.get("body") or "").rstrip().splitlines()
        marker = START.fullmatch(lines[-1].strip()) if lines else None
        if (bare(str((held.get("user") or {}).get("login") or "")) != slug or not marker
                or marker.group(1) != record["head"] or marker.group(2) != record["base"]):
            raise ReviewError("review " + str(review_id) + " is not this round's open reservation; nothing is posted")
    current = gh_json(["pr", "view", str(record["pr"]), "--json", "headRefOid,state,baseRefName"])
    moved = current.get("headRefOid") != record["head"]
    body = pathlib.Path(path).with_suffix(".md")
    body.write_text(("This review is of an earlier head; the pull request has moved on.\n\n" if moved else "") + comment(record))
    if review_id:
        run(["gh", "api", "-X", "PUT", reviews + "/" + str(review_id), "-F", "body=@" + str(body)])
    else:
        run(["gh", "api", "-X", "POST", reviews, "-f", "event=COMMENT", "-F", "body=@" + str(body)]
            + ([] if moved else ["-f", "commit_id=" + record["head"]]))
    if moved:
        return output(posted="false", reason="head moved; the round is recorded")
    key = planner.review_key(record["base"], plan_version)
    if reviewed(record["head"], key, slug):
        return output(posted="false", reason="this head and base already have a Review verdict")
    check = pathlib.Path(path).with_suffix(".check.json")
    check.write_text(json.dumps({
        "name": CHECK_NAME, "head_sha": record["head"], "status": "completed",
        "conclusion": "success" if record["verdict"] == "clean" else "failure", "external_id": key,
        "output": {"title": "Sheriff review: " + record["verdict"] + " (" + record["stage"] + ", " + record["model"] + ")",
                   "summary": (record["body"] or "No findings.")[:COMMENT_LIMIT],
                   "text": json.dumps({"sheriff_usage": 1, "stage": record["stage"], "usage": record.get("usage", [])})}}))
    run(["gh", "api", "-X", "POST", "repos/{owner}/{repo}/check-runs", "--input", str(check)])
    return output(posted="true", reason="")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="review.py")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("reserve")
    v.add_argument("--pr", type=int, required=True)
    v.add_argument("--default-branch", default=os.environ.get("SHERIFF_DEFAULT_BRANCH", ""))
    v.add_argument("--first-pass", default="")
    v.add_argument("--final", default="")
    v.add_argument("--first-rounds", type=int, default=3)
    v.add_argument("--final-rounds", type=int, default=3)
    v.add_argument("--unlock-authors", default="")
    v.add_argument("--plan", default="")
    p = sub.add_parser("prepare")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--checkout", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--default-branch", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--plan-issue", type=int, default=0)
    p.add_argument("--plan-version", type=int, default=0)
    p.add_argument("--app", default="")
    r = sub.add_parser("run")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--head", required=True)
    r.add_argument("--base", required=True)
    r.add_argument("--checkout", required=True)
    r.add_argument("--workdir", required=True)
    r.add_argument("--prompt", required=True)
    r.add_argument("--first-pass", default="")
    r.add_argument("--final", default="")
    r.add_argument("--first-rounds", type=int, default=3)
    r.add_argument("--final-rounds", type=int, default=3)
    r.add_argument("--out", required=True)
    r.add_argument("--counts", required=True)
    r.add_argument("--plan-version", type=int, default=0)
    s = sub.add_parser("post")
    s.add_argument("--verdict", required=True)
    s.add_argument("--default-branch", required=True)
    s.add_argument("--review-id", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "reserve":
            reserve(args)
        elif args.command == "prepare":
            prepare(args.pr, args.checkout, pathlib.Path(args.out), args.default_branch, args.head, args.base,
                    args.plan_issue, args.plan_version, args.app)
        elif args.command == "run":
            run_reviews(args)
        else:
            post(args.verdict, args.default_branch, args.review_id)
    except (ReviewError, reviewers.ReviewerError, planner.PlanError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
