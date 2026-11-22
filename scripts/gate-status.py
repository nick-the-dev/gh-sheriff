#!/usr/bin/env python3
"""Sheriff gate: post the Merge gate commit status on the heads of open pull requests into the default branch.

The Review check run is created by the Sheriff review workflow, as the gate App, so only that App can change it."""
import argparse
import datetime
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

MERGE_CONTEXT = "Merge gate"
ACK_RE = re.compile(r"<!--\s*sheriff-ack\s*-->")
TRANSIENT = ("has not concluded", "production does not serve the default branch", "no deploy run for the default branch")
DESCRIPTION_LIMIT = 140


class GateError(Exception):
    pass


def setting(name):
    return (os.environ.get(name) or "").strip()


def ack_authors():
    return tuple(name.strip().lower() for name in setting("SHERIFF_ACK_AUTHORS").split(",") if name.strip())


def acknowledger(comment, listed):
    return is_bot(comment) or author(comment).lower() in listed


def parse_time(value):
    text = value.strip() if isinstance(value, str) else ""
    try:
        parsed = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)).astimezone(datetime.timezone.utc)


def gh(args):
    try:
        result = subprocess.run(["gh"] + list(args), capture_output=True, text=True, check=False)
    except OSError as exc:
        raise GateError("cannot run gh: " + str(exc)) from exc
    if result.returncode:
        raise GateError("gh " + " ".join(args[:3]) + " failed: " + (result.stderr or result.stdout or "").strip()[:300])
    return result.stdout


def gh_json(args):
    try:
        return json.loads(gh(args))
    except ValueError as exc:
        raise GateError("gh returned invalid JSON") from exc


def gh_pages(path):
    try:
        return [json.loads(line) for line in gh(["api", "--paginate", "--jq", ".[]", path]).splitlines() if line.strip()]
    except ValueError as exc:
        raise GateError("gh returned invalid JSON") from exc


def author(comment):
    user = comment.get("user") or comment.get("author") or {}
    return str(user.get("login") or "") if isinstance(user, dict) else ""


def text(comment):
    return comment.get("body") if isinstance(comment.get("body"), str) else ""


def created(comment):
    return parse_time(comment.get("created_at") or comment.get("submitted_at"))


def active(comment):
    moments = [moment for moment in (created(comment), parse_time(comment.get("updated_at"))) if moment is not None]
    return max(moments) if moments else None


def is_bot(comment):
    user = comment.get("user") or comment.get("author") or {}
    return isinstance(user, dict) and (user.get("type") == "Bot" or str(user.get("login") or "").endswith("[bot]"))


def lines_of(body):
    return [line.strip() for line in (body or "").strip().splitlines() if line.strip()]


def trailer(body, pattern):
    lines = lines_of(body)
    return pattern.fullmatch(lines[-1]) if lines else None


def unanswered(comments, listed):
    acks = [created(comment) for comment in comments if acknowledger(comment, listed) and trailer(text(comment), ACK_RE)]
    acks = [moment for moment in acks if moment is not None]
    acked = max(acks) if acks else None
    pending = []
    for comment in comments:
        if acknowledger(comment, listed):
            continue
        moment = active(comment)
        if moment is not None and acked is not None and moment < acked:
            continue
        url = comment.get("html_url")
        pending.append(author(comment) + (" " + url if isinstance(url, str) and url else ""))
    return pending


def http_get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            return response.getcode(), response.read()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None, b""


def deploy_failures(workflow, main_sha):
    runs = gh_json(["run", "list", "--workflow", workflow, "--commit", main_sha, "--limit", "30",
                    "--json", "databaseId,status,conclusion,createdAt"])
    runs = [run for run in runs if isinstance(run, dict) and run.get("conclusion") != "skipped"]
    if not runs:
        return ["no deploy run for the default branch " + main_sha[:12] + " yet"]
    run = max(runs, key=lambda item: (str(item.get("createdAt") or ""), str(item.get("databaseId") or "")))
    if run.get("status") != "completed":
        return ["deploy run " + str(run.get("databaseId")) + " for the default branch has not concluded"]
    if run.get("conclusion") != "success":
        return ["deploy run " + str(run.get("databaseId")) + " for the default branch concluded " + str(run.get("conclusion"))]
    return []


def production_failures(version_url, health_url, workflow):
    if not (version_url or health_url or workflow):
        return []
    main_sha = gh(["api", "repos/{owner}/{repo}/commits/HEAD", "--jq", ".sha"]).strip()
    failures = []
    if version_url:
        code, body = http_get(version_url)
        try:
            served = json.loads(body.decode("utf-8", "replace")).get("sha") if code == 200 else None
        except (ValueError, AttributeError):
            served = None
        if code != 200:
            failures.append("production version endpoint is not 200")
        elif served != main_sha:
            failures.append("production does not serve the default branch " + main_sha[:12] + " yet")
    if health_url and http_get(health_url)[0] != 200:
        failures.append("production health endpoint is not 200")
    if workflow:
        failures.extend(deploy_failures(workflow, main_sha))
    return failures


def merge_status(number, comments, prod_failures, listed, override, decision="", prod_checked=True):
    overridden = override == str(number)
    failures = [] if overridden else list(prod_failures)
    if decision == "CHANGES_REQUESTED":
        failures.append("a reviewer requested changes")
    failures.extend("unacknowledged comment from " + item for item in unanswered(comments, listed))
    if not failures:
        if overridden:
            return "success", "override: production checks skipped for this PR by SHERIFF_OVERRIDE"
        if not prod_checked:
            return "success", "comments answered; no production checks configured"
        return "success", "production serves the default branch and is healthy, deploy green, comments answered"
    transient = all(any(mark in item for mark in TRANSIENT) for item in failures)
    return ("pending" if transient else "failure"), failures[0]


def target_url():
    server, repo, run = (setting(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return server + "/" + repo + "/actions/runs/" + run if server and repo and run else ""


def current_statuses(sha):
    actor = setting("GH_ACTOR_LOGIN").lower()
    rows = gh_json(["api", "repos/{owner}/{repo}/commits/" + sha + "/statuses?per_page=100"])
    latest = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        creator = row.get("creator") if isinstance(row.get("creator"), dict) else {}
        if actor and str(creator.get("login") or "").lower() != actor:
            continue
        latest.setdefault(str(row.get("context") or ""), (str(row.get("state") or ""), str(row.get("description") or "")))
    return latest


def post(sha, context, state, description, dry_run, current=None):
    payload = {"sha": sha, "context": context, "state": state, "description": description[:DESCRIPTION_LIMIT]}
    if current is not None and current.get(context) == (state, payload["description"]):
        return dict(payload, unchanged=True)
    if not dry_run:
        args = ["api", "-X", "POST", "repos/{owner}/{repo}/statuses/" + sha,
                "-f", "state=" + state, "-f", "context=" + context, "-f", "description=" + payload["description"]]
        url = target_url()
        gh(args + (["-f", "target_url=" + url] if url else []))
    return payload


def changes_requested(reviews):
    latest = {}
    for review in reviews:
        state = str(review.get("state") or "").upper() if isinstance(review, dict) else ""
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") and not is_bot(review):
            latest[author(review)] = state
    return "CHANGES_REQUESTED" in latest.values()


def pull_comments(number, reviews):
    rows = list(reviews)
    for path in ("issues/" + str(number) + "/comments", "pulls/" + str(number) + "/comments"):
        rows += gh_pages("repos/{owner}/{repo}/" + path)
    return [row for row in rows if isinstance(row, dict) and text(row).strip()]


def judge(number, prod_failures, listed, override, dry_run, prod_checked=True, default_branch="", quiet=False):
    view = gh_json(["pr", "view", str(number), "--json", "state,headRefOid,baseRefName"])
    head = str(view.get("headRefOid") or "")
    if str(view.get("state") or "").upper() != "OPEN" or len(head) != 40:
        return {"pr": number, "skipped": "not open"}
    if default_branch and str(view.get("baseRefName") or "") != default_branch:
        return {"pr": number, "head": head, "ignored": "not into the default branch"}
    try:
        reviews = gh_pages("repos/{owner}/{repo}/pulls/" + str(number) + "/reviews")
        comments = pull_comments(number, reviews)
    except GateError as exc:
        if not quiet:
            post(head, MERGE_CONTEXT, "error", "cannot read this pull request: " + str(exc), dry_run)
        raise
    decision = "CHANGES_REQUESTED" if changes_requested(reviews) else ""
    state, description = merge_status(number, comments, prod_failures, listed, override, decision, prod_checked)
    return {"pr": number, "head": head, "state": state, "description": description, "quiet": quiet,
            "current": None if quiet else current_statuses(head)}


def publish(judged, dry_run):
    if "state" not in judged:
        return judged
    return {"pr": judged["pr"], "head": judged["head"], "statuses": [
        post(judged["head"], MERGE_CONTEXT, judged["state"], judged["description"], dry_run, judged["current"])]}


def required_contexts(branch):
    try:
        rules = gh_json(["api", "repos/{owner}/{repo}/rules/branches/" + branch])
    except GateError:
        return None
    if not isinstance(rules, list):
        return None
    found = set()
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            for entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
                if isinstance(entry, dict) and entry.get("context"):
                    found.add(str(entry["context"]))
    return found - {MERGE_CONTEXT}


def checks_green(row, required=None):
    latest = {}
    for item in row.get("statusCheckRollup") or []:
        name = (item.get("name") or item.get("context") or "") if isinstance(item, dict) else ""
        if not name or name == MERGE_CONTEXT:
            continue
        moment = next((str(item[key]) for key in ("completedAt", "startedAt", "createdAt") if item.get(key) and not str(item[key]).startswith("0001")), "")
        if name not in latest or moment >= latest[name][0]:
            latest[name] = (moment, (item.get("conclusion") or item.get("state") or "").upper())
    if required is not None:
        if not required <= set(latest):
            return False
        latest = {name: value for name, value in latest.items() if name in required}
    return all(result in ("SUCCESS", "NEUTRAL", "SKIPPED") for _, result in latest.values())


def serialize(judged, armed, green):
    for number in armed:
        if judged.get(number, {}).get("state") == "success" and number not in green:
            judged[number]["state"] = "pending"
            judged[number]["description"] = "waiting for its checks before merging"
    passing = sorted(number for number, item in judged.items() if number in armed and item.get("state") == "success")
    for number in passing[1:]:
        judged[number]["state"] = "pending"
        judged[number]["description"] = "one merge per deploy: waiting for #" + str(passing[0])


def open_rows():
    rows = gh_json(["pr", "list", "--state", "open", "--limit", "1000", "--json", "number,autoMergeRequest,baseRefName,headRefOid,statusCheckRollup"])
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("number"), int)]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gate-status.py")
    parser.add_argument("--pr", type=int, action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        listed = ack_authors()
        default_branch = setting("SHERIFF_DEFAULT_BRANCH") or gh(["api", "repos/{owner}/{repo}", "--jq", ".default_branch"]).strip()
        rows = open_rows()
        numbers = args.pr or [row["number"] for row in rows]
        prod_settings = (setting("SHERIFF_VERSION_URL"), setting("SHERIFF_HEALTH_URL"), setting("SHERIFF_DEPLOY_WORKFLOW"))
        try:
            prod = production_failures(*prod_settings) if numbers else []
        except GateError as exc:
            prod = ["cannot read the production state: " + str(exc)]
        override = setting("SHERIFF_OVERRIDE")
    except GateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1
    armed = {row["number"] for row in rows if row.get("autoMergeRequest") and row.get("baseRefName") == default_branch}
    required = required_contexts(default_branch)
    green = {row["number"] for row in rows if checks_green(row, required)}
    judged, queue, swept = {}, list(numbers), not args.pr

    def run(number, quiet=False):
        try:
            judged[number] = judge(number, prod, listed, override, args.dry_run, any(prod_settings), default_branch, quiet)
        except GateError as exc:
            judged[number] = {"pr": number, "error": str(exc), "quiet": quiet}

    while queue:
        number = queue.pop(0)
        run(number)
        if "skipped" in judged[number] and not swept:
            swept = True
            queue += [other["number"] for other in rows if other["number"] not in judged and other["number"] not in queue]
        elif not swept and number not in armed and "state" in judged[number]:
            queue += sorted(other for other in armed if other not in judged and other not in queue)
    for number in args.pr:
        if number in armed and judged[number].get("state") == "success":
            for lower in sorted(other for other in armed if other < number and other not in judged):
                run(lower, quiet=True)
    serialize(judged, armed, green)
    results = [publish(item, args.dry_run) for item in judged.values() if not item.get("quiet")]
    failed = any("error" in item for item in results)
    print(json.dumps({"ok": not failed, "results": results}, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
