#!/usr/bin/env python3
"""Sheriff plan: the facts the planner shares, namely the comment markers, an issue read with its comments, the
App's current plan on a task issue, the task issue of a pull request, the pull requests linked to an issue and the
review keys that bind a review to a plan version; and who may ask for a plan, the budget, and the reservation comment
that the gate App posts before any model runs."""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

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
ISSUE_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){issue(number:$number){
  number title body state author{login __typename} issueType{name} labels(first:20){nodes{name}}
  comments(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{databaseId body createdAt lastEditedAt author{login __typename} editor{login}}}}}}"""


class PlanError(Exception):
    pass


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
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


def review_key(base, version):
    return base + KEY_SEPARATOR + str(version) if version else base


def keyed_to(external_id, version):
    return str(external_id or "").endswith(KEY_SEPARATOR + str(version))


def escape(text):
    return str(text or "").replace("<!--", "&lt;!--")


def supersede(body, newer):
    head, _, rest = str(body).partition("\n")
    return head.rstrip().removesuffix("-->").rstrip() + " superseded-by=" + str(newer) + " -->\n**Superseded by v" + str(newer) + ".**\n" + rest


def failure_note(version, why):
    return ("<!-- sheriff-plan-failed v=" + str(version) + " -->\nSheriff could not write plan v" + str(version) + ": "
            + escape(str(why)[:FAILURE_LIMIT]) + ". Comment `/sheriff plan` (or `/sheriff replan <reason>`) to try again.")


def usage_of(body):
    lines = str(body or "").rstrip().splitlines()
    found = USAGE_MARKER.fullmatch(lines[-1].strip()) if lines else None
    try:
        data = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
        asked = {c["author"] for c in comments if before < c["created"] < budget["created"] and not c["edited"] and REPLAN_COMMAND.match(c["body"])}
        if comment["author"] not in asked and (comment["author"].lower() in unlockers or role(comment["author"]) in MAINTAINER_ROLES):
            found.append((budget, comment))
    return found


def decide(issue, slug, role, ack, unlockers, rounds, now):
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
        elif not (asks or (not since and trusted(issue["author"], issue["author_bot"], role, ack))):
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
    decided = decide(issue, slug, make_role(), logins(args.ack_authors), logins(args.unlock_authors), args.rounds, datetime.datetime.now(datetime.timezone.utc))
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plan.py")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reserve")
    r.add_argument("--issue", type=int, required=True)
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--ack-authors", default="")
    r.add_argument("--unlock-authors", default="")
    args = parser.parse_args(argv)
    try:
        reserve(args)
    except PlanError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
