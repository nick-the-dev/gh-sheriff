#!/usr/bin/env python3
"""Sheriff plan: the facts the planner shares, namely the comment markers, an issue read with its comments, the
App's current plan on a task issue, the task issue of a pull request, the pull requests linked to an issue and the
review keys that bind a review to one plan by its version and comment; who may ask for a plan, the budget, the
reservation comment that the gate App posts before any model runs, and the same decision rechecked after a post for a
command that arrived meanwhile; and writing the plan: the issue prepared as data, the read-only model run, and the post
that turns the reservation into the plan or a failure note and marks the old version superseded."""
import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ID = r"[A-Z][A-Z0-9-]+"
TASK_TITLE = re.compile(r"^(" + ID + r")(?: #\d+)?: ")
PR_TASK = re.compile(r"\((" + ID + r")\)$")
PLAN_MARKER = re.compile(r"<!--\s*sheriff-plan\s+v=(\d+)\s+base=([0-9a-f]{40})(?:\s+superseded-by=(\d+))?\s*-->")
START_MARKER = re.compile(r"<!--\s*sheriff-plan-start\s+v=(\d+)\s*-->")
FAILED_MARKER = re.compile(r"<!--\s*sheriff-plan-failed\s+v=(\d+)\s*-->")
BUDGET_MARKER = "<!-- sheriff-plan-budget -->"
PLAN_COMMAND = re.compile(r"^\s*/sheriff plan\s*$")
REPLAN_COMMAND = re.compile(r"^\s*/sheriff replan\s+(\S[\s\S]*?)\s*$")
STALE_MINUTES = 30
REASON_LIMIT = 2_000
FAILURE_LIMIT = 300
FAILURES_ALLOWED = 2
BUDGET_FAILURES = "plan failed twice in a row"
MAINTAINER_ROLES = ("admin", "maintain")
USAGE_MARKER = re.compile(r"<!--\s*sheriff-usage\s+(\{.*\})\s*-->")
NO_PLAN_LABEL = "sheriff:no-plan"
KEY_SEPARATOR = "/plan-"
DEFAULT_MODEL = "claude:claude-opus-5-5"
SECTIONS = ("Problem", "Solution space", "Chosen approach", "Tests", "Production check", "Out of scope")
PLAN_LIMIT = 20_000
TITLE_LIMIT = 300
BODY_LIMIT = 20_000
COMMENT_LIMIT = 4_000
COMMENTS_SHOWN = 50
SHA = re.compile(r"[0-9a-f]{40}")
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
REQUESTER = re.compile(r"(?:[A-Za-z0-9-]{1,39}(?:\[bot\])?)?")
ISSUE_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){issue(number:$number){
  number title body state author{login __typename} issueType{name} labels(first:20){nodes{name}}
  comments(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{databaseId body createdAt lastEditedAt author{login __typename} editor{login}}}}}}"""
_SPEC = importlib.util.spec_from_file_location("sheriff_reviewers", pathlib.Path(__file__).with_name("reviewers.py"))
reviewers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reviewers)


class PlanError(Exception):
    pass


def run(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PlanError(" ".join(args[:3]) + " failed: " + (result.stderr or result.stdout).strip()[:300])
    return result.stdout


def gh_json(args):
    try:
        return json.loads(run(["gh"] + args))
    except ValueError as exc:
        raise PlanError("gh returned invalid JSON") from exc


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(key + "=" + str(value) + "\n" for key, value in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    print(json.dumps(values), flush=True)


def bare(login):
    return str(login or "").removesuffix("[bot]")


def first_line(body):
    return str(body or "").split("\n", 1)[0].strip()


def task_id(title):
    found = TASK_TITLE.match(title or "")
    return found.group(1) if found else None


def login_of(actor):
    login = (actor or {}).get("login") or ""
    return login + "[bot]" if login and (actor or {}).get("__typename") == "Bot" else login


def read_issue(number, fetch):
    owner, name = os.environ["GH_REPO"].split("/", 1)
    issue, comments, cursor = None, [], None
    while True:
        args = ["api", "graphql", "-f", "query=" + ISSUE_QUERY, "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + str(number)]
        data = fetch(args + (["-f", "cursor=" + cursor] if cursor else []))
        if not isinstance(data, dict):
            raise PlanError("issue response is not an object")
        raw = (((data.get("data") or {}).get("repository")) or {}).get("issue")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise PlanError("issue response cannot be read")
        page = raw.get("comments") or {}
        for node in page.get("nodes") or []:
            author = node.get("author") or {}
            comments.append({"id": node.get("databaseId"), "author": login_of(author), "bot": author.get("__typename") == "Bot",
                             "body": node.get("body") or "", "created": node.get("createdAt") or "",
                             "edited": bool(node.get("lastEditedAt")), "editor": (node.get("editor") or {}).get("login") or ""})
        if issue is None:
            labels = [(label.get("name") or "").lower() for label in (raw.get("labels") or {}).get("nodes") or []]
            kind = ((raw.get("issueType") or {}).get("name") or "").lower()
            author = raw.get("author") or {}
            issue = {"number": raw.get("number"), "title": raw.get("title") or "", "body": raw.get("body") or "",
                     "open": raw.get("state") == "OPEN", "author": login_of(author), "author_bot": author.get("__typename") == "Bot",
                     "decision": kind == "decision" or "decision" in labels}
        if not (page.get("pageInfo") or {}).get("hasNextPage"):
            return dict(issue, comments=comments)
        cursor = page["pageInfo"]["endCursor"]


def plannable(issue):
    if not task_id(issue["title"]):
        return "not a task issue"
    if issue["decision"]:
        return "a decision"
    return "" if issue["open"] else "closed"


def plans(comments, slug):
    found = []
    for comment in comments:
        marker = PLAN_MARKER.match(first_line(comment["body"]))
        if not (marker and comment["bot"] and bare(comment["author"]) == slug):
            continue
        found.append({"id": comment["id"], "version": int(marker.group(1)), "base": marker.group(2),
                      "superseded": marker.group(3) is not None, "body": comment["body"], "created": comment["created"],
                      "tampered": bool(comment["edited"]) and bare(comment["editor"]) != slug})
    return found


def current_plan(comments, slug):
    found = plans(comments, slug)
    newest = max(found, key=lambda item: item["version"], default=None)
    return None if newest is None or newest["superseded"] or newest["tampered"] else newest


def open_reservation(comments, comment_id, version, slug):
    held = next((c for c in comments if str(c["id"]) == str(comment_id)), None)
    marker = START_MARKER.match(first_line(held["body"])) if held else None
    return bool(held and held["bot"] and bare(held["author"]) == slug and marker and marker.group(1) == str(version)
                and not (held["edited"] and bare(held["editor"]) != slug))


def in_repo(ref, repo):
    target = (ref.get("repository") or {})
    return (((target.get("owner") or {}).get("login") or "") + "/" + (target.get("name") or "")).lower() == repo.lower()


def closing(rows, repo):
    return sorted(ref["number"] for ref in rows or [] if in_repo(ref, repo))


def task_issue(view, repo, fetch):
    for number in closing(view.get("closingIssuesReferences"), repo):
        issue = read_issue(number, fetch)
        if issue and not plannable(issue):
            return issue
    wanted = PR_TASK.search(view.get("title") or "")
    if not wanted:
        return None
    pages = fetch(["api", "--paginate", "--slurp", "repos/{owner}/{repo}/issues?state=open&per_page=100"])
    rows = [row for page in pages or [] for row in (page if isinstance(page, list) else [page]) if isinstance(row, dict)]
    for number in sorted(row["number"] for row in rows if "pull_request" not in row and task_id(row.get("title")) == wanted.group(1)):
        issue = read_issue(number, fetch)
        if issue and not plannable(issue):
            return issue
    return None


def linked_prs(number, task, rows, repo):
    return sorted(row["number"] for row in rows
                  if number in closing(row.get("closingIssuesReferences"), repo) or (row.get("title") or "").endswith("(" + task + ")"))


def planning_on(value):
    return str(value or "").strip().lower() == "on"


def review_key(base, version, plan_id=0):
    return base + KEY_SEPARATOR + str(version) + "." + str(plan_id) if version else base


def keyed_to(external_id, version, plan_id):
    return str(external_id or "").partition(KEY_SEPARATOR)[2] == str(version) + "." + str(plan_id)


def escape(text):
    return str(text or "").replace("<!--", "&lt;!--")


def supersede(body, newer):
    head, _, rest = str(body).partition("\n")
    return head.rstrip().removesuffix("-->").rstrip() + " superseded-by=" + str(newer) + " -->\n**Superseded by v" + str(newer) + ".**\n" + rest


def failure_note(version, why, planned=False):
    retry = "`/sheriff replan <reason>`" if planned else "`/sheriff plan` (or `/sheriff replan <reason>`)"
    return ("<!-- sheriff-plan-failed v=" + str(version) + " -->\nSheriff could not write plan v" + str(version) + ": "
            + escape(str(why))[:FAILURE_LIMIT] + ". Comment " + retry + " to try again.")


def usage_of(body):
    lines = str(body or "").rstrip().splitlines()
    found = USAGE_MARKER.fullmatch(lines[-1].strip()) if lines else None
    try:
        data = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def check_plan(text):
    missing = [name for name in SECTIONS if not re.search(r"^#{1,3}\s*" + re.escape(name) + r"\s*$", text, re.I | re.M)]
    if missing:
        return "missing sections: " + ", ".join(missing)
    return "longer than " + str(PLAN_LIMIT) + " characters" if len(text) > PLAN_LIMIT else ""


def plan_comment(record):
    version, requester = str(record["version"]), record.get("requester") or ""
    numbers = {key: value for key, value in record["usage"].items()
               if re.fullmatch(r"[a-z_]{1,32}", str(key)) and isinstance(value, (int, float)) and not isinstance(value, bool)}
    usage = json.dumps({"provider": record["provider"], "model": record["model"], "usage": numbers})
    return ("<!-- sheriff-plan v=" + version + " base=" + record["base"] + " -->\n**Sheriff plan v" + version + "** (`" + record["model"]
            + "`, default branch `" + record["base"][:12] + "`" + (", replan requested by @" + requester if requester else "") + ")\n\n"
            + escape(record["text"]).strip() + "\n\n<!-- sheriff-usage " + usage + " -->")


def trusted(login, bot, role, ack):
    return bool(bot or (login and (role(login) in MAINTAINER_ROLES or login.lower() in ack)))


def moment(text):
    return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def is_budget(comment):
    lines = str(comment["body"]).rstrip().splitlines()
    return bool(lines) and lines[-1].strip() == BUDGET_MARKER


def command(comment, pattern, role, ack):
    found = None if comment["edited"] else pattern.match(comment["body"])
    return found if found and trusted(comment["author"], comment["bot"], role, ack) else None


def newest_replan(comments, after, role, ack):
    for comment in reversed(comments):
        found = command(comment, REPLAN_COMMAND, role, ack) if comment["created"] > after else None
        if found:
            return comment, found.group(1)[:REASON_LIMIT]
    return None, ""


def unlocks_of(comments, budgets, marks, role, unlockers):
    found = []
    for comment in comments:
        earlier = [b for b in budgets if b["created"] < comment["created"]]
        if not (earlier and not comment["edited"] and not comment["bot"] and PLAN_COMMAND.match(comment["body"])):
            continue
        budget = earlier[-1]
        before = max([at for at, _ in marks if at < budget["created"]], default="")
        asked = {c["author"] for c in comments if before < c["created"] < comment["created"] and not c["edited"] and REPLAN_COMMAND.match(c["body"])}
        if comment["author"] not in asked and (comment["author"].lower() in unlockers or role(comment["author"]) in MAINTAINER_ROLES):
            found.append((budget, comment))
    return found


def decide(issue, slug, role, ack, unlockers, rounds, now, opened=False):
    def result(action, why, version=0, reason="", requester=""):
        return {"action": action, "why": why, "version": version, "reason": reason, "requester": requester}
    why = plannable(issue)
    if why:
        return result("skip", why)
    comments = issue["comments"]
    mine = [c for c in comments if c["bot"] and bare(c["author"]) == slug]
    found = plans(comments, slug)
    latest = max((item["version"] for item in found), default=0)
    plan_at = max((item["created"] for item in found), default="")
    marks = sorted([(item["created"], "plan") for item in found] + [(c["created"], "failed") for c in mine if FAILED_MARKER.match(first_line(c["body"]))]
                   + [(c["created"], "budget") for c in mine if is_budget(c)])
    since = marks[-1][0] if marks else ""
    budgets = sorted((c for c in mine if is_budget(c)), key=lambda c: c["created"])
    failures = sum(1 for at, kind in marks if kind == "failed" and at > max(plan_at, budgets[-1]["created"] if budgets else ""))
    unlocked = unlocks_of(comments, budgets, marks, role, unlockers)
    reason, requester = "", ""
    if marks and marks[-1][1] == "budget":
        asks = [c for b, c in unlocked if b is budgets[-1]]
        if not asks:
            return result("skip", "plan budget used")
        replan, reason = newest_replan(comments, plan_at, role, ack)
        requester = replan["author"] if replan else ""
    elif current_plan(comments, slug):
        replan, reason = newest_replan(comments, since, role, ack)
        if not replan:
            return result("skip", "already planned")
        requester = replan["author"]
    else:
        asks = [c for c in comments if c["created"] > since and command(c, PLAN_COMMAND, role, ack)]
        replan, reason = newest_replan(comments, since, role, ack)
        if replan:
            requester = replan["author"]
        elif not (asks or (opened and not since and trusted(issue["author"], issue["author_bot"], role, ack))):
            return result("skip", "waiting for /sheriff plan from a maintainer")
    version = latest + 1
    if latest >= rounds + len(unlocked) or failures >= FAILURES_ALLOWED:
        return result("budget", BUDGET_FAILURES if failures >= FAILURES_ALLOWED else "plan budget used", version, reason, requester)
    started = [c for c in mine if (START_MARKER.match(first_line(c["body"])) or [None, 0])[1] == str(version)]
    if any(now - moment(c["created"]) < datetime.timedelta(minutes=STALE_MINUTES) for c in started):
        return result("skip", "a plan is being written")
    return result("plan", "", version, reason, requester)


def make_role():
    cache = {}

    def role(login):
        if login not in cache:
            try:
                cache[login] = str((gh_json(["api", "repos/{owner}/{repo}/collaborators/" + login + "/permission"]) or {}).get("role_name") or "")
            except PlanError:
                cache[login] = ""
        return cache[login]
    return role


def logins(text):
    return tuple(name.strip().lower() for name in (text or "").split(",") if name.strip())


def post_comment(number, body):
    created = gh_json(["api", "-X", "POST", "repos/{owner}/{repo}/issues/" + str(number) + "/comments", "-f", "body=" + body]) or {}
    comment_id = str(created.get("id") or "")
    if not comment_id.isdigit():
        raise PlanError("GitHub returned no id for the comment")
    return comment_id


def reserve(args):
    login = os.environ.get("GH_ACTOR_LOGIN", "").strip()
    if not login:
        raise PlanError("GH_ACTOR_LOGIN is not set: run plan.py reserve through app-token.py as the gate App")
    slug = bare(login)
    empty = {"version": "", "comment_id": "", "request": "", "app": slug}
    issue = read_issue(args.issue, gh_json)
    if issue is None:
        return output(skip="issue not found", **empty)
    decided = decide(issue, slug, make_role(), logins(args.ack_authors), logins(args.unlock_authors), args.rounds, datetime.datetime.now(datetime.timezone.utc), args.event == "opened")
    if decided["action"] == "skip":
        return output(skip=decided["why"], **empty)
    if decided["action"] == "budget":
        if decided["why"] == BUDGET_FAILURES:
            text = "Sheriff failed to write a plan " + str(FAILURES_ALLOWED) + " times in a row. A maintainer comments `/sheriff plan` to try again."
        else:
            text = ("Sheriff has written " + str(decided["version"] - 1) + " plans for this issue, the limit. A maintainer who did not ask "
                    "for this replan comments `/sheriff plan` to allow one more.")
        post_comment(args.issue, text + "\n\n" + BUDGET_MARKER)
        return output(skip="plan budget used", **empty)
    server, repo, run_id = (os.environ.get(name, "").strip() for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    link = " ([workflow run](" + server + "/" + repo + "/actions/runs/" + run_id + "))" if server and repo and run_id else ""
    version = decided["version"]
    comment_id = post_comment(args.issue, "<!-- sheriff-plan-start v=" + str(version) + " -->\nSheriff is writing plan v" + str(version) + " for this issue" + link)
    request = json.dumps({"version": version, "reason": decided["reason"], "requester": decided["requester"]}, separators=(",", ":"))
    return output(skip="", version=version, comment_id=comment_id, request=request, app=slug)


def recheck(args):
    login = os.environ.get("GH_ACTOR_LOGIN", "").strip()
    if not login:
        raise PlanError("GH_ACTOR_LOGIN is not set: run plan.py decide through app-token.py as the gate App")
    issue = read_issue(args.issue, gh_json)
    if issue is None:
        return output(action="skip", why="issue not found")
    decided = decide(issue, bare(login), make_role(), logins(args.ack_authors), logins(args.unlock_authors), args.rounds,
                     datetime.datetime.now(datetime.timezone.utc))
    return output(action=decided["action"], why=decided["why"])


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def prepare(args):
    out, request = pathlib.Path(args.out), json.loads(args.request)
    version = int(request["version"])

    def skip(why):
        write_json(out / "plan.json", {"issue": args.issue, "version": version, "ok": False, "failure": why})
        return output(skip=why, base="")
    issue = read_issue(args.issue, gh_json)
    if issue is None:
        return skip("issue not found")
    why = plannable(issue)
    if why:
        return skip(why)
    newer = max((item["version"] for item in plans(issue["comments"], args.app)), default=0)
    if newer >= version:
        return skip("plan v" + str(newer) + " already exists")
    if not open_reservation(issue["comments"], args.comment_id, version, args.app):
        return skip("the reservation of plan v" + str(version) + " is no longer open")
    base = run(["git", "rev-parse", "HEAD"], cwd=args.checkout).strip()
    role, ack = make_role(), logins(args.ack_authors)
    shown = [{"author": c["author"], "created": c["created"], "body": c["body"][:COMMENT_LIMIT]} for c in issue["comments"]
             if not (c["bot"] and bare(c["author"]) == args.app) and trusted(c["author"], c["bot"], role, ack)][-COMMENTS_SHOWN:]
    write_json(out / "issue.json", {"number": issue["number"], "title": issue["title"][:TITLE_LIMIT], "body": issue["body"][:BODY_LIMIT],
                                    "author": issue["author"], "comments": shown})
    write_json(out / "request.json", {"issue": args.issue, "version": version, "reason": str(request.get("reason") or ""),
                                      "requester": str(request.get("requester") or ""), "base": base})
    current = current_plan(issue["comments"], args.app)
    if current:
        (out / "previous.md").write_text(current["body"])
    return output(skip="", base=base)


def run_plan(args):
    request = json.loads(args.request)
    record = {"issue": args.issue, "version": int(request["version"]), "ok": False}
    try:
        spec = reviewers.parse_spec(args.model or DEFAULT_MODEL)
        web = reviewers.web_tools(args.web, args.fetch_domains)
        result = reviewers.run(spec, pathlib.Path(args.prompt).read_text(), args.workdir, turns=args.turns, web=web if spec[0] == "claude" else ())
    except reviewers.ReviewerError as exc:
        record["failure"] = "the planner ended without a result: " + str(exc)
    else:
        text = result["text"].strip()
        record["failure"] = check_plan(text)
        if not record["failure"]:
            record = {"issue": args.issue, "version": record["version"], "base": args.base, "provider": result["provider"], "model": result["model"],
                      "ok": True, "text": text, "usage": result["usage"], "requester": str(request.get("requester") or "")}
    write_json(pathlib.Path(args.out), record)
    return output(ok=str(record["ok"]).lower(), issue=args.issue, version=record["version"])


def record_problem(record, issue, version):
    if not (isinstance(record, dict) and record.get("issue") == issue and record.get("version") == version and type(record.get("version")) is int
            and isinstance(record.get("ok"), bool)):
        return "the planning run produced a malformed result"
    if not record["ok"]:
        return ""
    if not (SHA.fullmatch(str(record.get("base"))) and MODEL_NAME.fullmatch(str(record.get("model"))) and record.get("provider") in reviewers.SECRETS
            and REQUESTER.fullmatch(str(record.get("requester") or "")) and isinstance(record.get("usage"), dict) and isinstance(record.get("text"), str)):
        return "the planning run produced a malformed result"
    return check_plan(record["text"])


def patch_comment(comment_id, body):
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "body.md"
        path.write_text(body)
        run(["gh", "api", "-X", "PATCH", "repos/{owner}/{repo}/issues/comments/" + str(comment_id), "-F", "body=@" + str(path)])


def post(args):
    login = os.environ.get("GH_ACTOR_LOGIN", "").strip()
    if not login:
        raise PlanError("GH_ACTOR_LOGIN is not set: run plan.py post through app-token.py as the gate App")
    slug = bare(login)
    issue = read_issue(args.issue, gh_json)
    if not open_reservation((issue or {}).get("comments", []), args.comment_id, args.version, slug):
        raise PlanError("comment " + str(args.comment_id) + " is not this run's open reservation; nothing is posted")
    refused = ""
    try:
        record = json.loads(pathlib.Path(args.record).read_text())
    except (OSError, ValueError):
        record, why = None, "the planning run ended without a result"
    else:
        refused = record_problem(record, args.issue, args.version)
        why = refused or ("" if record["ok"] else str(record.get("failure") or "the planner gave no reason"))
    newer = max((item["version"] for item in plans(issue["comments"], slug)), default=0)
    if not why and newer >= args.version:
        why = "overtaken by plan v" + str(newer)
    old = current_plan(issue["comments"], slug)
    if why:
        patch_comment(args.comment_id, failure_note(args.version, why, bool(old)))
        output(posted="false", prs="")
        if refused:
            raise PlanError("the record is refused: " + refused)
        return
    patch_comment(args.comment_id, plan_comment(record))
    if old:
        try:
            patch_comment(old["id"], supersede(old["body"], args.version))
        except PlanError as exc:
            print(json.dumps({"ok": False, "warning": str(exc)}), file=sys.stderr, flush=True)
    prs = ""
    try:
        rows = gh_json(["pr", "list", "--state", "open", "--limit", "200", "--json", "number,title,closingIssuesReferences"])
        task = task_id(issue["title"])
        prs = " ".join(str(n) for n in linked_prs(args.issue, task, rows, os.environ["GH_REPO"])) if task else ""
    except PlanError as exc:
        print(json.dumps({"ok": False, "warning": "linked pull requests of issue " + str(args.issue) + " could not be listed: " + str(exc)}), file=sys.stderr, flush=True)
    return output(posted="true", prs=prs)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plan.py")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reserve")
    r.add_argument("--issue", type=int, required=True)
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--ack-authors", default="")
    r.add_argument("--unlock-authors", default="")
    r.add_argument("--event", default="")
    d = sub.add_parser("decide")
    d.add_argument("--issue", type=int, required=True)
    d.add_argument("--rounds", type=int, default=3)
    d.add_argument("--ack-authors", default="")
    d.add_argument("--unlock-authors", default="")
    q = sub.add_parser("prepare")
    q.add_argument("--issue", type=int, required=True)
    q.add_argument("--request", required=True)
    q.add_argument("--app", required=True)
    q.add_argument("--comment-id", required=True)
    q.add_argument("--ack-authors", default="")
    q.add_argument("--checkout", required=True)
    q.add_argument("--out", required=True)
    w = sub.add_parser("run")
    w.add_argument("--issue", type=int, required=True)
    w.add_argument("--request", required=True)
    w.add_argument("--base", required=True)
    w.add_argument("--workdir", required=True)
    w.add_argument("--prompt", required=True)
    w.add_argument("--model", default=DEFAULT_MODEL)
    w.add_argument("--turns", type=int, default=40)
    w.add_argument("--web", default="search")
    w.add_argument("--fetch-domains", default="")
    w.add_argument("--out", required=True)
    s = sub.add_parser("post")
    s.add_argument("--record", required=True)
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--version", type=int, required=True)
    s.add_argument("--comment-id", required=True)
    args = parser.parse_args(argv)
    try:
        {"reserve": reserve, "decide": recheck, "prepare": prepare, "run": run_plan, "post": post}[args.command](args)
    except (PlanError, reviewers.ReviewerError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
