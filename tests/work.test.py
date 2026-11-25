#!/usr/bin/env python3
import argparse
import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("work_module", ROOT / "scripts" / "work.py")
work = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(work)
work.REPO = "o/r"
NO_VARIABLES = {"SHERIFF_PROJECT": "", "SHERIFF_WORK_STATE_SINCE": "", "SHERIFF_DECIDERS": ""}
work.VARIABLES.update(NO_VARIABLES)

C1, C2, PROD = "1" * 40, "2" * 40, "f" * 40


def item(task="A", number=1, stage="Candidate", verify="Live check", decision=False, is_open=True, reason=None, blocked_by=(), awaits=None,
         claimed_by=None, needs_decision=False, asked=False, priority=None, created="2026-11-01T00:00:00Z", closed=None):
    return {"id": task, "node": "N" + str(number), "number": number, "title": "t", "open": is_open, "reason": reason, "url": "u",
            "created": created, "updated": created, "closed": closed if closed or is_open else "2026-11-03T00:00:00Z", "type": "Task",
            "priority": priority, "decision": decision, "needs_decision": needs_decision, "asked": asked, "stage": stage, "verify": verify,
            "next": None, "claimed_by": claimed_by, "assignees": [], "blocked_by": list(blocked_by), "awaits_reply": awaits}


def pr(state="OPEN", commit=None, merged_at=None, number=10):
    return {"number": number, "state": state, "merged_at": merged_at, "commit": commit}


def mark(sha):
    return work.VERIFIED_MARK.format(sha)


MERGED = {"A": [pr("MERGED", C1, "2026-11-02")]}
SINCE = "2026-11-02T00:00:00Z"


def derive(entry, prs, live=()):
    return work.derive(entry, prs, lambda commit: "evidence" if commit in live else "")


KNOWN = {"repo": "R", "missing": [], "remaining": 5000, "types": {"Task": "T1", "Bug": "T2", "Feature": "T3"},
         "fields": {"stage": {"id": "F1", "options": [{"name": name, "id": "S" + str(index)} for index, name in enumerate(work.STAGES)]},
                    "verify": {"id": "F2", "options": [{"name": name, "id": "V" + str(index)} for index, name in enumerate(work.VERIFY)]},
                    "next": {"id": "F3"}}, "claim": {"id": "F4"}}


class DeriveTest(unittest.TestCase):
    def test_open_pull_request_means_in_progress(self):
        self.assertEqual(derive(item(), [pr()]), "In progress")
        self.assertEqual(derive(item(stage=None), [pr()]), "In progress")

    def test_no_pull_request_changes_nothing(self):
        self.assertIsNone(derive(item(), []))
        self.assertIsNone(derive(item(stage="In progress"), []))

    def test_merged_and_not_released_is_merged(self):
        self.assertEqual(derive(item(stage="In progress"), [pr("MERGED", C1, "2026-11-02")]), "Merged")

    def test_merged_and_released_is_released(self):
        self.assertEqual(derive(item(stage="Merged"), [pr("MERGED", C1, "2026-11-02")], live={C1}), "Released")

    def test_release_only_work_is_verified_by_the_release(self):
        self.assertEqual(derive(item(stage="Merged", verify="Release only"), [pr("MERGED", C1, "2026-11-02")], live={C1}), "Verified")

    def test_live_check_work_never_verifies_itself(self):
        self.assertIsNone(derive(item(stage="Released"), [pr("MERGED", C1, "2026-11-02")], live={C1}))

    def test_verified_and_decisions_are_never_changed(self):
        self.assertIsNone(derive(item(stage="Verified"), [pr()]))
        self.assertIsNone(derive(item(decision=True), [pr()]))

    def test_never_backwards(self):
        merged = pr("MERGED", C1, "2026-11-02")
        self.assertIsNone(derive(item(stage="Released"), [merged]))
        self.assertIsNone(derive(item(stage="Released"), [merged, pr(number=11)], live={C1}))
        self.assertIsNone(derive(item(stage="Merged"), [merged, pr(number=11)]))
        self.assertIsNone(derive(item(stage="Released"), [merged, pr("MERGED", C2, "2026-11-05", 11)], live={C1}))

    def test_an_open_pull_request_holds_the_release_back(self):
        prs = [pr("MERGED", C1, "2026-11-02"), pr(number=11)]
        self.assertIsNone(derive(item(stage="In progress", verify="Release only"), prs, live={C1}))
        self.assertEqual(derive(item(), prs, live={C1}), "In progress")

    def test_latest_merge_decides(self):
        prs = [pr("MERGED", C1, "2026-11-02"), pr("MERGED", C2, "2026-11-06", 11)]
        self.assertEqual(derive(item(stage="In progress"), prs, live={C1}), "Merged")
        self.assertEqual(derive(item(stage="Merged"), prs, live={C2}), "Released")

    def test_parked_work_stays_parked_until_a_pull_request_opens(self):
        self.assertIsNone(derive(item(stage="Parked"), [pr("MERGED", C1, "2026-11-02")], live={C1}))
        self.assertEqual(derive(item(stage="Parked"), [pr()]), "In progress")

    def test_merge_older_than_the_issue_counts_only_when_the_stage_says_merged(self):
        old = [pr("MERGED", C1, "2026-10-01")]
        self.assertIsNone(derive(item(verify="Release only"), old, live={C1}))
        self.assertEqual(derive(item(stage="Merged"), old, live={C1}), "Released")

    def test_candidate_merged_between_runs_is_recorded(self):
        self.assertEqual(derive(item(), [pr("MERGED", C1, "2026-11-02")]), "Merged")

    def test_a_merge_without_a_commit_is_merged_not_released(self):
        self.assertEqual(derive(item(), [pr("MERGED", None, "2026-11-02")], live={None}), "Merged")

    def test_no_release_check_runs_when_it_cannot_move_the_stage(self):
        check = mock.Mock(side_effect=AssertionError("no release check expected"))
        self.assertIsNone(work.derive(item(stage="Released"), [pr("MERGED", C1, "2026-11-02")], check))


class PullRequestTaskTest(unittest.TestCase):
    def test_title_suffix_names_the_task(self):
        self.assertEqual(work.pr_task({"headRefName": "fix/x", "title": "fix(a): b (UX-09C) "}), "UX-09C")

    def test_branch_tail_names_the_task(self):
        self.assertEqual(work.pr_task({"headRefName": "ai/OBS-02B", "title": "anything"}), "OBS-02B")
        self.assertEqual(work.pr_task({"headRefName": "GATE-STATUSES", "title": "anything"}), "GATE-STATUSES")

    def test_dependency_bump_has_no_task(self):
        self.assertIsNone(work.pr_task({"headRefName": "dependabot/npm/x-1.2", "title": "chore(deps): bump x (#12)"}))


class PullRequestsTest(unittest.TestCase):
    def test_forks_and_merges_into_other_branches_do_not_count(self):
        rows = {"OPEN": [{"number": 1, "title": "a (AA)", "headRefName": "x", "isCrossRepository": True},
                         {"number": 2, "title": "b (BB)", "headRefName": "x", "isCrossRepository": False}],
                "MERGED": [{"number": 3, "title": "c (CC)", "headRefName": "x", "baseRefName": "release", "mergedAt": "t", "mergeCommit": {"oid": C1}},
                           {"number": 4, "title": "d (DD)", "headRefName": "x", "baseRefName": "main", "mergedAt": "t", "mergeCommit": {"oid": C2}},
                           {"number": 5, "title": "renamed (EE)", "headRefName": "x", "baseRefName": "main", "mergedAt": "t",
                            "mergeCommit": {"oid": C2}, "isCrossRepository": True}],
                "CLOSED": [{"number": 6, "title": "f (FF)", "headRefName": "x", "baseRefName": "main", "closedAt": "t2"}]}

        def fake(query, variables=None):
            nodes = rows[variables["states"][0]]
            return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequests": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}
        with mock.patch.object(work, "gql", fake):
            groups, branch = work.pull_requests()
        self.assertEqual((sorted(groups), branch, groups["DD"][0]["commit"]), (["BB", "DD", "FF"], "main", C2))
        self.assertEqual((groups["FF"][0]["state"], groups["FF"][0]["closed_at"]), ("CLOSED", "t2"))


class ReleaseCheckTest(unittest.TestCase):
    def check(self, env, compares=None, runs=None, served=None):
        statuses = compares or {}
        calls = []

        def fake_gh(args, stdin=None):
            if args[:2] == ["run", "list"]:
                return mock.Mock(returncode=0, stdout=json.dumps(runs or []))
            calls.append(args[1])
            key = args[1].split("/compare/")[1].split("?")[0]
            return mock.Mock(returncode=0 if key in statuses else 1, stdout=statuses.get(key, "") + "\n")
        for patcher in (mock.patch.dict(work.os.environ, env, clear=True), mock.patch.object(work, "gh", fake_gh),
                        mock.patch.object(work, "served_sha", lambda url: served)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return work.release_check("main"), calls

    def run_row(self, sha, number, conclusion="success", status="completed", at="2026-11-02"):
        return {"databaseId": number, "headSha": sha, "status": status, "conclusion": conclusion, "createdAt": at}

    def test_production_serving_a_descendant_is_released(self):
        released, _ = self.check({"SHERIFF_VERSION_URL": "u"}, {C1 + "..." + PROD: "ahead"}, served=PROD)
        self.assertIn(PROD, released(C1))

    def test_production_behind_or_unreadable_is_not_released(self):
        self.assertEqual(self.check({"SHERIFF_VERSION_URL": "u"}, {C1 + "..." + PROD: "behind"}, served=PROD)[0](C1), "")
        self.assertEqual(self.check({"SHERIFF_VERSION_URL": "u"}, served=None)[0](C1), "")
        self.assertEqual(self.check({"SHERIFF_VERSION_URL": "u"}, served=PROD)[0](C1), "")

    def test_newest_deploy_run_containing_the_merge_decides(self):
        runs = [self.run_row(PROD, 3, at="2026-11-03"), self.run_row(C2, 2, conclusion="failure", at="2026-11-02")]
        released, _ = self.check({"SHERIFF_DEPLOY_WORKFLOW": "Deploy"}, {C1 + "..." + PROD: "ahead"}, runs)
        self.assertIn("deploy run 3", released(C1))
        failed, _ = self.check({"SHERIFF_DEPLOY_WORKFLOW": "Deploy"}, {C1 + "..." + C2: "identical"},
                               [self.run_row(C2, 2, conclusion="failure"), self.run_row(PROD, 1, at="2026-11-01")])
        self.assertEqual(failed(C1), "")

    def test_a_running_deploy_is_not_a_release_and_skipped_runs_are_ignored(self):
        runs = [self.run_row(PROD, 4, conclusion="skipped", at="2026-11-04"), self.run_row(PROD, 3, status="in_progress", conclusion="", at="2026-11-03")]
        self.assertEqual(self.check({"SHERIFF_DEPLOY_WORKFLOW": "Deploy"}, {C1 + "..." + PROD: "ahead"}, runs)[0](C1), "")

    def test_scan_stops_at_a_deploy_older_than_the_merge(self):
        runs = [self.run_row(C2, 2, at="2026-11-03"), self.run_row(PROD, 1, at="2026-11-01")]
        released, calls = self.check({"SHERIFF_DEPLOY_WORKFLOW": "Deploy"}, {C1 + "..." + C2: "behind", C1 + "..." + PROD: "ahead"}, runs)
        self.assertEqual(released(C1), "")
        self.assertEqual(len(calls), 1)

    def test_without_a_release_check_the_merge_is_the_release(self):
        self.assertTrue(self.check({})[0](C1))

    def test_compare_results_are_cached(self):
        released, calls = self.check({"SHERIFF_VERSION_URL": "u"}, {C1 + "..." + PROD: "ahead"}, served=PROD)
        released(C1)
        released(C1)
        self.assertEqual(len(calls), 1)


class ServedShaTest(unittest.TestCase):
    def served(self, code, body):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = code
        response.read.return_value = body
        opener = mock.Mock(open=mock.Mock(return_value=response))
        with mock.patch.object(work.urllib.request, "build_opener", return_value=opener):
            return work.served_sha("u")

    def test_a_full_sha_is_read(self):
        self.assertEqual(self.served(200, json.dumps({"sha": PROD}).encode()), PROD)

    def test_anything_else_is_unknown(self):
        self.assertIsNone(self.served(200, b'{"sha": "abc"}'))
        self.assertIsNone(self.served(200, b"ok"))
        self.assertIsNone(self.served(503, json.dumps({"sha": PROD}).encode()))


class SyncTest(unittest.TestCase):
    def run_sync(self, items, prs, apply=True, only=None, moved=(), known=KNOWN, live=(C1,), comments=None, since=SINCE):
        calls, bodies = [], comments or {}

        def fake_gql(query, variables=None):
            return {"nodes": [{"id": i["node"], "comments": {"nodes": [{"body": body} for body in bodies.get(i["id"], [])]}}
                              for i in items if i["node"] in variables["ids"]]}
        patches = {
            "schema": lambda: known, "issues": lambda state="OPEN", pages=0: [i for i in items if i["open"] == (state == "OPEN")],
            "pull_requests": lambda pages=0, merged=True: (prs, "main"),
            "release_check": lambda branch: (lambda commit: "evidence for " + commit if commit in live else ""),
            "unchanged": lambda entry: entry["id"] not in moved,
            "set_fields": lambda known, entry, **values: calls.append(("set", entry["id"], values)),
            "comment": lambda entry, body, mark=work.AGENT_MARK: calls.append(("comment", entry["id"], body + "\n\n" + mark)),
            "close": lambda entry, reason="COMPLETED": calls.append(("close", entry["id"])),
            "reopen": lambda entry: calls.append(("reopen", entry["id"])), "gql": fake_gql,
            "variable": lambda name: since if name == "SHERIFF_WORK_STATE_SINCE" else "",
            "release_claim": lambda known, entry: calls.append(("release", entry["id"])),
        }
        with mock.patch.multiple(work, **patches):
            return work.sync(apply, only), calls

    def test_release_only_work_is_commented_set_and_closed(self):
        result, calls = self.run_sync([item(stage="Merged", verify="Release only")], {"A": [pr("MERGED", C1, "2026-11-02")]})
        self.assertEqual([call[:2] for call in calls], [("comment", "A"), ("set", "A"), ("close", "A")])
        self.assertIn("evidence for " + C1, calls[0][2])
        self.assertTrue(calls[0][2].endswith("\n\n" + mark(C1)))
        self.assertEqual(calls[1][2], {"stage": "Verified"})

    def test_issue_closed_at_merge_still_moves_but_is_not_closed_again(self):
        entry = item(stage="In progress", verify="Release only", is_open=False, reason="COMPLETED")
        result, calls = self.run_sync([entry], {"A": [pr("MERGED", C1, "2026-11-02")]})
        self.assertEqual([call[:2] for call in calls], [("comment", "A"), ("set", "A")])

    def test_closed_as_not_planned_or_never_staged_is_skipped(self):
        items = [item("A", 1, "In progress", is_open=False, reason="NOT_PLANNED"), item("B", 2, None, is_open=False, reason="COMPLETED")]
        result, calls = self.run_sync(items, {"A": [pr("MERGED", C1, "2026-11-02")], "B": [pr("MERGED", C1, "2026-11-02")]})
        self.assertEqual((result["changes"], calls), ([], []))

    def test_dry_run_writes_nothing(self):
        result, calls = self.run_sync([item(stage="Merged")], {"A": [pr("MERGED", C1, "2026-11-02")]}, apply=False)
        self.assertEqual((calls, result["changes"][0]["to"]), ([], "Released"))
        self.assertEqual(work.sync_text(result), "would move #1 A: Merged -> Released")

    def test_a_second_run_changes_nothing_and_prints_nothing(self):
        prs = {"A": [pr("MERGED", C1, "2026-11-02")]}
        first, calls = self.run_sync([item(stage="In progress")], prs)
        second, again = self.run_sync([item(stage=calls[0][2]["stage"])], prs)
        self.assertEqual((first["changes"][0]["to"], second["changes"], again, work.sync_text(second)), ("Released", [], [], ""))

    def test_item_changed_since_the_read_is_not_written(self):
        result, calls = self.run_sync([item(stage="Merged", verify="Release only")], {"A": [pr("MERGED", C1, "2026-11-02")]}, moved=("A",))
        self.assertEqual((calls, result["changes"][0]["skipped"]), ([], "changed since it was read"))

    def test_only_filter_touches_one_task(self):
        items = [item("A", 1, "Merged"), item("B", 2, "Merged")]
        result, calls = self.run_sync(items, {"A": [pr("MERGED", C1, "2026-11-02")], "B": [pr("MERGED", C1, "2026-11-02")]}, only="B")
        self.assertEqual([call[1] for call in calls], ["B"])

    def test_issues_without_a_task_id_are_ignored(self):
        result, calls = self.run_sync([item(None)], {"A": [pr()]})
        self.assertEqual(calls, [])

    def test_verified_with_evidence_for_the_latest_merge_stays(self):
        evidence = {"A": ["## Verified in production\n\nchecked\r\n\r\n" + mark(C1) + " \r\n", "thanks"]}
        result, calls = self.run_sync([item(stage="Verified", is_open=False, reason="COMPLETED")], MERGED, comments=evidence)
        self.assertEqual((result["changes"], calls, work.sync_text(result)), ([], [], ""))

    def test_verified_without_evidence_moves_back_to_released_with_one_comment(self):
        result, calls = self.run_sync([item(stage="Verified")], MERGED, comments={"A": ["looks good"]})
        self.assertEqual([call[:2] for call in calls], [("comment", "A"), ("set", "A")])
        self.assertIn("set back to Released.", calls[0][2])
        self.assertIn("`gh sheriff task verify A --evidence-file <file>`", calls[0][2])
        self.assertTrue(calls[0][2].endswith(work.AGENT_MARK))
        self.assertEqual(calls[1][2], {"stage": "Released"})
        self.assertEqual(work.sync_text(result), "#1 A: Verified -> Released (Verified without recorded evidence)\n"
                                                 "1 Verified task(s) without recorded evidence were set back")

    def test_evidence_for_an_older_merge_does_not_count(self):
        prs = {"A": [pr("MERGED", C1, "2026-11-02"), pr("MERGED", C2, "2026-11-06", 11)]}
        for live, stage in (((C1, C2), "Released"), ((C1,), "Merged")):
            with self.subTest(stage=stage):
                result, calls = self.run_sync([item(stage="Verified")], prs, live=live, comments={"A": ["checked\n\n" + mark(C1)]})
                self.assertEqual((result["changes"][0]["to"], calls[1][2]), (stage, {"stage": stage}))

    def test_a_marker_that_is_not_the_last_line_or_names_another_sha_does_not_count(self):
        bodies = [mark(C1) + "\nedited below", "checked\n\n" + mark(C2), "checked\n\n<!-- sheriff-verified sha=" + C1[:7] + " -->"]
        result, _ = self.run_sync([item(stage="Verified")], MERGED, comments={"A": bodies})
        self.assertEqual(result["changes"][0]["to"], "Released")

    def test_a_verified_issue_closed_as_completed_is_reopened(self):
        result, calls = self.run_sync([item(stage="Verified", is_open=False, reason="COMPLETED")], MERGED)
        self.assertEqual([call[:2] for call in calls], [("comment", "A"), ("set", "A"), ("reopen", "A")])
        self.assertIn("set back to Released and the issue reopened.", calls[0][2])

    def test_a_second_sync_after_a_revert_does_nothing(self):
        first, calls = self.run_sync([item(stage="Verified")], MERGED)
        notes = {"A": [call[2] for call in calls if call[0] == "comment"]}
        second, again = self.run_sync([item(stage=first["changes"][0]["to"])], MERGED, comments=notes)
        self.assertEqual((second["changes"], again, work.sync_text(second)), ([], [], ""))

    def test_a_revert_without_apply_is_only_reported(self):
        result, calls = self.run_sync([item(stage="Verified", is_open=False, reason="COMPLETED")], MERGED, apply=False)
        self.assertEqual(calls, [])
        self.assertEqual(work.sync_text(result), "would move #1 A: Verified -> Released (Verified without recorded evidence)\n"
                                                 "1 Verified task(s) without recorded evidence would be set back")

    def test_a_revert_goes_no_higher_than_the_facts(self):
        for prs, stage, verify in ((MERGED, "Released", "Release only"), ({"A": [pr()]}, "In progress", "Live check"), ({}, "Candidate", "Live check")):
            with self.subTest(stage=stage):
                self.assertEqual(self.run_sync([item(stage="Verified", verify=verify)], prs)[0]["changes"][0]["to"], stage)

    def test_a_marker_without_a_sha_counts_only_while_there_is_no_merge(self):
        notes = {"A": ["checked\n\n" + mark(work.NO_MERGE)]}
        self.assertEqual(self.run_sync([item(stage="Verified")], {}, comments=notes)[0]["changes"], [])
        self.assertEqual(self.run_sync([item(stage="Verified")], MERGED, comments=notes)[0]["changes"][0]["to"], "Released")

    def test_decisions_stay_and_a_marker_counts_when_no_merge_is_in_view(self):
        items = [item("A", 1, "Verified", decision=True), item("B", 2, "Verified")]
        result, calls = self.run_sync(items, {}, comments={"B": ["checked\n\n" + mark(C2)]})
        self.assertEqual((result["changes"], calls), ([], []))

    def test_a_run_low_on_rate_limit_waits_for_a_later_one(self):
        result, calls = self.run_sync([item(stage="Merged")], {"A": [pr("MERGED", C1, "2026-11-02")]}, known=dict(KNOWN, remaining=work.RESERVE - 1))
        self.assertEqual((calls, result["changes"]), ([], []))
        self.assertIn("later run", work.sync_text(result))

    def test_a_repository_without_the_fields_is_skipped_quietly(self):
        result, calls = self.run_sync([item()], {"A": [pr()]}, known=dict(KNOWN, missing=["Stage"]))
        self.assertEqual((calls, result["changes"]), ([], []))
        self.assertIn("gh sheriff install", work.sync_text(result))


class EvidenceTest(unittest.TestCase):
    def test_markers_are_read_from_each_issues_comments_in_batches_of_100(self):
        seen = []

        def fake(query, variables=None):
            seen.append(len(variables["ids"]))
            return {"nodes": [{"id": node, "comments": {"nodes": [{"body": "x\n" + mark(C1)}, {"body": None}, None]}} for node in variables["ids"]] + [None]}
        with mock.patch.object(work, "gql", fake):
            found = work.recorded([item(number=number) for number in range(150)])
        self.assertEqual((seen, found["N7"], len(found)), ([100, 50], {C1}, 150))

    def test_only_a_full_sha_on_the_last_line_is_a_marker(self):
        self.assertEqual(work.marked_sha("evidence\r\n\r\n" + mark(C2) + "\r\n"), C2)
        for body in ("", None, mark(C2) + "\nmore", "<!-- sheriff-verified sha=abc -->", "see " + mark(C2)):
            with self.subTest(body=body):
                self.assertIsNone(work.marked_sha(body))


class FieldInputTest(unittest.TestCase):
    def test_stage_and_verify_resolve_to_option_ids(self):
        self.assertEqual(work.field_inputs(KNOWN, stage="Merged", verify="Live check"),
                         [{"fieldId": "F1", "singleSelectOptionId": "S2"}, {"fieldId": "F2", "singleSelectOptionId": "V1"}])

    def test_unknown_stage_or_missing_option_is_refused(self):
        with self.assertRaises(work.WorkError):
            work.field_inputs(KNOWN, stage="Done")
        lacking = dict(KNOWN, fields=dict(KNOWN["fields"], stage={"id": "F1", "options": []}))
        with self.assertRaises(work.WorkError):
            work.field_inputs(lacking, stage="Merged")

    def test_empty_next_action_clears_the_field(self):
        self.assertEqual(work.field_inputs(KNOWN, next_action=""), [{"fieldId": "F3", "delete": True}])

    def test_next_action_over_the_limit_is_refused(self):
        with self.assertRaises(work.WorkError):
            work.field_inputs(KNOWN, next_action="x" * (work.TEXT_LIMIT + 1))


class ItemTest(unittest.TestCase):
    def node(self, title="OBS-05: Alerts", kind="Task", labels=()):
        return {"id": "I1", "number": 12, "title": title, "state": "OPEN", "stateReason": None, "url": "u", "createdAt": "c",
                "issueType": {"name": kind}, "labels": {"nodes": [{"name": name} for name in labels]},
                "issueFieldValues": {"nodes": [{"name": "Released", "field": {"name": "Stage"}}, {"value": "decide", "field": {"name": "Next action"}},
                                               {"name": "High", "field": {"name": "Priority"}}, {"value": "agent-7", "field": {"name": "Claimed by"}}, {}]},
                "blockedBy": {"nodes": [{"number": 7, "state": "CLOSED", "title": "Old work"}]}, "comments": {"nodes": []}}

    def test_issue_becomes_an_item(self):
        made = work.to_item(self.node())
        self.assertEqual((made["id"], made["title"], made["stage"], made["next"], made["verify"], made["decision"]),
                         ("OBS-05", "Alerts", "Released", "decide", None, False))
        self.assertEqual(made["blocked_by"], [{"number": 7, "open": False, "title": "Old work"}])
        self.assertEqual((made["claimed_by"], made["needs_decision"]), ("agent-7", False))
        self.assertTrue(work.to_item(self.node(labels=("sheriff:needs-decision",)))["needs_decision"])

    def test_issue_without_a_task_prefix_has_no_id(self):
        self.assertIsNone(work.to_item(self.node("Add build step to CI"))["id"])

    def test_decision_by_type_or_label(self):
        self.assertTrue(work.to_item(self.node(kind="Decision"))["decision"])
        self.assertTrue(work.to_item(self.node(kind=None, labels=("decision",)))["decision"])


class UnchangedTest(unittest.TestCase):
    def fresh(self, state, stage, verify):
        node = {"state": state, "issueFieldValues": {"nodes": [{"name": stage, "field": {"name": "Stage"}}, {"name": verify, "field": {"name": "Verify by"}}]}}
        with mock.patch.object(work, "gql", lambda query, variables=None: {"node": node}):
            return work.unchanged(item(stage="Merged", verify="Release only"))

    def test_same_state_stage_and_verify_is_unchanged(self):
        self.assertTrue(self.fresh("OPEN", "Merged", "Release only"))

    def test_closed_restaged_or_reverified_is_changed(self):
        self.assertFalse(self.fresh("CLOSED", "Merged", "Release only"))
        self.assertFalse(self.fresh("OPEN", "Released", "Release only"))
        self.assertFalse(self.fresh("OPEN", "Merged", "Live check"))


class ReplyTest(unittest.TestCase):
    def test_person_commenting_last_is_waiting_for_a_reply(self):
        self.assertEqual(work.awaits_reply([{"author": {"login": "owner", "__typename": "User"}, "body": "do option 2"}]), "owner")

    def test_agent_or_bot_comment_last_is_not(self):
        marked = [{"author": {"login": "agent", "__typename": "User"}, "body": "done\n\n" + work.AGENT_MARK}]
        self.assertEqual(work.awaits_reply(marked), "agent")
        with mock.patch.dict(work.os.environ, {"SHERIFF_ACK_AUTHORS": "Agent"}):
            self.assertIsNone(work.awaits_reply(marked))
            quoted = [{"author": {"login": "agent", "__typename": "User"}, "body": work.AGENT_MARK + "\nmore"}]
            self.assertEqual(work.awaits_reply(quoted), "agent")
        self.assertIsNone(work.awaits_reply([{"author": {"login": "github-actions", "__typename": "Bot"}, "body": "x"}]))
        self.assertIsNone(work.awaits_reply([]))


class OverviewTest(unittest.TestCase):
    def test_groups(self):
        blocker = {"number": 9, "open": True}
        items = [item("A", 1), item("B", 2, "In progress", blocked_by=[blocker]), item("C", 3, "Merged"), item("D", 4, "Released", blocked_by=[blocker]),
                 item("Q", 9, None, decision=True), item("E", 5, None), item(None, 7, None, awaits="someone"), item("F", 8, "Bogus")]
        view = work.overview(items)
        ids = lambda rows: [row["id"] for row in rows]
        self.assertEqual((ids(view["replies_waiting"]), ids(view["needs_you"]), ids(view["blocked"]), ids(view["no_stage"])),
                         ([None], ["Q"], ["B"], ["E", "F"]))
        self.assertEqual({stage: ids(rows) for stage, rows in view["stages"].items() if rows},
                         {"Candidate": ["A"], "In progress": ["B"], "Merged": ["C"], "Released": ["D"]})
        self.assertIn("Needs human: 1", work.render(view))

    def test_ready_is_unclaimed_unblocked_candidates_without_an_open_question(self):
        blocker = {"number": 9, "open": True}
        items = [item("A", 1), item("B", 2, claimed_by="agent-1"), item("C", 3, blocked_by=[blocker]), item("D", 4, needs_decision=True),
                 item("E", 5, "In progress"), item("Q", 6, "Candidate", decision=True), item("F", 7, "Candidate", is_open=False)]
        view = work.overview(items)
        self.assertEqual([row["id"] for row in view["ready"]], ["A"])
        text = work.render(view)
        self.assertIn("Ready: unclaimed, unblocked candidates without an open pull request: 1", text)
        self.assertIn("#2 B: t  [claimed by agent-1]", text)

    def test_a_released_blocker_is_met(self):
        view = work.overview([item("A", 1, blocked_by=[{"number": 2, "open": True}]), item("B", 2, "Released")])
        self.assertEqual(view["blocked"], [])


class MakeIdTest(unittest.TestCase):
    def test_id_comes_from_the_titles_first_words(self):
        self.assertEqual(work.make_id("Post the Review and Merge gate statuses", lambda task: False), "POST-REVIEW-MERGE")

    def test_taken_ids_get_a_number(self):
        taken = {"FIX-LOGIN", "FIX-LOGIN-2"}
        self.assertEqual(work.make_id("fix login", taken.__contains__), "FIX-LOGIN-3")

    def test_ids_always_parse_back(self):
        for title in ("2fa setup", "x", "é", "the a"):
            with self.subTest(title=title):
                self.assertRegex(work.make_id(title, lambda task: False) + ": t", work.ISSUE_TITLE_RE)


class CommandTest(unittest.TestCase):
    def call(self, argv, entry=None, prs=None, known=KNOWN, open_items=(), closed=(), live=(C1,), searched=()):
        calls = []
        patches = {
            "use_repo": lambda explicit=None: "o/r", "schema": lambda: known,
            "lookup": lambda task: entry, "issues": lambda state="OPEN", pages=0: list(open_items),
            "closed_matches": lambda task: [c for c in closed if c["id"] == task],
            "pull_requests": lambda pages=0, merged=True: (prs or {}, "main"),
            "release_check": lambda branch: (lambda commit: "released" if commit in live else ""),
            "set_fields": lambda known, e, **values: calls.append(("set", values)),
            "comment": lambda e, body, mark=work.AGENT_MARK: calls.append(("comment", body + "\n\n" + mark)),
            "close": lambda e, reason="COMPLETED": calls.append(("close", reason)),
            "label_id": lambda name: calls.append(("label", name)) or "L1",
            "gql": lambda query, variables=None: calls.append(("gql", variables)) or {"createIssue": {"issue": {"id": "I9", "number": 9, "url": "u9"}}},
            "read_text": lambda path: "x" * 50 if path == "long" else "short",
            "mirror_status": lambda node, stage: None, "task_merges": lambda task, branch: list(searched),
        }
        out = io.StringIO()
        with mock.patch.multiple(work, **patches), mock.patch("sys.stdout", out):
            code = work.main(argv)
        return code, calls, out.getvalue()

    def test_set_refuses_derived_stages_while_a_pull_request_is_open(self):
        self.assertEqual(self.call(["set", "A", "--stage", "Merged"], item(stage="In progress"), {"A": [pr()]})[:2], (1, []))
        code, calls, _ = self.call(["set", "A", "--stage", "Released"], item(stage="In progress"))
        self.assertEqual((code, calls), (0, [("set", {"stage": "Released", "verify": None, "next_action": None})]))

    def test_set_never_takes_verified_and_needs_something_to_set(self):
        self.assertEqual(self.call(["set", "A", "--stage", "Verified"], item())[:2], (1, []))
        self.assertEqual(self.call(["set", "A"], item())[:2], (1, []))

    def test_set_next_and_parked_work_on_an_issue_closed_as_completed(self):
        code, calls, _ = self.call(["set", "#1", "--next", "", "--stage", "Parked"], item(is_open=False, reason="COMPLETED"))
        self.assertEqual((code, calls), (0, [("set", {"stage": "Parked", "verify": None, "next_action": ""})]))

    def test_issues_closed_as_not_planned_are_refused(self):
        self.assertEqual(self.call(["set", "A", "--next", "x"], item(is_open=False, reason="NOT_PLANNED"))[:2], (1, []))

    def test_verify_needs_released_work_and_real_evidence(self):
        self.assertEqual(self.call(["verify", "A", "--evidence-file", "long"], item(stage="Merged"))[:2], (1, []))
        self.assertEqual(self.call(["verify", "A", "--evidence-file", "short"], item(stage="Released"))[:2], (1, []))

    def test_verify_records_the_evidence_with_the_latest_merge_sets_verified_and_closes(self):
        prs = {"A": [pr("MERGED", C2, "2026-11-06", 11), pr("MERGED", C1, "2026-11-02")]}
        code, calls, _ = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released"), prs, live=(C1, C2))
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], "comment")
        self.assertIn("x" * 50, calls[0][1])
        self.assertTrue(calls[0][1].endswith("\n\n" + mark(C2)))
        self.assertEqual(calls[1:], [("set", {"stage": "Verified", "next_action": ""}), ("close", "COMPLETED")])

    def test_verify_refuses_without_a_released_merge(self):
        for prs, live in (({"A": [pr()]}, (C1,)), (MERGED, ()), ({"A": [pr("MERGED", None, "2026-11-02")]}, (None,))):
            with self.subTest(prs=prs, live=live):
                code, calls, out = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released"), prs, live=live)
                self.assertEqual((code, calls), (1, []))
                self.assertIn("released", json.loads(out)["error"])

    def test_verify_of_work_without_a_pull_request_records_a_marker_without_a_sha(self):
        code, calls, _ = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released"), {"A": [pr("CLOSED")]})
        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1].endswith("\n\n" + mark(work.NO_MERGE)))
        self.assertEqual(work.marked_sha(calls[0][1]), work.NO_MERGE)

    def test_verify_finds_a_merge_older_than_the_recent_pull_requests(self):
        code, calls, _ = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released"), searched=[pr("MERGED", C2, "2026-10-01", 3)], live=(C2,))
        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1].endswith("\n\n" + mark(C2)))

    def test_verify_does_not_reclose_an_issue_closed_at_merge(self):
        code, calls, _ = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released", is_open=False, reason="COMPLETED"), MERGED)
        self.assertEqual([call[0] for call in calls], ["comment", "set"])

    def test_reply_comments_with_the_agent_marker(self):
        with mock.patch.multiple(work, use_repo=lambda explicit=None: "o/r", lookup=lambda task: item(), read_text=lambda path: "answer",
                                 gql=lambda query, variables=None: self.seen.append(variables) or {}):
            self.seen = []
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(work.main(["reply", "#1", "--body-file", "f"]), 0)
        self.assertTrue(self.seen[0]["body"].endswith(work.AGENT_MARK))

    def test_new_task_gets_a_generated_id_candidate_stage_and_type(self):
        code, calls, out = self.call(["new", "Fix the login page", "--verify", "Release only"], open_items=[item("FIX-LOGIN-PAGE")])
        created = calls[-1][1]["input"]
        self.assertEqual((code, created["title"], created["issueTypeId"]), (0, "FIX-LOGIN-PAGE-2: Fix the login page", "T1"))
        self.assertEqual(created["issueFields"], [{"fieldId": "F1", "singleSelectOptionId": "S0"}, {"fieldId": "F2", "singleSelectOptionId": "V0"}])
        self.assertEqual(json.loads(out)["id"], "FIX-LOGIN-PAGE-2")

    def test_a_new_task_joins_the_board_when_it_exists(self):
        with mock.patch.object(work, "board_id", return_value="PB") as board:
            code, calls, out = self.call(["new", "Board task", "--id", "BOARD-1"])
        self.assertEqual((code, calls[-1][1]["input"]["projectV2Ids"], json.loads(out)["on_board"]), (0, ["PB"], True))
        self.assertEqual(board.call_args[0][0], "r work")

    def test_the_board_is_named_after_the_repository_unless_one_is_given(self):
        self.assertEqual(work.board_title("acme/shop-api"), "shop-api work")
        with mock.patch.object(work, "board_id", return_value=None) as board:
            self.call(["new", "Board task", "--id", "BOARD-1", "--project", "Team board"])
        self.assertEqual(board.call_args[0][0], "Team board")

    def test_a_decision_without_the_type_gets_the_label_and_no_stage(self):
        code, calls, _ = self.call(["new", "Pick a database", "--id", "ASK-DB", "--type", "Decision"])
        created = calls[-1][1]["input"]
        self.assertEqual((code, calls[0], created["labelIds"], created["issueFields"]), (0, ("label", "decision"), ["L1"], []))
        self.assertNotIn("issueTypeId", created)

    def test_an_id_already_used_open_or_closed_is_refused(self):
        self.assertEqual(self.call(["new", "x", "--id", "OLD"], closed=[item("OLD", is_open=False)])[:2], (1, []))
        self.assertEqual(self.call(["new", "x", "--id", "lower"])[:2], (1, []))

    def test_new_links_blockers_both_ways(self):
        code, calls, out = self.call(["new", "Linked", "--id", "LINKED", "--blocked-by", "A", "--blocks", "#1"], item())
        links = [call[1] for call in calls if call[0] == "gql" and "blocking" in (call[1] or {})]
        self.assertEqual((code, links), (0, [{"issue": "I9", "blocking": "N1"}, {"issue": "N1", "blocking": "I9"}]))
        self.assertEqual((json.loads(out)["blocked_by"], json.loads(out)["blocks"]), ([1], [1]))

    def test_show_prints_one_item(self):
        code, calls, out = self.call(["show", "A"], item(priority="P1"))
        self.assertEqual((code, calls, json.loads(out)["priority"]), (0, [], "P1"))

    def test_drop_comments_releases_the_claim_and_closes_as_not_planned(self):
        with mock.patch.object(work, "release_claim", lambda known, entry: None) as _:
            code, calls, out = self.call(["drop", "A", "--reason", "superseded by B"], item(claimed_by="agent-1"))
        self.assertEqual((code, calls[0], calls[1], json.loads(out)["released"]), (0, ("comment", "Dropped: superseded by B\n\n" + work.AGENT_MARK),
                                                                                  ("close", "NOT_PLANNED"), "agent-1"))
        self.assertEqual(self.call(["drop", "A", "--reason", "x"], item(is_open=False, reason="COMPLETED"))[:2], (1, []))

    def test_commands_need_the_fields(self):
        code, calls, out = self.call(["set", "A", "--next", "x"], item(), known=dict(KNOWN, missing=["Stage"]))
        self.assertEqual((code, calls), (1, []))
        self.assertIn("gh sheriff install", json.loads(out)["error"])


class ClaimTest(unittest.TestCase):
    def call(self, argv, entry=None, known=KNOWN, holder_after="me", env=None, login=None):
        writes, out = [], io.StringIO()

        def fake_gql(query, variables=None):
            if query == work.FRESH_QUERY:
                values = [{"value": holder_after, "field": {"name": "Claimed by"}}] if holder_after else []
                return {"node": {"state": "OPEN", "issueFieldValues": {"nodes": values}}}
            writes.append(variables)
            return {}
        user = mock.Mock(returncode=0 if login else 1, stdout=(login or "") + "\n")
        with mock.patch.multiple(work, use_repo=lambda explicit=None: "o/r", schema=lambda: known, lookup=lambda task: entry or item(),
                                 gql=fake_gql, gh=lambda args, stdin=None: user, CLAIM_SETTLE=0), \
                mock.patch.dict(work.os.environ, env or {}, clear=True), mock.patch("sys.stdout", out):
            code = work.main(argv)
        return code, writes, json.loads(out.getvalue())

    def test_an_unclaimed_task_is_claimed_and_read_back(self):
        code, writes, out = self.call(["claim", "A", "--by", "me"])
        self.assertEqual((code, out["claimed_by"]), (0, "me"))
        self.assertEqual(writes, [{"issue": "N1", "fields": [{"fieldId": "F4", "textValue": "me"}]}])

    def test_claiming_again_as_the_holder_writes_nothing(self):
        self.assertEqual(self.call(["claim", "A", "--by", "me"], item(claimed_by="me"))[:2], (0, []))

    def test_a_task_someone_else_holds_is_refused(self):
        code, writes, out = self.call(["claim", "A", "--by", "me"], item(claimed_by="other"))
        self.assertEqual((code, writes), (1, []))
        self.assertIn("claimed by other", out["error"])

    def test_a_claim_lost_to_a_simultaneous_one_fails(self):
        code, writes, out = self.call(["claim", "A", "--by", "me"], holder_after="other")
        self.assertEqual((code, len(writes)), (1, 1))
        self.assertIn("other at the same time", out["error"])

    def test_the_claimant_defaults_to_sheriff_agent_then_the_gh_login(self):
        self.assertEqual(self.call(["claim", "A"], env={"SHERIFF_AGENT": "me"}, login="person")[2]["claimed_by"], "me")
        self.assertEqual(self.call(["claim", "A"], login="me")[2]["claimed_by"], "me")
        code, writes, out = self.call(["claim", "A"])
        self.assertEqual((code, writes), (1, []))
        self.assertIn("--by", out["error"])

    def test_closed_tasks_and_repositories_without_the_field_are_refused(self):
        self.assertEqual(self.call(["claim", "A", "--by", "me"], item(is_open=False, reason="COMPLETED"))[:2], (1, []))
        code, writes, out = self.call(["claim", "A", "--by", "me"], known=dict(KNOWN, claim=None))
        self.assertEqual((code, writes), (1, []))
        self.assertIn("no issue field Claimed by: run gh sheriff install", out["error"])

    def test_the_holder_releases_and_others_need_force(self):
        code, writes, out = self.call(["release", "A", "--by", "me"], item(claimed_by="me"))
        self.assertEqual((code, writes, out["released"]), (0, [{"issue": "N1", "fields": [{"fieldId": "F4", "delete": True}]}], "me"))
        code, writes, out = self.call(["release", "A", "--by", "me"], item(claimed_by="other"))
        self.assertEqual((code, writes), (1, []))
        self.assertIn("--force", out["error"])
        self.assertEqual(len(self.call(["release", "A", "--force"], item(claimed_by="other"))[1]), 1)

    def test_releasing_an_unclaimed_task_writes_nothing(self):
        self.assertEqual(self.call(["release", "A"], item())[:2], (0, []))


class SchemaTest(unittest.TestCase):
    def test_the_claim_field_is_optional_so_installs_from_before_claims_keep_working(self):
        fields = [{"id": "F" + name, "name": name} for name in ("Stage", "Verify by", "Next action")]
        data = {"rateLimit": {"remaining": 10}, "repository": {"id": "R", "issueTypes": {"nodes": []}, "issueFields": {"nodes": fields}}}
        with mock.patch.object(work, "gql", lambda query, variables=None: data):
            known = work.schema()
            self.assertEqual((known["missing"], known["claim"]), ([], None))
            data["repository"]["issueFields"]["nodes"].append({"id": "FC", "name": "Claimed by"})
            self.assertEqual(work.schema()["claim"]["id"], "FC")


class LookupTest(unittest.TestCase):
    def test_a_number_reads_that_issue(self):
        node = ItemTest().node()
        with mock.patch.object(work, "gql", lambda query, variables=None: {"repository": {"issue": node}}):
            self.assertEqual(work.lookup("#12")["number"], 12)

    def test_an_id_is_found_open_first_then_closed(self):
        with mock.patch.multiple(work, issues=lambda state="OPEN", pages=0: [item("A", 1)], closed_matches=lambda task: [item(task, 2, is_open=False)]):
            self.assertEqual(work.lookup("A")["number"], 1)
            self.assertEqual(work.lookup("B")["number"], 2)

    def test_an_ambiguous_or_unknown_id_is_refused(self):
        with mock.patch.multiple(work, issues=lambda state="OPEN", pages=0: [item("A", 1), item("A", 2)], closed_matches=lambda task: []):
            for task in ("A", "Z"):
                with self.subTest(task=task), self.assertRaises(work.WorkError):
                    work.lookup(task)


class GraphqlTest(unittest.TestCase):
    def test_errors_in_the_body_are_raised(self):
        body = json.dumps({"data": {"x": None}, "errors": [{"message": "no permission"}]})
        with mock.patch.object(work, "gh", return_value=mock.Mock(returncode=1, stdout=body, stderr="gh: no permission")):
            with self.assertRaisesRegex(work.WorkError, "no permission"):
                work.gql("query{x}")

    def test_data_is_returned(self):
        with mock.patch.object(work, "gh", return_value=mock.Mock(returncode=0, stdout='{"data": {"x": 1}}', stderr="")):
            self.assertEqual(work.gql("query{x}"), {"x": 1})


class AskTest(unittest.TestCase):
    def call(self, argv, entry=None, deciders=""):
        calls, out = [], io.StringIO()
        patches = {"use_repo": lambda explicit=None: "o/r", "schema": lambda: KNOWN, "lookup": lambda task: entry or item(),
                   "label_id": lambda name, description="": calls.append(("label", name)) or "L9",
                   "gql": lambda query, variables=None: calls.append(("gql", query.split("{", 1)[1].split("(")[0], variables)) or {},
                   "comment": lambda e, body, mark=work.AGENT_MARK: calls.append(("comment", body)),
                   "api": lambda args, stdin=None: calls.append(("api", args)) or {},
                   "variable": lambda name: deciders if name == "SHERIFF_DECIDERS" else ""}
        with mock.patch.multiple(work, **patches), mock.patch("sys.stdout", out):
            code = work.main(argv)
        return code, calls, json.loads(out.getvalue())

    def test_ask_labels_mentions_and_assigns(self):
        code, calls, out = self.call(["ask", "A", "--question", "Which region?", "--to", "@alice", "--to", "bob"])
        self.assertEqual((code, out["to"]), (0, ["alice", "bob"]))
        self.assertEqual(calls[0], ("label", work.ASK_LABEL))
        self.assertEqual(calls[1][1:], ("addLabelsToLabelable", {"id": "N1", "labels": ["L9"]}))
        self.assertTrue(calls[2][1].startswith("@alice @bob Which region?"))
        self.assertEqual(calls[3], ("api", ["repos/o/r/issues/1/assignees", "-X", "POST", "-f", "assignees[]=alice", "-f", "assignees[]=bob"]))

    def test_ask_defaults_to_the_deciders_and_needs_someone_and_a_question(self):
        self.assertEqual(self.call(["ask", "A", "--question", "q"], deciders="carol, dan")[2]["to"], ["carol", "dan"])
        self.assertEqual(self.call(["ask", "A", "--question", "q"])[:2], (1, []))
        self.assertEqual(self.call(["ask", "A", "--to", "x"])[:2], (1, []))
        self.assertEqual(self.call(["ask", "A", "--question", "q", "--to", "x"], item(is_open=False, reason="COMPLETED"))[:2], (1, []))

    def test_clear_removes_the_label_only_when_it_is_there(self):
        code, calls, out = self.call(["ask", "A", "--clear"], item(asked=True))
        self.assertEqual((code, calls[1][1], out["asked"]), (0, "removeLabelsFromLabelable", False))
        self.assertEqual(self.call(["ask", "A", "--clear"], item())[:2], (0, []))


class AdoptionTest(unittest.TestCase):
    run_sync = SyncTest.run_sync

    def test_closed_issues_are_audited_only_when_closed_after_the_cutoff(self):
        old = item("A", 1, "Verified", is_open=False, reason="COMPLETED", closed="2026-10-01T00:00:00Z")
        new = item("B", 2, "Verified", is_open=False, reason="COMPLETED", closed="2026-11-05T00:00:00Z")
        result, calls = self.run_sync([old, new], {"A": MERGED["A"], "B": MERGED["A"]})
        self.assertEqual(([change["id"] for change in result["changes"]], ("reopen", "A") in calls), (["B"], False))
        self.assertEqual(result["audits_closed_since"], SINCE)

    def test_without_a_cutoff_closed_issues_are_never_reopened_and_open_ones_are_still_audited(self):
        closed, opened = item("A", 1, "Verified", is_open=False, reason="COMPLETED"), item("B", 2, "Verified")
        result, calls = self.run_sync([closed, opened], {"A": MERGED["A"], "B": MERGED["A"]}, since="")
        self.assertEqual(([change["id"] for change in result["changes"]], [call for call in calls if call[0] == "reopen"]), (["B"], []))


class TriageTest(unittest.TestCase):
    run_sync = SyncTest.run_sync

    def test_an_open_task_without_a_stage_becomes_a_live_check_candidate(self):
        result, calls = self.run_sync([item(stage=None, verify=None), item("B", 2, stage=None, decision=True),
                                       item("C", 3, stage=None, is_open=False, reason="COMPLETED")], {})
        self.assertEqual(calls, [("set", "A", {"stage": "Candidate", "verify": "Live check"})])
        self.assertEqual(work.sync_text(result), "#1 A: no stage -> Candidate (it had no stage)")

    def test_work_whose_pull_requests_were_all_closed_returns_to_candidate_and_loses_its_claim(self):
        closed = pr("CLOSED")
        closed["closed_at"] = "2026-11-04T00:00:00Z"
        result, calls = self.run_sync([item(stage="In progress", claimed_by="agent-1")], {"A": [closed]})
        self.assertEqual(calls, [("set", "A", {"stage": "Candidate"}), ("release", "A")])
        self.assertEqual(result["changes"][0]["why"], "its pull requests were closed without merging")

    def test_pull_requests_closed_before_the_issue_or_beside_an_open_one_change_nothing(self):
        old, live = pr("CLOSED"), pr("CLOSED", number=11)
        old["closed_at"], live["closed_at"] = "2026-10-01T00:00:00Z", "2026-11-04T00:00:00Z"
        for prs in ([old], [live, pr()]):
            with self.subTest(prs=prs):
                self.assertEqual(self.run_sync([item(stage="In progress")], {"A": prs})[1], [])


class PriorityTest(unittest.TestCase):
    def test_ready_and_needs_you_sort_by_priority_then_age_and_ready_skips_busy_or_asked_work(self):
        items = [item("A", 1, priority="P3", created="2026-11-01"), item("B", 2, created="2026-11-02"), item("C", 3, priority="P0", created="2026-11-03"),
                 item("D", 4, created="2026-11-01"), item("E", 5, asked=True, priority="P1"), item("F", 6), item("Q", 7, None, decision=True, priority="P0"),
                 item("G", 8, needs_decision=True)]
        view = work.overview(items, busy={"F"})
        self.assertEqual([row["id"] for row in view["ready"]], ["C", "D", "B", "A"])
        self.assertEqual([row["id"] for row in view["needs_you"]], ["Q", "E", "G"])

    def test_labels_give_the_priority_and_the_question(self):
        node = ItemTest().node(labels=("priority:P1", "sheriff:needs-person"))
        node.update(updatedAt="u2", closedAt=None, assignees={"nodes": [{"login": "alice"}]})
        made = work.to_item(node)
        self.assertEqual((made["priority"], made["asked"], made["updated"], made["assignees"]), ("P1", True, "u2", ["alice"]))
        self.assertIsNone(work.to_item(ItemTest().node())["priority"])


class VariableTest(unittest.TestCase):
    def setUp(self):
        work.VARIABLES.clear()
        self.addCleanup(work.VARIABLES.update, NO_VARIABLES)

    def test_the_environment_wins_then_the_repository_variable_is_read_once(self):
        found = mock.Mock(return_value=mock.Mock(returncode=0, stdout="Team board\n"))
        with mock.patch.dict(work.os.environ, {}, clear=True), mock.patch.object(work, "gh", found):
            self.assertEqual((work.board_title("o/r"), work.board_title("o/r")), ("Team board", "Team board"))
        self.assertEqual(found.call_count, 1)
        work.VARIABLES.clear()
        with mock.patch.dict(work.os.environ, {"SHERIFF_PROJECT": "Env board"}), mock.patch.object(work, "gh", side_effect=AssertionError):
            self.assertEqual(work.board_title("o/r"), "Env board")

    def test_an_unreadable_variable_is_empty(self):
        with mock.patch.dict(work.os.environ, {}, clear=True), mock.patch.object(work, "gh", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(work.board_title("o/r"), "r work")


class HealthTest(unittest.TestCase):
    def check(self, healthy):
        with mock.patch.dict(work.os.environ, {"SHERIFF_HEALTH_URL": "h"}, clear=True), mock.patch.object(work, "answers", lambda url: healthy):
            return work.release_check("main")(C1)

    def test_an_unhealthy_production_releases_nothing(self):
        self.assertEqual(self.check(False), "")

    def test_a_healthy_production_says_so_in_the_evidence(self):
        self.assertIn("h answers 200", self.check(True))


class WiringTest(unittest.TestCase):
    def test_every_command_parses_with_a_repository(self):
        parser = work.add_commands(argparse.ArgumentParser())
        for argv in (["new", "T", "--repo", "o/r"], ["set", "#3", "--stage", "Parked"], ["verify", "A", "--evidence-file", "-"],
                     ["status", "--json"], ["reply", "A", "--body-file", "-"], ["sync", "--apply", "--pages", "2"],
                     ["claim", "A", "--by", "x"], ["release", "#3", "--force"], ["ask", "A", "--question", "q", "--to", "x"], ["ask", "A", "--clear"],
                     ["show", "A"], ["drop", "A", "--reason", "r"], ["new", "T", "--blocked-by", "A", "--blocks", "B"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).task_command, argv[0])


BOARD = {"title": "r work", "status": {"id": "SF", "options": [{"id": "O-" + name, "name": name} for name in work.STAGES if name != "Parked"]}}


class MirrorStatusTest(unittest.TestCase):
    def mirror(self, stage, current, title="r work", refuse=False, field=BOARD["status"]):
        writes = []
        items = [{"id": "PI", "project": {"id": "P", "title": title}, "status": {"name": current} if current else None}]

        def fake(query, variables=None):
            if query == work.STATUS_QUERY:
                return {"node": {"status": field}}
            if query == work.ITEM_QUERY:
                return {"node": {"projectItems": {"nodes": items}}}
            if query == work.SET_STATUS:
                if refuse:
                    raise work.WorkError("GitHub: Resource not accessible")
                writes.append(variables)
            return {}

        work.BOARDS.clear()
        work.WARNED.clear()
        err = io.StringIO()
        with mock.patch.object(work, "gql", fake), mock.patch("sys.stderr", err):
            work.mirror_status("N1", stage)
        return writes, err.getvalue()

    def test_a_stage_write_sets_a_differing_status(self):
        self.assertEqual(self.mirror("Released", "Merged")[0], [{"project": "P", "item": "PI", "field": "SF", "option": "O-Released"}])

    def test_an_equal_status_is_skipped(self):
        self.assertEqual(self.mirror("Released", "Released"), ([], ""))

    def test_another_board_is_left_alone(self):
        self.assertEqual(self.mirror("Released", None, title="other")[0], [])

    def test_a_missing_option_is_skipped_with_a_warning(self):
        writes, err = self.mirror("Parked", "Merged")
        self.assertEqual((writes, "no option named Parked" in err), ([], True))

    def test_a_board_without_status_is_skipped_with_a_warning(self):
        writes, err = self.mirror("Released", None, field=None)
        self.assertEqual((writes, "no readable Status field" in err), ([], True))

    def test_a_refused_write_is_a_warning(self):
        writes, err = self.mirror("Released", "Merged", refuse=True)
        self.assertEqual((writes, "refused the Status write" in err), ([], True))

    def test_set_fields_mirrors_a_stage_write(self):
        calls = []
        with mock.patch.object(work, "gql", lambda query, variables=None: calls.append(query) or {}), mock.patch.object(work, "mirror_status", lambda node, stage: calls.append(("mirror", node, stage))):
            work.set_fields(KNOWN, item(), stage="Merged")
        self.assertEqual(calls, [work.SET_MUTATION, ("mirror", "N1", "Merged")])


class BackfillTest(unittest.TestCase):
    def backfill(self, apply):
        def entry(item_id, stage, status):
            values = [{"__typename": "IssueFieldSingleSelectValue", "name": stage, "field": {"name": "Stage"}}] if stage else []
            return {"id": item_id, "status": {"name": status} if status else None, "content": {"issueFieldValues": {"nodes": values}}}

        nodes = [entry("I0", "Verified", "Released"), entry("I1", "Merged", "Merged"), entry("I2", "Released", None), entry("I3", None, "x"), entry("I4", "Parked", None)]
        node = dict(BOARD, items={"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes})
        writes = []

        def fake(query, variables=None):
            if query == work.ITEMS_QUERY:
                return {"node": node}
            writes.append(variables)
            return {}

        with mock.patch.object(work, "gql", fake), mock.patch.object(work, "board_id", lambda title: "P"):
            return work.board_status("r work", apply), writes

    def test_dry_run_counts_by_stage_and_writes_nothing(self):
        result, writes = self.backfill(False)
        self.assertEqual((result["differ"], result["by_stage"], result["no_status_option"], writes), (3, {"Verified": 1, "Released": 1, "Parked": 1}, ["Parked"], []))

    def test_apply_sets_status_where_it_differs_and_an_option_exists(self):
        _, writes = self.backfill(True)
        self.assertEqual([(write["item"], write["option"]) for write in writes], [("I0", "O-Verified"), ("I2", "O-Released")])


if __name__ == "__main__":
    unittest.main()
