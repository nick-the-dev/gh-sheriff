#!/usr/bin/env python3
import datetime
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
