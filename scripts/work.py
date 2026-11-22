#!/usr/bin/env python3
"""Sheriff work state: one issue per task; its Stage, Verify by and Next action issue fields move with pull requests and releases."""
import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

STAGES = ("Candidate", "In progress", "Merged", "Released", "Verified", "Parked")
FORWARD = STAGES[:5]
DERIVED = ("In progress", "Merged", "Released")
VERIFY = ("Release only", "Live check", "Awaits event")
TYPES = ("Task", "Bug", "Feature", "Decision")
FIELDS = {"stage": "Stage", "verify": "Verify by", "next": "Next action"}
ID = r"[A-Z][A-Z0-9-]+"
ID_RE = re.compile(ID)
ISSUE_TITLE_RE = re.compile(r"^(" + ID + r"): ")
PR_TITLE_RE = re.compile(r"\((" + ID + r")\)$")
SHA_RE = re.compile(r"[0-9a-f]{40}")
AGENT_MARK = "<!-- sheriff-ack -->"
VERIFIED_MARK = "<!-- sheriff-verified sha={} -->"
VERIFIED_RE = re.compile(r"<!-- sheriff-verified sha=([0-9a-f]{40}) -->")
DECISION_LABEL = "decision"
STOP_WORDS = frozenset("a an and as at be by for from in into is it of on or the to with".split())
PAGES = 2
RESERVE = 100
MAX_PAGES = 50
SHOWN = 40
TEXT_LIMIT = 1024
BODY_LIMIT = 65000
EVIDENCE_MIN = 40
REPO = ""


class WorkError(Exception):
    pass


def setting(name):
    return (os.environ.get(name) or "").strip()


def gh(args, stdin=None):
    try:
        return subprocess.run(["gh"] + list(args), input=stdin, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise WorkError("cannot run gh: " + str(exc)) from exc


def gh_out(args, stdin=None):
    result = gh(args, stdin)
    if result.returncode:
        raise WorkError("gh " + " ".join(args[:2]) + " failed: " + (result.stderr or result.stdout or "").strip()[:300])
    return result.stdout


def api(args, stdin=None):
    out = gh_out(["api"] + list(args), stdin)
    try:
        return json.loads(out) if out.strip() else None
    except ValueError as exc:
        raise WorkError("gh returned invalid JSON") from exc


def gql(query, variables=None):
    result = gh(["api", "graphql", "--input", "-"], json.dumps({"query": query, "variables": variables or {}}))
    try:
        body = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    errors = [str(error.get("message")) for error in body.get("errors") or [] if isinstance(error, dict)]
    if errors or result.returncode or not isinstance(body.get("data"), dict):
        raise WorkError("GitHub: " + ("; ".join(errors) or (result.stderr or "no data").strip())[:300])
    return body["data"]


def use_repo(explicit=None):
    global REPO
    REPO = explicit or setting("GH_REPO")
    if not REPO:
        found = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
        REPO = found.stdout.strip() if found.returncode == 0 else ""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", REPO):
        raise WorkError("run this inside a GitHub repository or pass --repo owner/name")
    return REPO


def names():
    owner, name = REPO.split("/", 1)
    return {"owner": owner, "name": name}


def paged(query, variables, pick, pages):
    cursor, nodes, data = None, [], {}
    for _ in range(pages):
        data = gql(query, dict(variables, cursor=cursor))
        connection = pick(data)
        nodes.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        cursor = connection["pageInfo"]["endCursor"]
    return data, nodes


SCHEMA_QUERY = """query($owner:String!,$name:String!){rateLimit{remaining} repository(owner:$owner,name:$name){id
 issueTypes(first:50){nodes{id name}}
 issueFields(first:50){nodes{__typename ... on IssueFieldSingleSelect{id name options{id name}} ... on IssueFieldText{id name}}}}}"""


def schema():
    data = gql(SCHEMA_QUERY, names())
    repository = data["repository"]
    fields = {node["name"]: node for node in repository["issueFields"]["nodes"] if node.get("name")}
    return {"repo": repository["id"], "types": {node["name"]: node["id"] for node in repository["issueTypes"]["nodes"]},
            "fields": {key: fields.get(label) for key, label in FIELDS.items()},
            "missing": [label for label in FIELDS.values() if label not in fields], "remaining": (data.get("rateLimit") or {}).get("remaining")}


def require(known):
    if known["missing"]:
        raise WorkError("the repository has no issue fields " + ", ".join(known["missing"]) + ": run gh sheriff install")
    return known


ISSUE = """id number title state stateReason url createdAt issueType{name} labels(first:20){nodes{name}}
 issueFieldValues(first:20){nodes{__typename
  ... on IssueFieldSingleSelectValue{name field{... on IssueFieldSingleSelect{name}}}
  ... on IssueFieldTextValue{value field{... on IssueFieldText{name}}}}}
 blockedBy(first:30){nodes{number state}}
 comments(last:1){nodes{author{login __typename} body}}"""
ISSUES_QUERY = ("query($owner:String!,$name:String!,$states:[IssueState!],$field:IssueOrderField!,$direction:OrderDirection!,$cursor:String){"
                "repository(owner:$owner,name:$name){issues(first:100,after:$cursor,states:$states,orderBy:{field:$field,direction:$direction}){"
                "pageInfo{hasNextPage endCursor} nodes{" + ISSUE + "}}}}")
NUMBER_QUERY = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){" + ISSUE + "}}}"
SEARCH_QUERY = "query($q:String!){search(query:$q,type:ISSUE,first:50){nodes{... on Issue{" + ISSUE + "}}}}"
FRESH_QUERY = """query($id:ID!){node(id:$id){... on Issue{state issueFieldValues(first:20){nodes{__typename
 ... on IssueFieldSingleSelectValue{name field{... on IssueFieldSingleSelect{name}}}
 ... on IssueFieldTextValue{value field{... on IssueFieldText{name}}}}}}}}"""


def field_values(node):
    out = {}
    for value in (node.get("issueFieldValues") or {}).get("nodes") or []:
        label = (value.get("field") or {}).get("name")
        if label:
            out[label] = value.get("name") if "name" in value else value.get("value")
    return out


def awaits_reply(comments):
    if not comments:
        return None
    last = comments[-1]
    author = last.get("author") or {}
    listed = {name.strip().lower() for name in os.environ.get("SHERIFF_ACK_AUTHORS", "").split(",") if name.strip()}
    trailer = (last.get("body") or "").rstrip().endswith(AGENT_MARK)
    if author.get("__typename") == "Bot" or (trailer and str(author.get("login") or "").lower() in listed):
        return None
    return author.get("login") or "someone"


def to_item(node):
    values = field_values(node)
    title = node.get("title") or ""
    match = ISSUE_TITLE_RE.match(title)
    kind = (node.get("issueType") or {}).get("name")
    labels = [label.get("name") for label in (node.get("labels") or {}).get("nodes") or []]
    return {"id": match.group(1) if match else None, "node": node["id"], "number": node["number"],
            "title": title[len(match.group(0)):] if match else title, "open": node.get("state") == "OPEN",
            "reason": node.get("stateReason"), "url": node.get("url"), "created": node.get("createdAt") or "", "type": kind,
            "decision": kind == "Decision" or DECISION_LABEL in labels,
            "stage": values.get(FIELDS["stage"]), "verify": values.get(FIELDS["verify"]), "next": values.get(FIELDS["next"]),
            "blocked_by": [{"number": b["number"], "open": b.get("state") == "OPEN"} for b in (node.get("blockedBy") or {}).get("nodes") or []],
            "awaits_reply": awaits_reply((node.get("comments") or {}).get("nodes") or [])}


def issues(state="OPEN", pages=MAX_PAGES):
    field, direction = ("CREATED_AT", "ASC") if state == "OPEN" else ("UPDATED_AT", "DESC")
    variables = dict(names(), states=[state], field=field, direction=direction)
    return [to_item(node) for node in paged(ISSUES_QUERY, variables, lambda data: data["repository"]["issues"], pages)[1]]


def closed_matches(task):
    query = "repo:" + REPO + ' is:issue is:closed in:title "' + task + '"'
    return [item for item in (to_item(node) for node in gql(SEARCH_QUERY, {"q": query})["search"]["nodes"] if node) if item["id"] == task]


def lookup(task):
    text = str(task).strip().lstrip("#")
    if text.isdigit():
        node = gql(NUMBER_QUERY, dict(names(), number=int(text)))["repository"]["issue"]
        if not node:
            raise WorkError("no issue #" + text)
        return to_item(node)
    found = [item for item in issues("OPEN") if item["id"] == text] or closed_matches(text)
    if not found:
        raise WorkError("no issue for " + text)
    if len(found) > 1:
        raise WorkError(text + " matches issues " + ", ".join("#" + str(item["number"]) for item in found))
    return found[0]


PRS_QUERY = """query($owner:String!,$name:String!,$states:[PullRequestState!],$cursor:String){repository(owner:$owner,name:$name){
 defaultBranchRef{name} pullRequests(first:100,after:$cursor,states:$states,orderBy:{field:UPDATED_AT,direction:DESC}){
 pageInfo{hasNextPage endCursor} nodes{number title state headRefName baseRefName isCrossRepository mergedAt mergeCommit{oid}}}}}"""


def pr_task(pr):
    match = PR_TITLE_RE.search((pr.get("title") or "").strip())
    if match:
        return match.group(1)
    tail = (pr.get("headRefName") or "").rsplit("/", 1)[-1]
    return tail if ID_RE.fullmatch(tail) else None


def pull_requests(pages=PAGES, merged=True):
    groups, default = {}, None
    for state, limit in (("OPEN", MAX_PAGES), ("MERGED", pages if merged else 0)):
        if not limit:
            continue
        data, nodes = paged(PRS_QUERY, dict(names(), states=[state]), lambda found: found["repository"]["pullRequests"], limit)
        default = ((data.get("repository") or {}).get("defaultBranchRef") or {}).get("name") or default
        for pr in nodes:
            task = pr_task(pr)
            if not task or pr.get("isCrossRepository") or (state == "MERGED" and pr.get("baseRefName") != default):
                continue
            groups.setdefault(task, []).append({"number": pr["number"], "state": state, "merged_at": pr.get("mergedAt"),
                                                "commit": (pr.get("mergeCommit") or {}).get("oid")})
    return groups, default


def served_sha(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8", "replace")) if response.getcode() == 200 else None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None
    sha = body.get("sha") if isinstance(body, dict) else None
    return sha if isinstance(sha, str) and SHA_RE.fullmatch(sha) else None


def compare(commit, sha, cache):
    if commit == sha:
        return "identical"
    if not (SHA_RE.fullmatch(commit or "") and SHA_RE.fullmatch(sha or "")):
        return ""
    if (commit, sha) not in cache:
        found = gh(["api", "repos/" + REPO + "/compare/" + commit + "..." + sha + "?per_page=1", "--jq", ".status"])
        cache[(commit, sha)] = found.stdout.strip() if found.returncode == 0 else ""
    return cache[(commit, sha)]


def deploy_runs(workflow, branch):
    args = ["run", "list", "--repo", REPO, "--workflow", workflow, "--limit", "30", "--json", "databaseId,headSha,status,conclusion,createdAt"]
    try:
        rows = json.loads(gh_out(args + (["--branch", branch] if branch else [])) or "[]")
    except (WorkError, ValueError):
        return []
    runs = [row for row in rows if isinstance(row, dict) and row.get("conclusion") != "skipped"]
    return sorted(runs, key=lambda row: (str(row.get("createdAt") or ""), str(row.get("databaseId") or "")), reverse=True)


def release_check(branch):
    url, workflow, cache = setting("SHERIFF_VERSION_URL"), setting("SHERIFF_DEPLOY_WORKFLOW"), {}
    if url:
        sha = served_sha(url)
        return lambda commit: ("production serves `" + sha + "`, which contains `" + commit + "`"
                               if sha and compare(commit, sha, cache) in ("ahead", "identical") else "")
    if workflow:
        runs = deploy_runs(workflow, branch)

        def check(commit):
            for run in runs:
                status = compare(commit, str(run.get("headSha") or ""), cache)
                if status == "behind":
                    return ""
                if status in ("ahead", "identical"):
                    done = run.get("status") == "completed" and run.get("conclusion") == "success"
                    return ("deploy run " + str(run.get("databaseId")) + " of " + workflow + " succeeded on `" + str(run.get("headSha")) +
                            "`, which contains `" + commit + "`") if done else ""
            return ""
        return check
    return lambda commit: "`" + commit + "` is merged and no release check is configured, so the merge is the release"


def latest_merge(item, prs):
    counted = item["stage"] in ("Merged", "Released", "Verified")
    merged = [pr for pr in prs if pr["state"] == "MERGED" and (counted or (pr["merged_at"] or "") >= item["created"])]
    return max(merged, key=lambda pr: pr["merged_at"] or "") if merged else None


def derive(item, prs, released):
    stage = item["stage"]
    if item["decision"] or stage == "Verified":
        return None
    rank = FORWARD.index(stage) if stage in FORWARD else -1
    if any(pr["state"] == "OPEN" for pr in prs):
        return "In progress" if stage == "Parked" or rank < 1 else None
    merge = None if stage == "Parked" else latest_merge(item, prs)
    best = "Verified" if item["verify"] == "Release only" else "Released"
    if not merge or rank >= FORWARD.index(best):
        return None
    target = best if merge["commit"] and released(merge["commit"]) else "Merged"
    return target if FORWARD.index(target) > rank else None


EVIDENCE_QUERY = "query($ids:[ID!]!){nodes(ids:$ids){... on Issue{id comments(last:100){nodes{body}}}}}"


def marked_sha(body):
    lines = (body or "").strip().splitlines()
    match = VERIFIED_RE.fullmatch(lines[-1].strip()) if lines else None
    return match.group(1) if match else None


def recorded(items):
    found = {}
    for start in range(0, len(items), 100):
        for node in filter(None, gql(EVIDENCE_QUERY, {"ids": [item["node"] for item in items[start:start + 100]]})["nodes"]):
            found[node["id"]] = {marked_sha((entry or {}).get("body")) for entry in (node.get("comments") or {}).get("nodes") or []} - {None}
    return found


def unproven(item, prs, released, shas):
    if item["decision"] or item["stage"] != "Verified":
        return None
    merge = latest_merge(item, prs)
    if shas and (not merge or merge["commit"] in shas):
        return None
    if merge:
        return "Released" if merge["commit"] and released(merge["commit"]) else "Merged"
    return "In progress" if any(pr["state"] == "OPEN" for pr in prs) else "Candidate"


REVERT_NOTE = ("Stage was Verified without valid recorded evidence, so it was set back to {stage}{reopened}. Verified needs evidence recorded with "
               "`gh sheriff task verify {task} --evidence-file <file>`, or by the automatic release verification for \"Release only\" work.")
SET_MUTATION = "mutation($issue:ID!,$fields:[IssueFieldCreateOrUpdateInput!]!){setIssueFieldValue(input:{issueId:$issue,issueFields:$fields}){issue{number}}}"
COMMENT_MUTATION = "mutation($id:ID!,$body:String!){addComment(input:{subjectId:$id,body:$body}){clientMutationId}}"
CLOSE_MUTATION = "mutation($id:ID!,$reason:IssueClosedStateReason){closeIssue(input:{issueId:$id,stateReason:$reason}){clientMutationId}}"
REOPEN_MUTATION = "mutation($id:ID!){reopenIssue(input:{issueId:$id}){clientMutationId}}"
CREATE_MUTATION = "mutation($input:CreateIssueInput!){createIssue(input:$input){issue{id number url}}}"
LABEL_QUERY = "query($owner:String!,$name:String!,$label:String!){repository(owner:$owner,name:$name){label(name:$label){id}}}"


def field_inputs(known, stage=None, verify=None, next_action=None):
    out = []
    for key, value, allowed in (("stage", stage, STAGES), ("verify", verify, VERIFY)):
        if value is None:
            continue
        if value not in allowed:
            raise WorkError(FIELDS[key] + " must be one of: " + ", ".join(allowed))
        options = {option["name"]: option["id"] for option in known["fields"][key].get("options") or []}
        if value not in options:
            raise WorkError("the issue field " + FIELDS[key] + " has no option " + value)
        out.append({"fieldId": known["fields"][key]["id"], "singleSelectOptionId": options[value]})
    if next_action is not None:
        if len(next_action) > TEXT_LIMIT:
            raise WorkError(FIELDS["next"] + " holds at most " + str(TEXT_LIMIT) + " characters; put the detail in a comment")
        field = known["fields"]["next"]["id"]
        out.append({"fieldId": field, "textValue": next_action} if next_action else {"fieldId": field, "delete": True})
    return out


def set_fields(known, item, **values):
    inputs = field_inputs(known, **values)
    if inputs:
        gql(SET_MUTATION, {"issue": item["node"], "fields": inputs})
    mirror_status(item["node"], values.get("stage"))


def comment(item, body, mark=AGENT_MARK):
    gql(COMMENT_MUTATION, {"id": item["node"], "body": body + "\n\n" + mark})


def close(item, reason="COMPLETED"):
    gql(CLOSE_MUTATION, {"id": item["node"], "reason": reason})


def reopen(item):
    gql(REOPEN_MUTATION, {"id": item["node"]})


def unchanged(item):
    node = gql(FRESH_QUERY, {"id": item["node"]})["node"] or {}
    values = field_values(node)
    return (node.get("state") == "OPEN", values.get(FIELDS["stage"]), values.get(FIELDS["verify"])) == (item["open"], item["stage"], item["verify"])


def sync(apply, only=None, pages=PAGES):
    known = schema()
    if known["missing"]:
        return {"skipped": "no issue fields " + ", ".join(known["missing"]) + " on this repository (gh sheriff install adds them)", "changes": []}
    if isinstance(known["remaining"], int) and known["remaining"] < RESERVE:
        return {"skipped": "only " + str(known["remaining"]) + " GraphQL points left this hour; a later run catches up", "changes": []}
    items = [item for item in issues("OPEN") + issues("CLOSED", pages)
             if item["id"] and (item["open"] or (item["reason"] == "COMPLETED" and item["stage"])) and (not only or item["id"] == only)]
    prs, branch = pull_requests(pages)
    released = release_check(branch)
    proofs = recorded([item for item in items if item["stage"] == "Verified" and not item["decision"]])
    changes = []
    for item in items:
        found = prs.get(item["id"], [])
        back = unproven(item, found, released, proofs.get(item["node"]))
        target = back or derive(item, found, released)
        if not target:
            continue
        changes.append(dict({"id": item["id"], "number": item["number"], "from": item["stage"], "to": target},
                            **({"why": "Verified without recorded evidence"} if back else {})))
        if not apply:
            continue
        if not unchanged(item):
            changes[-1]["skipped"] = "changed since it was read"
            continue
        if back:
            comment(item, REVERT_NOTE.format(stage=target, task=item["id"], reopened="" if item["open"] else " and the issue reopened"))
        if target == "Verified":
            commit = latest_merge(item, found)["commit"]
            comment(item, "Verified by release: " + released(commit) + ". Verify by is \"Release only\", so no live check is owed.",
                    VERIFIED_MARK.format(commit))
        set_fields(known, item, stage=target)
        if back and not item["open"]:
            reopen(item)
        if target == "Verified" and item["open"]:
            close(item)
    return {"changes": changes, "applied": bool(apply)}


def sync_text(result):
    if result.get("skipped"):
        return result["skipped"]
    return "\n".join(("" if result["applied"] else "would move ") + "#" + str(change["number"]) + " " + change["id"] + ": " +
                     str(change["from"] or "no stage") + " -> " + change["to"] + (" (" + change["why"] + ")" if change.get("why") else "") + (" (skipped: " + change["skipped"] + ")" if change.get("skipped") else "")
                     for change in result["changes"])


def make_id(title, taken):
    words = [word for word in re.findall(r"[A-Za-z0-9]+", title) if word.lower() not in STOP_WORDS]
    base = "-".join(word.upper() for word in words[:3])[:24].strip("-")
    base = base if len(base) > 1 else "TASK"
    base = base if base[0].isalpha() else "T-" + base
    candidate, number = base, 2
    while taken(candidate):
        candidate, number = base + "-" + str(number), number + 1
    return candidate


def label_id(name):
    found = gql(LABEL_QUERY, dict(names(), label=name))["repository"]["label"]
    if found:
        return found["id"]
    return api(["repos/" + REPO + "/labels", "-X", "POST", "-f", "name=" + name, "-f", "color=d4c5f9",
                "-f", "description=Waits for a person's decision"])["node_id"]


def read_text(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


BOARD_QUERY = """query($login:String!,$title:String!){repositoryOwner(login:$login){
 ... on ProjectV2Owner{projectsV2(first:20,query:$title){nodes{id title}}}}}"""
ADD_ITEM = """mutation($project:ID!,$content:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id}}}"""


def board_title(repo):
    return repo.split("/", 1)[1] + " work"


def board_id(title):
    try:
        owner = gql(BOARD_QUERY, {"login": REPO.split("/")[0], "title": title}).get("repositoryOwner") or {}
    except WorkError:
        return None
    return next((node["id"] for node in (owner.get("projectsV2") or {}).get("nodes") or [] if node.get("title") == title), None)


ITEMS_QUERY = """query($id:ID!,$cursor:String){node(id:$id){... on ProjectV2{title
 status:field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}
 items(first:100,after:$cursor){pageInfo{hasNextPage endCursor}
  nodes{id status:fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}
   content{... on Issue{issueFieldValues(first:20){nodes{__typename
    ... on IssueFieldSingleSelectValue{name field{... on IssueFieldSingleSelect{name}}}
    ... on IssueFieldTextValue{value field{... on IssueFieldText{name}}}}}}}}}}}}"""
STATUS_QUERY = """query($id:ID!){node(id:$id){... on ProjectV2{status:field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}}}}"""
ITEM_QUERY = """query($issue:ID!){node(id:$issue){... on Issue{projectItems(first:20){nodes{id project{id title}
 status:fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}"""
SET_STATUS = "mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,value:{singleSelectOptionId:$option}}){projectV2Item{id}}}"
WARNED = set()
BOARDS = {}


def warn(text):
    if text not in WARNED:
        WARNED.add(text)
        print("warning: " + text, file=sys.stderr)


def status_state(project, field):
    field = field or {}
    return {"project": project, "field": field.get("id"), "options": {option["name"]: option["id"] for option in field.get("options") or []}}


def board_state(project):
    if project not in BOARDS:
        try:
            state = status_state(project, gql(STATUS_QUERY, {"id": project})["node"].get("status"))
        except (WorkError, KeyError, TypeError, AttributeError):
            state = status_state(project, None)
        BOARDS[project] = state
        if not state["field"]:
            warn("the board has no readable Status field, so Status is not mirrored")
    return BOARDS[project]


def mirror_status(issue_node, stage):
    if not stage:
        return
    try:
        nodes = ((gql(ITEM_QUERY, {"issue": issue_node}).get("node") or {}).get("projectItems") or {}).get("nodes") or []
        for entry in nodes:
            if entry["project"]["title"] != board_title(REPO) or (entry.get("status") or {}).get("name") == stage:
                continue
            state = board_state(entry["project"]["id"])
            if not state["field"]:
                continue
            if stage not in state["options"]:
                warn("the board's Status has no option named " + stage + ", so Status is not mirrored (gh sheriff install adds it)")
                continue
            gql(SET_STATUS, {"project": state["project"], "item": entry["id"], "field": state["field"], "option": state["options"][stage]})
    except WorkError as error:
        warn("the board refused the Status write, so Status is not mirrored: " + str(error)[:120])


def board_status(title, apply):
    board = board_id(title)
    if not board:
        raise WorkError("no board '" + title + "' owned by " + REPO.split("/")[0] + ", or this token cannot read projects (gh auth refresh -s project)")
    cursor, rows, total = None, [], 0
    while True:
        node = gql(ITEMS_QUERY, {"id": board, "cursor": cursor})["node"]
        total += len(node["items"]["nodes"])
        for entry in node["items"]["nodes"]:
            stage = field_values(entry.get("content") or {}).get(FIELDS["stage"])
            if stage and (entry.get("status") or {}).get("name") != stage:
                rows.append((entry["id"], stage))
        if not node["items"]["pageInfo"]["hasNextPage"]:
            break
        cursor = node["items"]["pageInfo"]["endCursor"]
    state = status_state(board, node.get("status"))
    if not state["field"]:
        raise WorkError("the board has no Status field")
    counts = {}
    for _, stage in rows:
        counts[stage] = counts.get(stage, 0) + 1
    if apply:
        for entry_id, stage in rows:
            if stage in state["options"]:
                gql(SET_STATUS, {"project": board, "item": entry_id, "field": state["field"], "option": state["options"][stage]})
    return {"board": node["title"], "items": total, "differ": len(rows), "by_stage": counts,
            "no_status_option": sorted(stage for stage in counts if stage not in state["options"]), "applied": bool(apply)}


def add_to_board(board, items):
    added = 0
    for item in items:
        gql(ADD_ITEM, {"project": board, "content": item["node"]})
        added += 1
    return added


def new_task(known, args):
    body = read_text(args.body_file) if args.body_file else ""
    if len(body) > BODY_LIMIT:
        raise WorkError("the body is " + str(len(body)) + " characters; an issue holds " + str(BODY_LIMIT))
    open_ids = {item["id"] for item in issues("OPEN") if item["id"]}

    def taken(task):
        return task in open_ids or bool(closed_matches(task))
    if args.id:
        if not ID_RE.fullmatch(args.id):
            raise WorkError("a task id is capitals, digits and hyphens, starting with a capital")
        if taken(args.id):
            raise WorkError(args.id + " already names an issue")
    task = args.id or make_id(args.title, taken)
    decision = args.type == "Decision"
    payload = {"repositoryId": known["repo"], "title": (task + ": " + args.title.strip())[:256], "body": body,
               "issueFields": field_inputs(known, **({} if decision else {"stage": "Candidate", "verify": args.verify}))}
    if args.type in known["types"]:
        payload["issueTypeId"] = known["types"][args.type]
    else:
        payload["labelIds"] = [label_id(args.type.lower())]
    board = board_id(args.project or board_title(REPO))
    if board:
        payload["projectV2Ids"] = [board]
    made = gql(CREATE_MUTATION, {"input": payload})["createIssue"]["issue"]
    if not decision:
        mirror_status(made["id"], "Candidate")
    return {"id": task, "number": made["number"], "url": made["url"], "on_board": bool(board)}


def blocker_met(blocker, by_number):
    return not blocker["open"] or (by_number.get(blocker["number"]) or {}).get("stage") in ("Released", "Verified")


def overview(items):
    by_number = {item["number"]: item for item in items if item["open"]}
    work = [item for item in items if item["open"] and not item["decision"] and (item["id"] or item["stage"])]
    return {"replies_waiting": [item for item in items if item["open"] and item["awaits_reply"]],
            "needs_you": [item for item in items if item["open"] and item["decision"]],
            "blocked": [item for item in work if item["stage"] in ("Candidate", "In progress", None)
                        and any(not blocker_met(blocker, by_number) for blocker in item["blocked_by"])],
            "stages": {stage: [item for item in work if item["stage"] == stage] for stage in STAGES},
            "no_stage": [item for item in work if item["stage"] not in STAGES]}


def render(view):
    sections = [("A person commented last: read it and reply", view["replies_waiting"]), ("Needs human", view["needs_you"]),
                ("Blocked", view["blocked"])] + [(stage, view["stages"][stage]) for stage in STAGES] + [("No stage", view["no_stage"])]
    lines = []
    for heading, rows in sections:
        lines.append(heading + ": " + str(len(rows)) + (" (first " + str(SHOWN) + " shown; --json lists all)" if len(rows) > SHOWN else ""))
        for row in rows[:SHOWN]:
            lines.append("  #" + str(row["number"]) + " " + (row["id"] or "-") + ": " + row["title"][:90] + ("  -> " + row["next"] if row.get("next") else ""))
    return "\n".join(lines)


def run(args):
    use_repo(args.repo)
    command = args.task_command
    if command == "sync":
        return sync_text(sync(args.apply, args.task, args.pages))
    if command == "board-status":
        return board_status(args.project or board_title(REPO), args.apply)
    if command == "status":
        view = overview(issues("OPEN"))
        return view if args.json else render(view)
    if command == "reply":
        body = read_text(args.body_file).strip()
        if not body:
            raise WorkError("the reply is empty")
        item = lookup(args.task)
        comment(item, body)
        return {"ok": True, "number": item["number"]}
    known = require(schema())
    if command == "new":
        return new_task(known, args)
    item = lookup(args.task)
    if not item["open"] and item["reason"] != "COMPLETED":
        raise WorkError("#" + str(item["number"]) + " is closed as " + str(item["reason"] or "not planned").lower().replace("_", " "))
    if command == "set":
        if args.stage is None and args.verify is None and args.next is None:
            raise WorkError("nothing to set: pass --stage, --next or --verify")
        if args.stage == "Verified":
            raise WorkError("use the verify command, which records the evidence")
        if args.stage in DERIVED and item["id"] in pull_requests(merged=False)[0]:
            raise WorkError(item["id"] + " has an open pull request, so its stage follows the pull request and the release; do not set " + args.stage + " by hand")
        set_fields(known, item, stage=args.stage, verify=args.verify, next_action=args.next)
    if command == "verify":
        if item["stage"] != "Released":
            raise WorkError("#" + str(item["number"]) + " is at " + str(item["stage"]) + "; only released work can be verified")
        evidence = read_text(args.evidence_file).strip()
        if len(evidence) < EVIDENCE_MIN:
            raise WorkError("the evidence must say what was exercised in production and what was observed")
        prs, branch = pull_requests()
        merge = latest_merge(item, prs.get(item["id"], []))
        if not (merge and merge["commit"] and release_check(branch)(merge["commit"])):
            raise WorkError("#" + str(item["number"]) + " has no merge into the default branch known to be released, so there is no released commit to verify")
        comment(item, "## Verified in production\n\n" + evidence, VERIFIED_MARK.format(merge["commit"]))
        set_fields(known, item, stage="Verified", next_action="")
        if item["open"]:
            close(item)
    return {"ok": True, "number": item["number"]}


def add_commands(parser):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="owner/name (default: the repository in the current folder)")
    sub = parser.add_subparsers(dest="task_command", required=True)
    new = sub.add_parser("new", parents=[common], help="open a task issue at Stage Candidate")
    new.add_argument("title")
    new.add_argument("--id", help="task id: capitals, digits and hyphens (default: made from the title)")
    new.add_argument("--type", choices=TYPES, default="Task")
    new.add_argument("--verify", choices=VERIFY, default="Live check", help="how the work is verified (default: Live check)")
    new.add_argument("--body-file", help="the issue body; - reads standard input")
    new.add_argument("--project", help="the board the task joins (default: the repository's name plus ' work')")
    change = sub.add_parser("set", parents=[common], help="set what pull requests and releases cannot show")
    change.add_argument("task", help="task id or #issue number")
    change.add_argument("--stage", choices=STAGES)
    change.add_argument("--next", help="one line: who acts next and what ('' clears it)")
    change.add_argument("--verify", choices=VERIFY)
    verify = sub.add_parser("verify", parents=[common], help="record production evidence for the released merge, set Verified and close the issue")
    verify.add_argument("task", help="task id or #issue number")
    verify.add_argument("--evidence-file", required=True, help="what was exercised in production and what was observed; - reads standard input")
    status = sub.add_parser("status", parents=[common], help="open work by stage, and issues where a person commented last")
    status.add_argument("--json", action="store_true")
    reply = sub.add_parser("reply", parents=[common], help="answer a person's comment on an issue")
    reply.add_argument("task", help="task id or #issue number")
    reply.add_argument("--body-file", required=True, help="- reads standard input")
    synced = sub.add_parser("sync", parents=[common], help="move stages from pull request and release facts (what CI runs)")
    synced.add_argument("--apply", action="store_true", help="write the changes (default: only report them)")
    synced.add_argument("--task", help="only this task id")
    synced.add_argument("--pages", type=int, default=PAGES, help="pages of 100 recent merged pull requests and closed issues to read")
    cleared = sub.add_parser("board-status", parents=[common], help="backfill: set the board's built-in Status to each item's Stage where they differ")
    cleared.add_argument("--apply", action="store_true", help="write the changes (default: only report them)")
    cleared.add_argument("--project", help="the board (default: the repository's name plus ' work')")
    return parser


def run_cli(args):
    try:
        result = run(args)
    except WorkError as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
    if result:
        print(result if isinstance(result, str) else json.dumps(result, indent=1), flush=True)
    return 0


def main(argv=None):
    return run_cli(add_commands(argparse.ArgumentParser(prog="work.py", description=__doc__)).parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
