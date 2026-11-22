#!/usr/bin/env python3
import datetime
import importlib.util
import json
import os
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("plan", ROOT / "scripts/plan.py")
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)
APP = "acme-sheriff"
HEAD, BASE = "a" * 40, "c" * 40
USAGE = {"input": 10, "cache_read": 0, "cache_write": 0, "output": 5, "reasoning": 0, "cost_usd": 0.0}
NOW = datetime.datetime(2026, 11, 22, 12, 0, tzinfo=datetime.timezone.utc)


def note(author, body, at="2026-11-22T10:00:00Z", bot=False, editor=None, id=1):
    return {"id": id, "author": author, "bot": bot, "body": body, "created": at, "edited": editor is not None, "editor": editor or ""}


def planned(v, at="2026-11-22T10:00:00Z", superseded=None, editor=None):
    tail = "" if superseded is None else " superseded-by=" + str(superseded)
    body = "<!-- sheriff-plan v=" + str(v) + " base=" + BASE + tail + " -->\n**Sheriff plan v" + str(v) + "**\n## Problem\nx"
    return note(APP + "[bot]", body, at, True, editor, 100 + v)


def task(comments=(), title="LOGIN-FIX: fix login", author="boss", bot=False, open=True, decision=False):
    return {"number": 12, "title": title, "body": "", "open": open, "author": author, "author_bot": bot,
            "decision": decision, "comments": list(comments)}


def gql_page(nodes, more=False, **issue):
    body = {"number": 12, "title": "LOGIN-FIX: fix login", "body": "b", "state": "OPEN", "author": {"login": "boss", "__typename": "User"},
            "issueType": None, "labels": {"nodes": []},
            "comments": {"pageInfo": {"hasNextPage": more, "endCursor": "c1"}, "nodes": nodes}}
    body.update(issue)
    return {"data": {"repository": {"issue": body}}}


def ref(number, repo="o/r"):
    owner, name = repo.split("/")
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


def fake_fetch(titles, open_rows):
    def fetch(args):
        if "graphql" in args:
            number = int(next(a for a in args if a.startswith("number=")).split("=")[1])
            title = titles[number]
            title, kind = title if isinstance(title, tuple) else (title, None)
            return gql_page([], title=title, number=number, issueType={"name": kind} if kind else None)
        return [open_rows]
    return fetch


class FactsTests(unittest.TestCase):
    def test_task_ids_come_from_the_title_prefix(self):
        self.assertEqual(plan.task_id("LOGIN-FIX: x"), "LOGIN-FIX")
        self.assertEqual(plan.task_id("LOGIN-FIX #12: x"), "LOGIN-FIX")
        for title in ("login: x", "Fix LOGIN-FIX: x", "LOGIN-FIX x", ""):
            self.assertIsNone(plan.task_id(title), title)

    def test_only_open_task_issues_that_are_not_decisions_are_plannable(self):
        self.assertEqual(plan.plannable(task()), "")
        self.assertEqual(plan.plannable(task(title="Login broken")), "not a task issue")
        self.assertEqual(plan.plannable(task(decision=True)), "a decision")
        self.assertEqual(plan.plannable(task(open=False)), "closed")

    def test_the_current_plan_is_the_apps_newest_unedited_plan(self):
        current = lambda *comments: (plan.current_plan(list(comments), APP) or {}).get("version")
        self.assertEqual(current(planned(1)), 1)
        self.assertIsNone(current(dict(planned(1), author="writer", bot=False)))
        self.assertIsNone(current(planned(1, editor="writer")))
        self.assertEqual(current(planned(1, editor=APP + "[bot]")), 1)
        self.assertEqual(current(planned(1, superseded=2), planned(2)), 2)
        self.assertIsNone(current(planned(1, superseded=2)))
        self.assertIsNone(current(planned(1, superseded=2), planned(2, editor="writer")))
        self.assertIsNone(current(note(APP, "intro\n" + planned(1)["body"], bot=True)))

    def test_issue_comments_are_read_across_pages(self):
        pages = [gql_page([{"databaseId": 1, "body": "hi", "createdAt": "2026-11-22T09:00:00Z", "lastEditedAt": None,
                            "author": {"login": "writer", "__typename": "User"}, "editor": None}], more=True),
                 gql_page([{"databaseId": 2, "body": planned(1)["body"], "createdAt": "2026-11-22T10:00:00Z", "lastEditedAt": None,
                            "author": {"login": APP, "__typename": "Bot"}, "editor": None}])]
        calls = []
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            issue = plan.read_issue(12, lambda args: calls.append(args) or pages[len(calls) - 1])
        self.assertEqual([c["id"] for c in issue["comments"]], [1, 2])
        self.assertTrue(issue["comments"][1]["bot"])
        self.assertEqual((issue["comments"][0]["author"], issue["comments"][1]["author"]), ("writer", APP + "[bot]"))
        self.assertIn("cursor=c1", " ".join(calls[1]))
        self.assertEqual(plan.current_plan(issue["comments"], APP)["version"], 1)

    def test_owner_and_name_are_sent_as_strings(self):
        calls = []
        with mock.patch.dict(os.environ, {"GH_REPO": "123/456"}):
            plan.read_issue(12, lambda args: calls.append(args) or gql_page([]))
        joined = " ".join(calls[0])
        self.assertIn("-f owner=123", joined)
        self.assertIn("-f name=456", joined)

    def test_repository_names_match_case_insensitively(self):
        view = {"title": "x", "closingIssuesReferences": [ref(5, "Owner/Repo")]}
        with mock.patch.dict(os.environ, {"GH_REPO": "owner/repo"}):
            found = plan.task_issue(view, "owner/repo", fake_fetch({5: "LOGIN-FIX: a"}, []))
        self.assertEqual(found["number"], 5)

    def test_a_missing_issue_reads_as_none(self):
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            self.assertIsNone(plan.read_issue(12, lambda args: {"data": {"repository": {"issue": None}}}))

    def test_an_unreadable_response_is_a_plan_error(self):
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            with self.assertRaises(plan.PlanError):
                plan.read_issue(12, lambda args: ["nope"])

    def test_decisions_are_found_by_issue_type_or_label(self):
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            typed = plan.read_issue(12, lambda args: gql_page([], issueType={"name": "Decision"}))
            labelled = plan.read_issue(12, lambda args: gql_page([], labels={"nodes": [{"name": "decision"}]}))
            plain = plan.read_issue(12, lambda args: gql_page([]))
        self.assertEqual((typed["decision"], labelled["decision"], plain["decision"]), (True, True, False))
        self.assertEqual((plain["open"], plain["author"], plain["author_bot"]), (True, "boss", False))

    def test_a_closing_reference_to_a_task_issue_wins(self):
        view = {"title": "fix(x): y (OTHER)", "closingIssuesReferences": [ref(5)]}
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            found = plan.task_issue(view, "o/r", fake_fetch({5: "LOGIN-FIX: a"}, open_rows=[]))
        self.assertEqual(found["number"], 5)

    def test_other_repositories_and_non_task_references_fall_back_to_the_title(self):
        view = {"title": "fix(x): y (LOGIN-FIX)", "closingIssuesReferences": [ref(3, "other/r"), ref(4)]}
        rows = [{"number": 9, "title": "LOGIN-FIX: a"}, {"number": 7, "title": "LOGIN-FIX #2: b"},
                {"number": 6, "title": "LOGIN-FIX: c"}, {"number": 5, "title": "LOGIN-FIX: pr", "pull_request": {}}]
        with mock.patch.dict(os.environ, {"GH_REPO": "o/r"}):
            found = plan.task_issue(view, "o/r", fake_fetch({4: "Login broken", 6: ("LOGIN-FIX: c", "Decision"), 7: "LOGIN-FIX #2: b", 9: "LOGIN-FIX: a"}, rows))
        self.assertEqual(found["number"], 7)

    def test_a_pull_request_without_reference_or_task_id_has_no_task_issue(self):
        fetch = mock.Mock(side_effect=AssertionError("nothing to read"))
        self.assertIsNone(plan.task_issue({"title": "Install Sheriff", "closingIssuesReferences": []}, "o/r", fetch))

    def test_review_keys_bind_the_plan_version(self):
        self.assertEqual(plan.review_key(BASE, 0), BASE)
        self.assertEqual(plan.review_key(BASE, 2), BASE + "/plan-2")
        self.assertTrue(plan.keyed_to(BASE + "/plan-2", 2))
        self.assertFalse(plan.keyed_to(BASE + "/plan-12", 2))
        self.assertFalse(plan.keyed_to(BASE, 2))

    def test_linked_pull_requests_are_found_by_closing_reference_or_title_id(self):
        rows = [{"number": 21, "title": "a (LOGIN-FIX)", "closingIssuesReferences": []},
                {"number": 22, "title": "b", "closingIssuesReferences": [ref(12)]},
                {"number": 23, "title": "c", "closingIssuesReferences": [ref(12, "other/r")]},
                {"number": 24, "title": "d (OTHER)", "closingIssuesReferences": []}]
        self.assertEqual(plan.linked_prs(12, "LOGIN-FIX", rows, "o/r"), [21, 22])

ROLES = {"boss": "maintain", "chief": "admin"}


def decide(issue, rounds=3, opened=True):
    return plan.decide(issue, APP, lambda login: ROLES.get(login, "write"), ("helper",), ("friend",), rounds, NOW, opened)


class DecideTests(unittest.TestCase):
    def test_a_task_opened_by_a_maintainer_a_bot_or_a_listed_login_is_planned_at_once(self):
        for author, bot in (("boss", False), ("chief", False), ("lane[bot]", True), ("helper", False)):
            with self.subTest(author=author):
                self.assertEqual({k: decide(task(author=author, bot=bot))[k] for k in ("action", "version")}, {"action": "plan", "version": 1})

    def test_an_outsiders_issue_waits_for_a_trusted_command(self):
        self.assertEqual(decide(task(author="stranger"))["why"], "waiting for /sheriff plan from a maintainer")
        self.assertEqual(decide(task([note("stranger", "/sheriff plan")], author="stranger"))["action"], "skip")
        self.assertEqual(decide(task([note("boss", "/sheriff plan")], author="stranger"))["action"], "plan")
        self.assertEqual(decide(task([note("boss", "/sheriff plan", editor="boss")], author="stranger"))["action"], "skip")

    def test_a_planned_issue_is_not_planned_again_without_a_replan(self):
        self.assertEqual(decide(task([planned(1), note("boss", "/sheriff plan", "2026-11-22T11:00:00Z")]))["why"], "already planned")

    def test_a_replan_from_the_trusted_set_writes_the_next_version_with_its_reason(self):
        result = decide(task([planned(1), note("lane[bot]", "/sheriff replan the cache is shared", "2026-11-22T11:00:00Z", bot=True)]))
        self.assertEqual((result["action"], result["version"], result["reason"], result["requester"]), ("plan", 2, "the cache is shared", "lane[bot]"))
        self.assertEqual(decide(task([note("boss", "/sheriff replan x", "2026-11-22T09:00:00Z"), planned(1)]))["why"], "already planned")
        self.assertEqual(decide(task([planned(1), note("stranger", "/sheriff replan x", "2026-11-22T11:00:00Z")]))["why"], "already planned")

    def test_a_fresh_reservation_blocks_a_second_run_and_a_stale_one_does_not(self):
        fresh = note(APP, "<!-- sheriff-plan-start v=1 -->\nwriting", "2026-11-22T11:45:00Z", bot=True)
        stale = dict(fresh, created="2026-11-22T11:20:00Z")
        self.assertEqual(decide(task([fresh]))["why"], "a plan is being written")
        self.assertEqual(decide(task([stale]))["action"], "plan")

    def test_a_command_posted_while_a_plan_was_written_is_pending_afterwards(self):
        comments = [planned(1, "2026-11-22T09:00:00Z"), note("boss", "/sheriff replan a", "2026-11-22T10:00:00Z"),
                    dict(planned(2, "2026-11-22T10:01:00Z"), editor=APP), note("boss", "/sheriff replan b", "2026-11-22T10:05:00Z")]
        result = decide(task(comments))
        self.assertEqual((result["action"], result["version"], result["reason"]), ("plan", 3, "b"))

    def test_the_budget_stops_the_fourth_plan_once_and_an_unlock_allows_one_more(self):
        used = [planned(1, "2026-11-22T08:00:00Z"), planned(2, "2026-11-22T08:30:00Z"), planned(3, "2026-11-22T09:00:00Z")]
        replan = note("lane[bot]", "/sheriff replan again", "2026-11-22T10:00:00Z", bot=True)
        noted = note(APP, "limit\n\n<!-- sheriff-plan-budget -->", "2026-11-22T10:01:00Z", bot=True)
        self.assertEqual(decide(task(used + [replan]))["action"], "budget")
        self.assertEqual(decide(task(used + [replan, noted]))["why"], "plan budget used")
        for unlocker, action in (("boss", "plan"), ("friend", "plan"), ("lane[bot]", "skip"), ("stranger", "skip")):
            with self.subTest(unlocker=unlocker):
                unlock = note(unlocker, "/sheriff plan", "2026-11-22T10:05:00Z", bot=unlocker.endswith("[bot]"))
                self.assertEqual(decide(task(used + [replan, noted, unlock]))["action"], action)
        own = note("boss", "/sheriff replan mine", "2026-11-22T10:00:00Z")
        self.assertEqual(decide(task(used + [own, noted, note("boss", "/sheriff plan", "2026-11-22T10:05:00Z")]))["action"], "skip")

    def test_a_failure_note_is_not_a_plan_and_the_next_command_retries(self):
        failed = note(APP, "<!-- sheriff-plan-failed v=1 -->\nno result", "2026-11-22T10:00:00Z", bot=True)
        result = decide(task([failed, note("boss", "/sheriff plan", "2026-11-22T11:00:00Z")]))
        self.assertEqual((result["action"], result["version"]), ("plan", 1))

    def test_a_request_older_than_the_newest_sheriff_comment_is_not_answered_again(self):
        failed = note(APP, "<!-- sheriff-plan-failed v=1 -->\nno result", "2026-11-22T10:00:00Z", bot=True)
        self.assertEqual(decide(task([note("boss", "/sheriff plan", "2026-11-22T09:00:00Z"), failed]))["action"], "skip")
        self.assertEqual(decide(task([failed], author="boss"))["action"], "skip")
        self.assertEqual(decide(task([note("boss", "/sheriff replan x", "2026-11-22T09:00:00Z"), planned(1, "2026-11-22T09:30:00Z")]))["why"], "already planned")

    def test_two_failures_in_a_row_act_as_the_budget_and_an_unlock_allows_a_retry(self):
        failures = [note(APP, "<!-- sheriff-plan-failed v=1 -->\nx", "2026-11-22T10:00:00Z", bot=True),
                    note(APP, "<!-- sheriff-plan-failed v=1 -->\nx", "2026-11-22T10:30:00Z", bot=True)]
        request = note("boss", "/sheriff plan", "2026-11-22T11:00:00Z")
        self.assertEqual(decide(task(failures + [request]))["action"], "budget")
        noted = note(APP, "limit\n\n<!-- sheriff-plan-budget -->", "2026-11-22T11:01:00Z", bot=True)
        self.assertEqual(decide(task(failures + [request, noted]))["why"], "plan budget used")
        self.assertEqual(decide(task(failures + [request, noted, note("boss", "/sheriff plan", "2026-11-22T11:05:00Z")]))["action"], "plan")
        self.assertEqual(decide(task(failures[:1] + [request]))["action"], "plan")
        self.assertEqual(decide(task([failures[0], planned(1, "2026-11-22T10:15:00Z"), failures[1], request]))["action"], "skip")

    def test_decisions_closed_and_untitled_issues_are_never_planned(self):
        for issue, why in ((task(decision=True), "a decision"), (task(open=False), "closed"), (task(title="Login broken"), "not a task issue")):
            self.assertEqual(decide(issue)["why"], why)

    def test_an_unlock_carries_the_replan_that_hit_the_budget(self):
        used = [planned(1, "2026-11-22T08:00:00Z"), planned(2, "2026-11-22T08:30:00Z"), planned(3, "2026-11-22T09:00:00Z")]
        replan = note("lane[bot]", "/sheriff replan again", "2026-11-22T10:00:00Z", bot=True)
        noted = note(APP, "limit\n\n<!-- sheriff-plan-budget -->", "2026-11-22T10:01:00Z", bot=True)
        result = decide(task(used + [replan, noted, note("boss", "/sheriff plan", "2026-11-22T10:05:00Z")]))
        self.assertEqual((result["version"], result["reason"], result["requester"]), (4, "again", "lane[bot]"))

    def test_a_maintainer_cannot_unlock_a_replan_they_posted_after_the_budget_note(self):
        used = [planned(1, "2026-11-22T08:00:00Z"), planned(2, "2026-11-22T08:30:00Z"), planned(3, "2026-11-22T09:00:00Z")]
        noted = note(APP, "limit\n\n<!-- sheriff-plan-budget -->", "2026-11-22T10:01:00Z", bot=True)
        mine = note("chief", "/sheriff replan again", "2026-11-22T10:02:00Z")
        unlock = note("chief", "/sheriff plan", "2026-11-22T10:03:00Z")
        self.assertEqual(decide(task(used + [noted, mine, unlock]))["why"], "plan budget used")
        other = note("boss", "/sheriff plan", "2026-11-22T10:04:00Z")
        self.assertEqual(decide(task(used + [noted, mine, unlock, other]))["action"], "plan")

    def test_a_trusted_author_triggers_a_plan_only_on_the_opened_event(self):
        self.assertEqual(decide(task(), opened=False)["why"], "waiting for /sheriff plan from a maintainer")
        self.assertEqual(decide(task(), opened=True)["version"], 1)
        self.assertEqual(decide(task([note("boss", "/sheriff plan")], author="stranger"), opened=False)["action"], "plan")

    def test_a_long_reason_is_cut(self):
        result = decide(task([planned(1), note("boss", "/sheriff replan " + "x" * 5000, "2026-11-22T11:00:00Z")]))
        self.assertEqual(len(result["reason"]), plan.REASON_LIMIT)


class ReserveTests(unittest.TestCase):
    def reserve(self, issue, login=APP + "[bot]", event="opened"):
        outs, posts = [], []

        def fake(args):
            if "/permission" in args[-1]:
                return {"role_name": ROLES.get(args[-1].split("/")[-2], "write")}
            if args[:3] == ["api", "-X", "POST"]:
                posts.append({"path": args[3], "body": args[args.index("-f") + 1][5:]})
                return {"id": 55}
            raise AssertionError(args)
        with mock.patch.object(plan, "read_issue", return_value=issue), mock.patch.object(plan, "gh_json", side_effect=fake), \
             mock.patch.object(plan, "output", side_effect=lambda **values: outs.append(values)), mock.patch("sys.stderr"), \
             mock.patch.dict(os.environ, {"GH_REPO": "o/r", "GH_ACTOR_LOGIN": login, "GITHUB_SERVER_URL": "https://h", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9"}):
            code = plan.main(["reserve", "--issue", "12", "--rounds", "3", "--ack-authors", "", "--unlock-authors", "", "--event", event])
        return code, outs, posts

    def test_a_plan_is_reserved_with_a_start_comment_before_any_model_runs(self):
        code, outs, posts = self.reserve(task())
        self.assertEqual((code, len(posts)), (0, 1))
        self.assertEqual(posts[0]["path"], "repos/{owner}/{repo}/issues/12/comments")
        self.assertEqual(posts[0]["body"].splitlines()[0], "<!-- sheriff-plan-start v=1 -->")
        self.assertIn("Sheriff is writing plan v1 for this issue", posts[0]["body"])
        self.assertIn("https://h/o/r/actions/runs/9", posts[0]["body"])
        self.assertEqual({k: outs[-1][k] for k in ("skip", "version", "comment_id", "app")}, {"skip": "", "version": 1, "comment_id": "55", "app": APP})
        self.assertEqual(json.loads(outs[-1]["request"]), {"version": 1, "reason": "", "requester": ""})

    def test_a_used_budget_posts_one_note_and_reserves_nothing(self):
        code, outs, posts = self.reserve(task([planned(1), planned(2), planned(3), note("lane[bot]", "/sheriff replan x", "2026-11-22T11:00:00Z", bot=True)]))
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"].splitlines()[-1], "<!-- sheriff-plan-budget -->")
        self.assertIn("written 3 plans", posts[0]["body"])
        self.assertEqual((outs[-1]["skip"], outs[-1].get("comment_id", "")), ("plan budget used", ""))

    def test_two_failures_post_the_failure_budget_note(self):
        failed = lambda at: note(APP + "[bot]", "<!-- sheriff-plan-failed v=1 -->\nx", at, True)
        code, outs, posts = self.reserve(task([failed("2026-11-22T10:00:00Z"), failed("2026-11-22T10:30:00Z"), note("boss", "/sheriff plan", "2026-11-22T11:00:00Z")]))
        self.assertIn("failed to write a plan 2 times in a row", posts[0]["body"])
        self.assertEqual(posts[0]["body"].splitlines()[-1], "<!-- sheriff-plan-budget -->")
        self.assertEqual(outs[-1]["skip"], "plan budget used")

    def test_without_the_opened_event_a_trusted_authors_issue_is_not_reserved(self):
        code, outs, posts = self.reserve(task(), event="created")
        self.assertEqual((code, posts, outs[-1]["skip"]), (0, [], "waiting for /sheriff plan from a maintainer"))

    def test_a_skip_posts_nothing_and_without_the_apps_login_nothing_runs(self):
        self.assertEqual(self.reserve(task([planned(1)]))[2], [])
        self.assertEqual(self.reserve(task(), login="")[0], 1)


class CommentTextTests(unittest.TestCase):
    BODY = "<!-- sheriff-plan v=2 base=" + BASE + " -->\n**Sheriff plan v2**\n## Problem\nx\n<!-- sheriff-usage {\"provider\": \"claude\", \"model\": \"m\", \"usage\": {\"input\": 1}} -->"

    def test_superseding_rewrites_only_the_header(self):
        old = plan.supersede(self.BODY, 3)
        self.assertEqual(old.splitlines()[:2], ["<!-- sheriff-plan v=2 base=" + BASE + " superseded-by=3 -->", "**Superseded by v3.**"])
        self.assertEqual(old.splitlines()[2:], self.BODY.splitlines()[1:])
        self.assertIsNone(plan.current_plan([note(APP + "[bot]", old, bot=True)], APP))

    def test_a_failure_note_is_a_marker_line_and_an_escaped_cut_reason(self):
        body = plan.failure_note(2, "bad <!-- sheriff-plan v=9 --> " + "x" * 400)
        self.assertEqual(body.splitlines()[0], "<!-- sheriff-plan-failed v=2 -->")
        self.assertIn("Sheriff could not write plan v2: bad &lt;!-- sheriff-plan v=9 -->", body)
        self.assertIn("Comment `/sheriff plan` (or `/sheriff replan <reason>`) to try again.", body)
        self.assertEqual(body.count("<!--"), 1)
        self.assertEqual(plan.plans([note(APP + "[bot]", body, bot=True)], APP), [])

    def test_escaping_cannot_push_a_failure_reason_past_the_limit(self):
        body = plan.failure_note(1, "<!--" * 100)
        reason = body.splitlines()[1].split(": ", 1)[1].rsplit(". Comment", 1)[0]
        self.assertEqual(len(reason), plan.FAILURE_LIMIT)

    def test_usage_is_read_from_the_last_line(self):
        self.assertEqual(plan.usage_of(self.BODY), {"provider": "claude", "model": "m", "usage": {"input": 1}})
        self.assertIsNone(plan.usage_of("x\n<!-- sheriff-usage not json -->"))
        self.assertIsNone(plan.usage_of("<!-- sheriff-usage {} -->\nmore"))
        self.assertIsNone(plan.usage_of(""))


if __name__ == "__main__":
    unittest.main()
