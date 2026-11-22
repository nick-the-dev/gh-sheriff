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

C1, C2, PROD = "1" * 40, "2" * 40, "f" * 40


def item(task="A", number=1, stage="Candidate", verify="Live check", decision=False, is_open=True, reason=None, blocked_by=(), awaits=None):
    return {"id": task, "node": "N" + str(number), "number": number, "title": "t", "open": is_open, "reason": reason, "url": "u",
            "created": "2026-11-01T00:00:00Z", "type": "Task", "decision": decision, "stage": stage, "verify": verify, "next": None,
            "blocked_by": list(blocked_by), "awaits_reply": awaits}


def pr(state="OPEN", commit=None, merged_at=None, number=10):
    return {"number": number, "state": state, "merged_at": merged_at, "commit": commit}


def mark(sha):
    return work.VERIFIED_MARK.format(sha)


MERGED = {"A": [pr("MERGED", C1, "2026-11-02")]}


def derive(entry, prs, live=()):
    return work.derive(entry, prs, lambda commit: "evidence" if commit in live else "")


KNOWN = {"repo": "R", "missing": [], "remaining": 5000, "types": {"Task": "T1", "Bug": "T2", "Feature": "T3"},
         "fields": {"stage": {"id": "F1", "options": [{"name": name, "id": "S" + str(index)} for index, name in enumerate(work.STAGES)]},
                    "verify": {"id": "F2", "options": [{"name": name, "id": "V" + str(index)} for index, name in enumerate(work.VERIFY)]},
                    "next": {"id": "F3"}}}


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
                            "mergeCommit": {"oid": C2}, "isCrossRepository": True}]}

        def fake(query, variables=None):
            nodes = rows[variables["states"][0]]
            return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequests": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}
        with mock.patch.object(work, "gql", fake):
            groups, branch = work.pull_requests()
        self.assertEqual((sorted(groups), branch, groups["DD"][0]["commit"]), (["BB", "DD"], "main", C2))


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
    def run_sync(self, items, prs, apply=True, only=None, moved=(), known=KNOWN, live=(C1,), comments=None):
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
        self.assertEqual(work.sync_text(result), "#1 A: Verified -> Released (Verified without recorded evidence)")

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
        self.assertEqual(work.sync_text(result), "would move #1 A: Verified -> Released (Verified without recorded evidence)")

    def test_a_revert_goes_no_higher_than_the_facts(self):
        for prs, stage, verify in ((MERGED, "Released", "Release only"), ({"A": [pr()]}, "In progress", "Live check"), ({}, "Candidate", "Live check")):
            with self.subTest(stage=stage):
                self.assertEqual(self.run_sync([item(stage="Verified", verify=verify)], prs)[0]["changes"][0]["to"], stage)

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
                                               {"name": "High", "field": {"name": "Priority"}}, {}]},
                "blockedBy": {"nodes": [{"number": 7, "state": "CLOSED"}]}, "comments": {"nodes": []}}

    def test_issue_becomes_an_item(self):
        made = work.to_item(self.node())
        self.assertEqual((made["id"], made["title"], made["stage"], made["next"], made["verify"], made["decision"]),
                         ("OBS-05", "Alerts", "Released", "decide", None, False))
        self.assertEqual(made["blocked_by"], [{"number": 7, "open": False}])

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
        self.assertIn("Needs you: 1", work.render(view))

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
    def call(self, argv, entry=None, prs=None, known=KNOWN, open_items=(), closed=(), live=(C1,)):
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
        for prs, live in ((None, (C1,)), ({"A": [pr()]}, (C1,)), (MERGED, ()), ({"A": [pr("MERGED", None, "2026-11-02")]}, (None,))):
            with self.subTest(prs=prs, live=live):
                code, calls, out = self.call(["verify", "A", "--evidence-file", "long"], item(stage="Released"), prs, live=live)
                self.assertEqual((code, calls), (1, []))
                self.assertIn("released", json.loads(out)["error"])

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
        with mock.patch.object(work, "board_id", return_value="PB"):
            code, calls, out = self.call(["new", "Board task", "--id", "BOARD-1"])
        self.assertEqual((code, calls[-1][1]["input"]["projectV2Ids"], json.loads(out)["on_board"]), (0, ["PB"], True))

    def test_a_decision_without_the_type_gets_the_label_and_no_stage(self):
        code, calls, _ = self.call(["new", "Pick a database", "--id", "ASK-DB", "--type", "Decision"])
        created = calls[-1][1]["input"]
        self.assertEqual((code, calls[0], created["labelIds"], created["issueFields"]), (0, ("label", "decision"), ["L1"], []))
        self.assertNotIn("issueTypeId", created)

    def test_an_id_already_used_open_or_closed_is_refused(self):
        self.assertEqual(self.call(["new", "x", "--id", "OLD"], closed=[item("OLD", is_open=False)])[:2], (1, []))
        self.assertEqual(self.call(["new", "x", "--id", "lower"])[:2], (1, []))

    def test_commands_need_the_fields(self):
        code, calls, out = self.call(["set", "A", "--next", "x"], item(), known=dict(KNOWN, missing=["Stage"]))
        self.assertEqual((code, calls), (1, []))
        self.assertIn("gh sheriff install", json.loads(out)["error"])


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


class WiringTest(unittest.TestCase):
    def test_every_command_parses_with_a_repository(self):
        parser = work.add_commands(argparse.ArgumentParser())
        for argv in (["new", "T", "--repo", "o/r"], ["set", "#3", "--stage", "Parked"], ["verify", "A", "--evidence-file", "-"],
                     ["status", "--json"], ["reply", "A", "--body-file", "-"], ["sync", "--apply", "--pages", "2"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).task_command, argv[0])


if __name__ == "__main__":
    unittest.main()
