#!/usr/bin/env python3
"""Sheriff plan: the facts the planner shares, namely the comment markers, an issue read with its comments, the
App's current plan on a task issue, the task issue of a pull request, the pull requests linked to an issue and the
review keys that bind a review to a plan version."""
import os
import re

ID = r"[A-Z][A-Z0-9-]+"
TASK_TITLE = re.compile(r"^(" + ID + r")(?: #\d+)?: ")
PR_TASK = re.compile(r"\((" + ID + r")\)$")
PLAN_MARKER = re.compile(r"<!--\s*sheriff-plan\s+v=(\d+)\s+base=([0-9a-f]{40})(?:\s+superseded-by=(\d+))?\s*-->")
START_MARKER = re.compile(r"<!--\s*sheriff-plan-start\s+v=(\d+)\s*-->")
FAILED_MARKER = re.compile(r"<!--\s*sheriff-plan-failed\s+v=(\d+)\s*-->")
BUDGET_MARKER = "<!-- sheriff-plan-budget -->"
USAGE_MARKER = re.compile(r"<!--\s*sheriff-usage\s+(\{.*\})\s*-->")
NO_PLAN_LABEL = "sheriff:no-plan"
KEY_SEPARATOR = "/plan-"
DEFAULT_MODEL = "claude:claude-opus-5-5"
ISSUE_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){issue(number:$number){
  number title body state author{login __typename} issueType{name} labels(first:20){nodes{name}}
  comments(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{databaseId body createdAt lastEditedAt author{login __typename} editor{login}}}}}}"""


class PlanError(Exception):
    pass


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
