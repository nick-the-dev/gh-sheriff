#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gate_status_module", ROOT / "scripts" / "gate-status.py")
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)

HEAD = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
NOBODY = ()
ACK = "Answered.\n<!-- sheriff-ack -->"


def comment(author, body, at, kind="User"):
    return {"user": {"login": author, "type": kind}, "body": body, "created_at": at, "updated_at": at}


class AcknowledgementTests(unittest.TestCase):
    def test_a_persons_comment_blocks_until_a_bot_acknowledges_it(self):
        owner = comment("owner", "Why is this here?", "2026-11-21T10:00:00Z")
        ack = comment("agent[bot]", ACK, "2026-11-21T11:00:00Z", kind="Bot")
        state, description = status.merge_status(7, [owner], [], NOBODY, "")
        self.assertEqual(state, "failure")
        self.assertIn("unacknowledged comment from owner", description)
        self.assertEqual(status.merge_status(7, [owner, ack], [], NOBODY, "")[0], "success")

    def test_a_person_cannot_acknowledge_unless_listed(self):
        owner = comment("owner", "Why?", "2026-11-21T10:00:00Z")
        ack = comment("someone", ACK, "2026-11-21T11:00:00Z")
        self.assertEqual(status.merge_status(7, [owner, ack], [], NOBODY, "")[0], "failure")
        self.assertEqual(status.merge_status(7, [owner, ack], [], ("someone",), "")[0], "success")

    def test_the_agent_marker_no_longer_hides_a_persons_comment(self):
        marked = comment("owner", "Checkpoint\n<!-- sheriff-ack -->", "2026-11-21T10:00:00Z")
        self.assertEqual(status.merge_status(7, [marked], [], NOBODY, "")[0], "failure")

    def test_bots_never_block(self):
        rows = [comment("gitguardian[bot]", "scan", "2026-11-21T10:00:00Z", kind="Bot")]
        self.assertEqual(status.merge_status(7, rows, [], NOBODY, "")[0], "success")

    def test_a_comment_edited_after_the_ack_blocks_again(self):
        owner = comment("owner", "Why?", "2026-11-21T10:00:00Z")
        owner["updated_at"] = "2026-11-21T12:00:00Z"
        ack = comment("agent[bot]", ACK, "2026-11-21T11:00:00Z", kind="Bot")
        self.assertEqual(status.merge_status(7, [owner, ack], [], NOBODY, "")[0], "failure")

    def test_the_ack_must_be_the_comments_trailer(self):
        owner = comment("owner", "Why?", "2026-11-21T10:00:00Z")
        quoted = comment("agent[bot]", "<!-- sheriff-ack -->\nmore text", "2026-11-21T11:00:00Z", kind="Bot")
        self.assertEqual(status.merge_status(7, [owner, quoted], [], NOBODY, "")[0], "failure")


class MergeStatusTests(unittest.TestCase):
    def test_healthy_production_and_no_comment_is_success(self):
        self.assertEqual(status.merge_status(7, [], [], NOBODY, "")[0], "success")

    def test_deploy_still_landing_is_pending(self):
        landing = ["production does not serve the default branch abc yet", "deploy run 9 for the default branch has not concluded"]
        self.assertEqual(status.merge_status(7, [], landing, NOBODY, ""), ("pending", landing[0]))

    def test_unhealthy_production_is_failure(self):
        self.assertEqual(status.merge_status(7, [], ["production health endpoint is not 200"], NOBODY, "")[0], "failure")

    def test_requested_changes_block(self):
        self.assertEqual(status.merge_status(7, [], [], NOBODY, "", "CHANGES_REQUESTED"), ("failure", "a reviewer requested changes"))

    def test_success_text_does_not_claim_unconfigured_production_checks(self):
        self.assertEqual(status.merge_status(7, [], [], NOBODY, "", "", False), ("success", "comments answered; no production checks configured"))

    def test_override_skips_production_for_that_pull_request_only(self):
        broken = ["production health endpoint is not 200"]
        self.assertEqual(status.merge_status(7, [], broken, NOBODY, "7")[0], "success")
        self.assertEqual(status.merge_status(8, [], broken, NOBODY, "7")[0], "failure")
        owner = comment("owner", "wait", "2026-11-21T10:00:00Z")
        self.assertEqual(status.merge_status(7, [owner], [], NOBODY, "7")[0], "failure")


class ProductionTests(unittest.TestCase):
    def run_checks(self, version, health, runs, main_sha=HEAD):
        def fake_get(url):
            return version if url.endswith("/version.json") else health

        def fake_gh(args):
            return main_sha + "\n" if args[:2] == ["api", "repos/{owner}/{repo}/commits/HEAD"] else json.dumps(runs)

        with mock.patch.object(status, "http_get", side_effect=fake_get), mock.patch.object(status, "gh", side_effect=fake_gh):
            return status.production_failures("https://example.test/version.json", "https://example.test/health", "deploy.yml")

    def test_serving_the_default_branch_healthy_and_deployed_is_clean(self):
        runs = [{"databaseId": 1, "status": "completed", "conclusion": "success", "createdAt": "2026-11-21T10:00:00Z"}]
        self.assertEqual(self.run_checks((200, json.dumps({"sha": HEAD}).encode()), (200, b""), runs), [])

    def test_older_build_and_running_deploy_are_pending(self):
        runs = [{"databaseId": 1, "status": "completed", "conclusion": "success", "createdAt": "2026-11-21T09:00:00Z"},
                {"databaseId": 2, "status": "in_progress", "conclusion": None, "createdAt": "2026-11-21T10:00:00Z"}]
        failures = self.run_checks((200, json.dumps({"sha": OLD}).encode()), (200, b""), runs)
        self.assertEqual(status.merge_status(7, [], failures, NOBODY, "")[0], "pending")

    def test_down_production_and_failed_deploy_are_failures(self):
        runs = [{"databaseId": 4, "status": "completed", "conclusion": "failure", "createdAt": "2026-11-21T10:00:00Z"}]
        failures = self.run_checks((None, b""), (503, b""), runs)
        self.assertEqual(len(failures), 3)
        self.assertEqual(status.merge_status(7, [], failures, NOBODY, "")[0], "failure")

    def test_no_production_settings_means_no_production_checks(self):
        with mock.patch.object(status, "gh", side_effect=AssertionError("no gh call expected")):
            self.assertEqual(status.production_failures("", "", ""), [])


class ReviewDecisionTests(unittest.TestCase):
    def review(self, login, state, kind="User"):
        return {"user": {"login": login, "type": kind}, "state": state, "body": ""}

    def test_a_reviewer_whose_latest_review_requests_changes_blocks(self):
        self.assertTrue(status.changes_requested([self.review("owner", "CHANGES_REQUESTED")]))
        self.assertFalse(status.changes_requested([self.review("a", "CHANGES_REQUESTED"), self.review("a", "APPROVED")]))
        self.assertFalse(status.changes_requested([self.review("bot[bot]", "CHANGES_REQUESTED", "Bot")]))


class EvaluateTests(unittest.TestCase):
    def evaluate(self, view, pages=None, current=None):
        posted = []

        def fake_gh_json(args):
            return current if "/statuses?" in args[1] else view

        with mock.patch.object(status, "gh_json", side_effect=fake_gh_json), \
             mock.patch.object(status, "gh_pages", side_effect=pages or (lambda path: [])), \
             mock.patch.object(status, "gh", side_effect=lambda args: posted.append(args) or ""), \
             mock.patch.dict(os.environ, {"GH_ACTOR_LOGIN": "gate[bot]"}):
            result = status.publish(status.judge(7, [], NOBODY, "", False, True, "main"), False)
        return result, posted

    def test_only_merge_gate_is_posted(self):
        result, posted = self.evaluate({"state": "OPEN", "headRefOid": HEAD, "baseRefName": "main"}, current=[])
        self.assertEqual([item["context"] for item in result["statuses"]], ["Merge gate"])
        self.assertTrue(all("context=Review" not in args for args in posted))

    def test_pull_requests_into_other_branches_are_not_judged(self):
        result, posted = self.evaluate({"state": "OPEN", "headRefOid": HEAD, "baseRefName": "feature"})
        self.assertEqual(result["ignored"], "not into the default branch")
        self.assertEqual(posted, [])

    def test_an_unchanged_status_is_not_posted_again(self):
        current = [{"context": "Merge gate", "state": "success", "description": "comments answered", "creator": {"login": "gate[bot]"}}]
        with mock.patch.object(status, "merge_status", return_value=("success", "comments answered")):
            result, posted = self.evaluate({"state": "OPEN", "headRefOid": HEAD, "baseRefName": "main"}, current=current)
        self.assertTrue(result["statuses"][0].get("unchanged"))
        self.assertEqual(posted, [])

    def test_an_unreadable_pull_request_marks_merge_gate_as_error(self):
        def broken(path):
            raise status.GateError("API down")
        with self.assertRaises(status.GateError):
            self.evaluate({"state": "OPEN", "headRefOid": HEAD, "baseRefName": "main"}, pages=broken)

    def test_closed_pull_request_is_skipped(self):
        result, posted = self.evaluate({"state": "MERGED", "headRefOid": HEAD, "baseRefName": "main"})
        self.assertEqual(result, {"pr": 7, "skipped": "not open"})


class PlanGateTests(unittest.TestCase):
    ROLES = {"chief": "admin", "boss": "maintain"}

    def judge(self, plan_on=True, plan=1, plan_author="gate", reviewed_keys=(), created="2026-11-22T10:00:00Z",
              title="fix(x): y (LOGIN-FIX)", refs=None, labels=(), labeled_by="", comments=(), listing=(), no_permission=(), switch=None,
              plan_id=1):
        reads, self.fields = [], []
        refs = [{"number": 12, "repository": {"owner": {"login": "o"}, "name": "r"}}] if refs is None else refs
        view = {"state": "OPEN", "headRefOid": HEAD, "baseRefName": "main", "title": title, "createdAt": created,
                "labels": [{"name": name} for name in labels], "closingIssuesReferences": refs}
        notes = []
        if plan:
            body = "<!-- sheriff-plan v=" + str(plan) + " base=" + BASE + " -->\nplan"
            notes.append({"databaseId": plan_id, "body": body, "createdAt": "2026-11-22T09:00:00Z",
                          "author": {"login": plan_author, "__typename": "Bot" if plan_author == "gate" else "User"}})
        issue = {"data": {"repository": {"issue": {"number": 12, "title": "LOGIN-FIX: fix login", "body": "", "state": "OPEN",
                 "author": {"login": "owner", "__typename": "User"}, "labels": {"nodes": []},
                 "comments": {"pageInfo": {"hasNextPage": False}, "nodes": notes}}}}}
        runs = {"check_runs": [{"app": {"slug": "gate"}, "status": "completed", "external_id": key} for key in reviewed_keys]}
        events = [{"event": "labeled", "label": {"name": name}, "actor": {"login": labeled_by}} for name in labels]

        def fake_gh_json(args):
            if args[0] == "pr":
                self.fields.append(args[args.index("--json") + 1])
                return view
            if args[1] == "graphql":
                reads.append(args)
                number = int(args[args.index("-F") + 1].split("=")[1])
                issue["data"]["repository"]["issue"]["number"] = number
                return issue
            joined = " ".join(args)
            if "--slurp" in args:
                return [list(listing)]
            if "/permission" in joined:
                if args[1].split("/")[-2] in no_permission:
                    raise status.GateError("gh api failed: 404")
                return {"role_name": self.ROLES.get(args[1].split("/")[-2], "read")}
            if "check-runs" in joined:
                return runs
            return []

        def fake_pages(path):
            return events if path.endswith("/events") else list(comments)

        env = {"GH_REPO": "o/r", "GH_ACTOR_LOGIN": "gate[bot]", "SHERIFF_PLAN": switch if switch is not None else "on" if plan_on else "",
               "SHERIFF_PLAN_SINCE": "2026-11-22T00:00:00Z"}
        with mock.patch.object(status, "gh_json", side_effect=fake_gh_json), \
             mock.patch.object(status, "gh_pages", side_effect=fake_pages), \
             mock.patch.dict(os.environ, env):
            judged = status.judge(7, [], NOBODY, "", False, True, "main")
        return judged["state"], judged["description"], reads

    def test_without_planning_on_no_issue_is_read(self):
        state, description, reads = self.judge(plan_on=False, plan=None)
        self.assertEqual(state, "success")
        self.assertEqual(reads, [])

    def test_without_planning_on_the_pull_request_fields_are_the_ones_read_before_planning(self):
        self.judge(plan_on=False, plan=None)
        self.assertEqual(self.fields, ["state,headRefOid,baseRefName"])
        self.judge(plan=None)
        self.assertEqual(self.fields, ["state,headRefOid,baseRefName,title,createdAt,labels,closingIssuesReferences"])

    def test_the_plan_switch_ignores_case_like_the_workflow_condition(self):
        for value in ("ON", " On "):
            with self.subTest(value=value):
                self.assertEqual(self.judge(plan=None, switch=value)[:2], ("pending", "needs a Sheriff plan on #12"))
        self.assertEqual(self.judge(plan=None, switch="off")[0], "success")

    def test_a_task_pull_request_without_a_plan_is_pending_with_the_issue_number(self):
        self.assertEqual(self.judge(plan=None)[:2], ("pending", "needs a Sheriff plan on #12"))

    def test_a_plan_pasted_by_a_person_does_not_count(self):
        self.assertEqual(self.judge(plan=1, plan_author="writer")[:2], ("pending", "needs a Sheriff plan on #12"))

    def test_a_current_plan_without_a_review_against_it_is_pending(self):
        self.assertEqual(self.judge(plan=2, reviewed_keys=[BASE, BASE + "/plan-1.1", BASE + "/plan-2"])[:2],
                         ("pending", "needs a Sheriff review against plan v2 on #12"))

    def test_a_reviewed_current_plan_passes(self):
        self.assertEqual(self.judge(plan=2, reviewed_keys=[BASE + "/plan-2.1"])[0], "success")

    def test_a_review_against_another_issues_plan_at_the_same_version_does_not_count(self):
        relinked = {"refs": [{"number": 15, "repository": {"owner": {"login": "o"}, "name": "r"}}], "plan_id": 2}
        self.assertEqual(self.judge(plan=1, reviewed_keys=[BASE + "/plan-1.1"], **relinked)[:2], ("pending", "needs a Sheriff review against plan v1 on #15"))
        self.assertEqual(self.judge(plan=1, reviewed_keys=[BASE + "/plan-1.2"], **relinked)[0], "success")

    def test_a_review_against_a_deleted_and_rewritten_plan_at_the_same_version_does_not_count(self):
        self.assertEqual(self.judge(plan=1, plan_id=7, reviewed_keys=[BASE + "/plan-1.1"])[:2], ("pending", "needs a Sheriff review against plan v1 on #12"))
        self.assertEqual(self.judge(plan=1, plan_id=7, reviewed_keys=[BASE + "/plan-1.7"])[0], "success")

    def test_a_pull_request_opened_before_planning_started_is_exempt(self):
        self.assertEqual(self.judge(plan=None, created="2026-11-21T23:00:00Z")[0], "success")

    def test_a_pull_request_without_task_id_or_closing_reference_is_not_gated(self):
        self.assertEqual(self.judge(plan=None, title="Install Sheriff", refs=[])[0], "success")

    def test_the_no_plan_label_exempts_only_when_an_admin_applied_it(self):
        state, description, _ = self.judge(plan=None, labels=["sheriff:no-plan"], labeled_by="chief")
        self.assertEqual(state, "success")
        self.assertTrue(description.endswith("; no plan needed (sheriff:no-plan)"))
        self.assertEqual(self.judge(plan=None, labels=["sheriff:no-plan"], labeled_by="boss")[0], "pending")

    def test_a_label_applied_by_a_bot_is_ignored(self):
        state, description, _ = self.judge(plan=None, labels=["sheriff:no-plan"], labeled_by="app[bot]")
        self.assertEqual((state, description), ("pending", "needs a Sheriff plan on #12"))

    def test_a_labeler_whose_permission_cannot_be_read_is_ignored(self):
        state, description, _ = self.judge(plan=None, labels=["sheriff:no-plan"], labeled_by="ghost", no_permission=("ghost",))
        self.assertEqual((state, description), ("pending", "needs a Sheriff plan on #12"))

    def test_a_task_issue_found_by_the_pull_request_title(self):
        found = {"title": "fix(x): y (ABC-1)", "refs": [], "listing": [{"number": 15, "title": "ABC-1: something"}]}
        self.assertEqual(self.judge(plan=None, **found)[:2], ("pending", "needs a Sheriff plan on #15"))
        self.assertEqual(self.judge(plan=1, **found)[:2], ("pending", "needs a Sheriff review against plan v1 on #15"))
        self.assertEqual(self.judge(plan=1, reviewed_keys=[BASE + "/plan-1.1"], **found)[0], "success")

    def test_an_unreadable_plan_is_pending_not_an_error(self):
        with mock.patch.object(status.planner, "read_issue", side_effect=status.planner.PlanError("bad")):
            self.assertEqual(self.judge(plan=1)[:2], ("pending", "could not read the plan on #12"))

    def test_a_missing_plan_with_an_unanswered_comment_is_a_failure(self):
        self.assertEqual(self.judge(plan=None, comments=[comment("owner", "Why?", "2026-11-22T10:00:00Z")])[0], "failure")


class SweepTests(unittest.TestCase):
    def run_main(self, args, rows, failing=()):
        evaluated = []

        def fake_judge(number, *rest):
            evaluated.append(number)
            if number in failing:
                raise status.GateError("gh pr view failed")
            return {"pr": number, "skipped": "not open"} if number == 7 else {"pr": number, "statuses": []}

        with mock.patch.object(status, "judge", side_effect=fake_judge), \
             mock.patch.object(status, "open_rows", return_value=[row(n) for n in rows]), \
             mock.patch.object(status, "production_failures", return_value=[]), \
             mock.patch.dict(os.environ, {"SHERIFF_DEFAULT_BRANCH": "main"}), \
             mock.patch("builtins.print"):
            code = status.main(args)
        return code, evaluated

    def test_a_closed_pull_request_sweeps_every_open_one_once(self):
        self.assertEqual(self.run_main(["--pr", "7"], [8, 9]), (0, [7, 8, 9]))

    def test_open_pull_requests_do_not_sweep(self):
        self.assertEqual(self.run_main(["--pr", "8"], [8, 9]), (0, [8]))

    def test_one_failing_pull_request_does_not_stop_the_others(self):
        self.assertEqual(self.run_main(["--pr", "8", "--pr", "9"], [], failing=(8,)), (1, [8, 9]))


def row(number, armed=False, base="main", rollup=()):
    return {"number": number, "baseRefName": base, "autoMergeRequest": {"mergeMethod": "SQUASH"} if armed else None,
            "statusCheckRollup": list(rollup)}


class SerialMergeTests(unittest.TestCase):
    def run_main(self, args, rows, blocked=(), rules=status.GateError("unreadable")):
        posted, judged = {}, []

        def fake_gh_json(args):
            if args[0] == "pr" and args[1] == "view":
                number = int(args[2])
                return {"state": "OPEN", "headRefOid": str(number % 10) * 40, "baseRefName": "main"}
            if args[:2] == ["api", "repos/{owner}/{repo}/rules/branches/main"]:
                if isinstance(rules, Exception):
                    raise rules
                return list(rules)
            return []

        def fake_pages(path):
            if "/pulls/" in path and path.endswith("/reviews"):
                judged.append(int(path.split("/pulls/")[1].split("/")[0]))
            if any("/issues/%d/" % n in path for n in blocked):
                return [comment("owner", "Why?", "2026-11-21T10:00:00Z")]
            return []

        def fake_gh(args):
            if args[:2] == ["api", "-X"]:
                fields = dict(item.split("=", 1) for item in args if "=" in item and not item.startswith("repos/"))
                posted[args[3].split("/")[-1]] = (fields["state"], fields["description"])
            return ""

        with mock.patch.object(status, "gh_json", side_effect=fake_gh_json), \
             mock.patch.object(status, "gh_pages", side_effect=fake_pages), \
             mock.patch.object(status, "gh", side_effect=fake_gh), \
             mock.patch.object(status, "open_rows", return_value=rows), \
             mock.patch.object(status, "current_statuses", return_value={}), \
             mock.patch.object(status, "production_failures", return_value=[]), \
             mock.patch.dict(os.environ, {"SHERIFF_DEFAULT_BRANCH": "main"}), \
             mock.patch("builtins.print"):
            code = status.main(args)
        return code, {int(sha[0]): value for sha, value in posted.items()}, judged

    def test_only_the_lowest_armed_ready_pull_request_gets_success(self):
        code, posted, _ = self.run_main([], [row(3, True), row(2, True), row(4, True)])
        self.assertEqual(posted[2][0], "success")
        self.assertEqual(posted[3], ("pending", "one merge per deploy: waiting for #2"))
        self.assertEqual(posted[4], ("pending", "one merge per deploy: waiting for #2"))

    def test_only_the_required_checks_decide_whether_an_armed_pull_request_may_lead(self):
        required = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Tests"}, {"context": "Merge gate"}]}}]
        tests_ok = {"__typename": "CheckRun", "name": "Tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        optional_failed = {"__typename": "CheckRun", "name": "Docs Build", "status": "COMPLETED", "conclusion": "FAILURE"}
        tests_pending = {"__typename": "CheckRun", "name": "Tests", "status": "IN_PROGRESS", "conclusion": ""}
        code, posted, _ = self.run_main([], [row(2, True, rollup=[tests_ok, optional_failed])], rules=required)
        self.assertEqual(posted[2][0], "success")
        code, posted, _ = self.run_main([], [row(2, True, rollup=[tests_pending, optional_failed])], rules=required)
        self.assertEqual(posted[2], ("pending", "waiting for its checks before merging"))
        code, posted, _ = self.run_main([], [row(2, True, rollup=[optional_failed])], rules=required)
        self.assertEqual(posted[2][0], "pending")

    def test_an_unreadable_rules_endpoint_falls_back_to_the_whole_rollup(self):
        optional_failed = {"__typename": "CheckRun", "name": "Docs Build", "status": "COMPLETED", "conclusion": "FAILURE"}
        code, posted, _ = self.run_main([], [row(2, True, rollup=[optional_failed])], rules=status.GateError("403"))
        self.assertEqual(posted[2], ("pending", "waiting for its checks before merging"))

    def test_a_blocked_lowest_armed_pull_request_does_not_hold_the_next(self):
        code, posted, _ = self.run_main([], [row(2, True), row(3, True), row(4, True)], blocked=(2,))
        self.assertEqual(posted[2][0], "failure")
        self.assertEqual(posted[3][0], "success")
        self.assertEqual(posted[4], ("pending", "one merge per deploy: waiting for #3"))

    def test_a_pull_request_without_auto_merge_is_unaffected(self):
        code, posted, _ = self.run_main([], [row(2, True), row(3, False), row(4, True)])
        self.assertEqual([posted[2][0], posted[3][0], posted[4][0]], ["success", "success", "pending"])

    def test_single_mode_waits_for_a_lower_armed_ready_pull_request_without_posting_on_it(self):
        code, posted, judged = self.run_main(["--pr", "4"], [row(2, True), row(4, True)])
        self.assertEqual(posted, {4: ("pending", "one merge per deploy: waiting for #2")})
        self.assertIn(2, judged)

    def test_single_mode_leader_gets_success(self):
        code, posted, judged = self.run_main(["--pr", "4"], [row(2, True), row(4, True)], blocked=(2,))
        self.assertEqual(list(posted), [4])
        self.assertEqual(posted[4][0], "success")
        code, posted, judged = self.run_main(["--pr", "2"], [row(2, True), row(4, True)])
        self.assertEqual(posted[2][0], "success")
        self.assertEqual(judged, [2])

    def test_an_armed_pull_request_with_a_pending_check_does_not_hold_the_next(self):
        waiting = [{"__typename": "CheckRun", "name": "Review", "status": "IN_PROGRESS", "conclusion": ""}]
        code, posted, _ = self.run_main([], [row(2, True, rollup=waiting), row(3, True), row(4, True)])
        self.assertEqual(posted[2], ("pending", "waiting for its checks before merging"))
        self.assertEqual(posted[3][0], "success")
        self.assertEqual(posted[4], ("pending", "one merge per deploy: waiting for #3"))

    def test_a_rollup_with_only_merge_gate_counts_as_green(self):
        gate_only = [{"__typename": "StatusContext", "context": "Merge gate", "state": "PENDING"}]
        mixed = gate_only + [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SKIPPED"},
                             {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"}]
        code, posted, _ = self.run_main([], [row(2, True, rollup=gate_only), row(3, True, rollup=mixed)])
        self.assertEqual(posted[2][0], "success")
        self.assertEqual(posted[3], ("pending", "one merge per deploy: waiting for #2"))

    def test_an_older_failed_run_of_a_check_does_not_count_against_a_newer_success(self):
        rollup = [{"__typename": "CheckRun", "name": "CI Policy", "conclusion": "CANCELLED", "startedAt": "2026-11-22T10:00:00Z",
                   "completedAt": "2026-11-22T10:01:00Z"},
                  {"__typename": "CheckRun", "name": "CI Policy", "conclusion": "SUCCESS", "startedAt": "2026-11-22T10:05:00Z",
                   "completedAt": "2026-11-22T10:06:00Z"}]
        self.assertTrue(status.checks_green({"statusCheckRollup": rollup}))
        self.assertTrue(status.checks_green({"statusCheckRollup": rollup[::-1]}))
        self.assertFalse(status.checks_green({"statusCheckRollup": [rollup[1], dict(rollup[0], completedAt="2026-11-22T10:09:00Z")]}))
        running = {"name": "Review", "status": "IN_PROGRESS", "conclusion": "", "startedAt": "2026-11-22T10:07:00Z",
                   "completedAt": "0001-01-01T00:00:00Z"}
        self.assertFalse(status.checks_green({"statusCheckRollup": [rollup[1], running]}))

    def test_disarming_the_leader_releases_the_next_armed_pull_request(self):
        code, posted, _ = self.run_main(["--pr", "2"], [row(2, False), row(3, True), row(4, True)])
        self.assertEqual([posted[2][0], posted[3][0], posted[4][0]], ["success", "success", "pending"])


if __name__ == "__main__":
    unittest.main()
