#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ["SHERIFF_CONFIG"] = os.path.join(tempfile.mkdtemp(), "apps.json")
LOADER = importlib.machinery.SourceFileLoader("sheriff_cli", str(ROOT / "gh-sheriff"))
SPEC = importlib.util.spec_from_loader("sheriff_cli", LOADER)
cli = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(cli)


class RulesetTests(unittest.TestCase):
    def checks(self, ruleset):
        rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
        return {c["context"]: c.get("integration_id") for c in rule["parameters"]["required_status_checks"]}

    def test_existing_checks_are_kept_and_sheriff_statuses_are_pinned_to_the_gate_app(self):
        ruleset = cli.build_ruleset({"Tests": 15368, "Lint": None, "Review": 999}, 42)
        self.assertEqual(self.checks(ruleset), {"Lint": None, "Tests": 15368, "Review": 42, "Merge gate": 42})

    def test_no_bypass_default_branch_and_no_force_push(self):
        ruleset = cli.build_ruleset({}, 42)
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual({r["type"] for r in ruleset["rules"]}, {"deletion", "non_fast_forward", "pull_request", "required_status_checks"})
        self.assertEqual(ruleset["enforcement"], "active")


class ExistingChecksTests(unittest.TestCase):
    def test_rulesets_and_classic_protection_are_merged_and_sheriffs_own_ruleset_is_ignored(self):
        rules = [{"type": "required_status_checks", "ruleset_source": "o/r:Old",
                  "parameters": {"required_status_checks": [{"context": "Tests", "integration_id": 1}]}},
                 {"type": "required_status_checks", "ruleset_source": "o/r:Sheriff",
                  "parameters": {"required_status_checks": [{"context": "Review", "integration_id": 5}]}},
                 {"type": "deletion"}]
        classic = mock.Mock(returncode=0, stdout=json.dumps({"checks": [{"context": "Lint", "app_id": 2}, {"context": "Tests", "app_id": 9}]}))
        with mock.patch.object(cli, "gh_json", return_value=rules), mock.patch.object(cli.subprocess, "run", return_value=classic):
            self.assertEqual(cli.existing_required_checks("o/r", "main"), {"Tests": 1, "Lint": 2})


class StatsTests(unittest.TestCase):
    def test_the_smart_share_counts_only_final_reviews(self):
        u = lambda i, r, o: {"input": i, "cache_read": r, "cache_write": 0, "output": o, "cost_usd": 0.0}
        rows = [{"pr": 1, "stage": "first", "model": "cheap", "usage": u(900, 5000, 100)},
                {"pr": 1, "stage": "final", "model": "opus", "usage": u(90, 1000, 10)},
                {"pr": 2, "stage": "first", "model": "cheap", "usage": u(500, 0, 0)}]
        by_model, total, share = cli.summarise(rows)
        self.assertEqual(by_model[("first", "cheap")]["reviews"], 2)
        self.assertAlmostEqual(share["tokens_no_reads"], 100 / 1600 * 100)
        self.assertAlmostEqual(share["reviews"], 100 / 3)

    def test_no_reviews_means_zero_share(self):
        self.assertEqual(cli.summarise([])[2]["tokens"], 0.0)


class InstallPullRequestTests(unittest.TestCase):
    def check(self, head_repo="o/r", head_sha="h", files=(".github/workflows/sheriff.yml",)):
        pull = {"head": {"repo": {"full_name": head_repo}, "sha": head_sha}}
        rows = [{"filename": name} for name in files]
        with mock.patch.object(cli, "gh_json", side_effect=[pull, rows]):
            cli.verify_install_pr("o/r", 5, "h", {".github/workflows/sheriff.yml"})

    def test_only_our_own_commit_with_only_the_trigger_files_passes(self):
        self.check()
        for kwargs in ({"head_repo": "attacker/r"}, {"head_sha": "other"}, {"files": (".github/workflows/sheriff.yml", "src/evil.py")}):
            with self.subTest(**kwargs), self.assertRaises(cli.SheriffError):
                self.check(**kwargs)


class AppConfigTests(unittest.TestCase):
    def test_created_apps_are_remembered_by_slug(self):
        cli.remember_app("me-sheriff", 77)
        self.assertEqual(cli.known_app_id("me-sheriff"), 77)

    def test_the_manifest_asks_only_for_what_the_gate_needs(self):
        manifest = cli.manifest("Acme", 8000)
        self.assertEqual(manifest["name"], "acme-sheriff")
        self.assertFalse(manifest["public"])
        self.assertEqual(manifest["default_permissions"]["statuses"], "write")
        self.assertEqual(manifest["default_permissions"]["checks"], "write")
        self.assertNotIn("administration", manifest["default_permissions"])
        self.assertEqual(manifest["default_permissions"]["contents"], "read")


class TaskWiringTests(unittest.TestCase):
    def test_task_commands_reach_work_py_with_their_options(self):
        seen = []
        with mock.patch.object(cli.WORK, "run", lambda args: seen.append((args.task_command, args.repo, args.stage, args.next)) or {"ok": True}), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["task", "set", "GATE-1", "--stage", "Parked", "--next", "wait", "--repo", "o/r"]), 0)
        self.assertEqual(seen, [("set", "o/r", "Parked", "wait")])

    def test_a_refused_task_command_exits_1_with_the_reason(self):
        out = io.StringIO()
        with mock.patch.object(cli.WORK, "run", side_effect=cli.WORK.WorkError("no issue for X")), mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["task", "verify", "X", "--evidence-file", "-"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "no issue for X"})

    def test_install_and_doctor_take_the_work_state_options(self):
        with mock.patch.object(cli, "install", side_effect=lambda args: self.assertEqual((args.no_work_state, args.project_name), (True, "Board"))):
            cli.main(["install", "--no-work-state", "--project-name", "Board"])
        with mock.patch.object(cli, "doctor", side_effect=lambda args: self.assertIsNone(args.project_name) or 0):
            cli.main(["doctor"])


def issue_field(name):
    kind = "IssueFieldText" if name == "Next action" else "IssueFieldSingleSelect"
    options = {"Stage": cli.WORK.STAGES, "Verify by": cli.WORK.VERIFY}.get(name, ())
    return {"__typename": kind, "id": "IF-" + name, "name": name, **({"options": [{"name": o} for o in options]} if options else {})}


def repository(fields=(), types=("Task",), kind="Organization"):
    return {"id": "R1", "owner": {"__typename": kind, "id": "O1", "login": "o", "databaseId": 77},
            "issueTypes": {"nodes": [{"name": name} for name in types]}, "issueFields": {"nodes": [issue_field(name) for name in fields]}}


ORG = {"repo": "o/r", "owner": "o", "kind": "Organization"}
USER = {"repo": "me/r", "owner": "me", "kind": "User"}


class IssueFieldInstallTests(unittest.TestCase):
    def run_fields(self, info, present, fail=False):
        created, out = [], io.StringIO()

        def fake(query, variables=None):
            if "createIssueField" in query:
                if fail:
                    raise cli.WORK.WorkError("FORBIDDEN")
                created.append(variables["input"])
                present.append(variables["input"]["name"])
                return {"createIssueField": {"issueField": {}}}
            return {"repository": repository(present)}
        with mock.patch.object(cli.WORK, "gql", fake), mock.patch("sys.stdout", out):
            cli.ensure_issue_fields(info)
        return created, out.getvalue()

    def test_only_missing_fields_are_created_on_the_owner_with_their_options(self):
        created, out = self.run_fields(ORG, ["Stage"])
        self.assertEqual([(f["name"], f["dataType"], f["ownerId"]) for f in created], [("Verify by", "SINGLE_SELECT", "O1"), ("Next action", "TEXT", "O1")])
        self.assertEqual([o["name"] for o in created[0]["options"]], list(cli.WORK.VERIFY))
        self.assertEqual([o["priority"] for o in created[0]["options"]], [1, 2, 3])
        self.assertNotIn("options", created[1])
        self.assertIn("ok  issue fields", out)

    def test_nothing_is_created_when_all_exist(self):
        self.assertEqual(self.run_fields(ORG, ["Stage", "Verify by", "Next action"])[0], [])

    def test_a_refused_creation_names_the_scope_to_add(self):
        self.assertIn("gh auth refresh -s admin:org", self.run_fields(ORG, [], fail=True)[1])
        self.assertIn("gh auth refresh -s user", self.run_fields(USER, [], fail=True)[1])

    def test_wrong_kinds_and_missing_options_are_problems(self):
        found = repository(["Stage", "Verify by", "Next action"])
        found["issueFields"]["nodes"][0]["options"] = [{"name": "Candidate"}]
        found["issueFields"]["nodes"][2]["__typename"] = "IssueFieldSingleSelect"
        problems = cli.field_problems(found)
        self.assertEqual(len(problems), 2)
        self.assertIn("lacks the options In progress, Merged", problems[0])
        self.assertIn("'Next action' is not text", problems[1])


class ProjectInstallTests(unittest.TestCase):
    def run_project(self, info, projects, fields, views=(), linked=False, types=("Task",)):
        calls, posted, out = [], [], io.StringIO()
        detail = {"repositories": {"nodes": [{"id": "R1"}] if linked else []}, "views": {"nodes": [{"name": name} for name in views]},
                  "fields": {"nodes": [{"databaseId": index + 1, "name": name, "isIssueField": issue}
                                       for index, (name, issue) in enumerate(fields)]}}

        def fake(query, variables=None):
            for marker in ("createProjectV2IssueField", "linkProjectV2ToRepository", "createProjectV2(", "projectsV2(first", "node(id"):
                if marker in query:
                    calls.append((marker, variables))
                    break
            if "createProjectV2IssueField" in query:
                return {"createProjectV2IssueField": {"projectV2Field": {"databaseId": 50 + len(calls), "name": variables["input"]["issueFieldId"][3:], "isIssueField": True}}}
            if "createProjectV2(" in query:
                return {"createProjectV2": {"projectV2": {"id": "P2", "number": 9, "title": "r work", "url": "u"}}}
            if "projectsV2(first" in query:
                return {"repositoryOwner": {"projectsV2": {"nodes": projects}}}
            if "node(id" in query:
                return {"node": detail}
            return {}
        fields_repo = repository(["Stage", "Verify by", "Next action"], types, info["kind"])
        with mock.patch.object(cli.WORK, "gql", fake), mock.patch.object(cli, "gh_json", lambda args, stdin=None: posted.append((args, json.loads(stdin)))), \
                mock.patch("sys.stdout", out):
            project = cli.ensure_project(info, "r work", fields_repo)
        return project, calls, posted, out.getvalue()

    def test_an_existing_project_is_reused_linked_and_given_the_missing_fields_and_views(self):
        existing = [{"id": "P1", "number": 3, "title": "r work", "url": "u"}, {"id": "P0", "number": 1, "title": "r work old", "url": "x"}]
        project, calls, posted, _ = self.run_project(USER, existing, [("Title", False), ("Stage", True)], views=("Ready",))
        markers = [marker for marker, _ in calls]
        self.assertEqual(project["id"], "P1")
        self.assertNotIn("createProjectV2(", markers)
        self.assertEqual(markers.count("linkProjectV2ToRepository"), 1)
        self.assertEqual([v["input"]["issueFieldId"] for m, v in calls if m == "createProjectV2IssueField"], ["IF-Verify by", "IF-Next action"])
        self.assertEqual([body["name"] for _, body in posted], ["Needs human", "Blocked", "In flight", "Awaiting verification", "All open by stage"])
        self.assertTrue(all(args[3] == "users/me/projectsV2/3/views" for args, _ in posted))
        bodies = {body["name"]: body for _, body in posted}
        self.assertEqual((bodies["In flight"]["layout"], bodies["In flight"]["vertical_group_by"]), ("board", [2]))
        self.assertEqual(bodies["All open by stage"]["group_by"], [2])
        self.assertEqual((bodies["Needs human"]["filter"], bodies["Blocked"]["filter"]), ("is:open label:decision", "is:open is:blocked -label:decision"))
        self.assertEqual(bodies["Needs human"]["visible_fields"][:2], [1, 2])

    def test_a_missing_project_is_created_for_the_owner_and_the_repository(self):
        project, calls, posted, _ = self.run_project(ORG, [], [("Stage", True), ("Verify by", True), ("Next action", True)], linked=True, types=("Task", "Decision"))
        created = next(v for m, v in calls if m == "createProjectV2(")
        self.assertEqual(created["input"], {"ownerId": "O1", "title": "r work", "repositoryId": "R1"})
        self.assertNotIn("linkProjectV2ToRepository", [m for m, _ in calls])
        self.assertTrue(all(args[3] == "orgs/o/projectsV2/9/views" for args, _ in posted))
        self.assertEqual(posted[0][1]["filter"], "is:open type:Decision")

    def test_everything_present_changes_nothing(self):
        names = [view[0] for view in cli.view_specs(1, "")]
        project, calls, posted, _ = self.run_project(ORG, [{"id": "P1", "number": 3, "title": "r work", "url": "u"}],
                                                     [("Stage", True), ("Verify by", True), ("Next action", True)], views=names, linked=True)
        self.assertEqual(([m for m, _ in calls], posted), (["projectsV2(first", "node(id"], []))

    def test_a_project_field_of_the_same_name_blocks_the_views(self):
        project, calls, posted, out = self.run_project(ORG, [{"id": "P1", "number": 3, "title": "r work", "url": "u"}],
                                                       [("Stage", False), ("Verify by", True), ("Next action", True)], linked=True)
        self.assertEqual(posted, [])
        self.assertIn("its own field 'Stage'", out)


WORKFLOW_FILES = {
    "ci.yml": "name: CI\non:\n  pull_request:\n    branches: [main]\n  push:\njobs:\n  build:\n    runs-on: x\n",
    "inline.yml": "name: Inline\non: [push, pull_request]\njobs:\n  lint:\n    name: Lint\n    runs-on: x\n",
    "target.yml": "name: Target\non:\n  - pull_request_target\njobs:\n  t:\n    name: Target job\n",
    "quoted.yml": "name: 'Tests: unit # and \"more\" it''s'\n\"on\":\n  pull_request: {}\njobs:\n  unit:\n    name: \"Unit: a # b\"\n",
    "colon.yml": 'name: "Build: x # y"\non:\n  pull_request:\njobs:\n  b:\n    name: Backend Tests\n',
    "unnamed.yml": "on:\n  pull_request:\njobs:\n  Unnamed Job:\n    runs-on: x\n".replace("Unnamed Job", "unnamed-job"),
    "matrix.yml": "name: Matrix\non:\n  pull_request:\njobs:\n  m:\n    name: Matrix job\n    strategy:\n      matrix:\n        v: [1]\n",
    "templated.yml": "name: Templated\non:\n  pull_request:\njobs:\n  t:\n    name: T ${{ matrix.v }}\n",
    "optional.yml": "name: Optional\non:\n  pull_request:\njobs:\n  o:\n    name: Optional job\n",
    "push-only.yml": "name: Deploy\non:\n  push:\n  workflow_run:\n    workflows: [pull_request]\njobs:\n  Lint:\n    runs-on: x\n",
    "review-only.yml": "name: Review Bot\non:\n  pull_request_review:\n  # pull_request:\njobs:\n  Lint:\n    runs-on: x\n",
    "sheriff.yml": "name: Sheriff\non:\n  pull_request:\njobs:\n  Lint:\n    runs-on: x\n",
    "sheriff-relay.yml": "name: Sheriff Relay\non:\n  pull_request:\njobs:\n  relay:\n    runs-on: x\n",
}
REQUIRED = {"build", "Lint", "Target job", "Unit: a # b", "Backend Tests", "unnamed-job", "Matrix job", "T ${{ matrix.v }}", "Review", "Merge gate"}


def fake_repo_workflows(files):
    def fake_json(args, stdin=None):
        return [{"name": name, "type": "file"} for name in files]

    def fake_remote(repo, path, ref):
        return files.get(path.rsplit("/", 1)[1])
    return mock.patch.object(cli, "gh_json", side_effect=fake_json), mock.patch.object(cli, "remote_file", side_effect=fake_remote)


class CiWorkflowTests(unittest.TestCase):
    def names(self):
        listing, remote = fake_repo_workflows(WORKFLOW_FILES)
        with listing, remote:
            return cli.pr_workflow_names("o/r", "main", REQUIRED)

    def test_the_installer_lists_only_pull_request_workflows_with_a_required_job_by_literal_name(self):
        self.assertEqual(self.names(), ["CI", "Build: x # y", "Inline", "Tests: unit # and \"more\" it's", "Target", ".github/workflows/unnamed.yml"])

    def test_without_readable_required_checks_the_installer_lists_none(self):
        listing, remote = fake_repo_workflows(WORKFLOW_FILES)
        with listing, remote:
            self.assertEqual(cli.pr_workflow_names("o/r", "main", None), [])
            self.assertEqual(cli.pr_workflow_names("o/r", "main", set()), [])

    def test_the_required_contexts_come_from_the_branch_rules_and_a_failed_read_gives_none(self):
        rules = [{"type": "pull_request"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Tests"}, {"context": "CI Policy"}]}}]
        with mock.patch.object(cli, "gh_json", return_value=rules) as read:
            self.assertEqual(cli.required_contexts("o/r", "main"), {"Tests", "CI Policy"})
        self.assertEqual(read.call_args[0][0], ["api", "repos/o/r/rules/branches/main"])
        with mock.patch.object(cli, "gh_json", side_effect=cli.SheriffError("403")):
            self.assertIsNone(cli.required_contexts("o/r", "main"))

    def rendered(self, names):
        return cli.render_template((ROOT / "templates/sheriff.yml").read_text(), names)

    def workflows_list(self, text):
        line = next(row for row in text.splitlines() if row.startswith("    workflows:"))
        return json.loads(line.split(":", 1)[1])

    def test_the_rendered_template_lists_the_relay_and_the_ci_workflows_as_quoted_literals(self):
        names = self.names()
        text = self.rendered(names)
        self.assertEqual(self.workflows_list(text), ["Sheriff Relay"] + names)
        self.assertEqual(text.count("workflows:"), 1)

    def test_a_repository_without_pull_request_workflows_keeps_the_relay_alone(self):
        self.assertEqual(self.rendered([]), (ROOT / "templates/sheriff.yml").read_text())

    def test_review_reserve_and_post_exclude_runs_of_the_ci_workflows(self):
        text = self.rendered(self.names())
        for job in ("reserve", "review", "post"):
            start = text.index("\n  " + job + ":\n")
            block = text[start:text.index("\n    runs-on:", start)]
            condition = block[block.index("    if:"):]
            with self.subTest(job=job):
                self.assertIn("github.event.workflow_run.name == 'Sheriff Relay'", condition)
        state = text[text.index("\n  state:\n"):text.index("\n    runs-on:", text.index("\n  state:\n"))]
        self.assertIn("github.event.workflow_run.name == 'Sheriff Relay'", state)
        gate = text[text.index("\n  gate:\n"):text.index("\n  reserve:\n")]
        self.assertNotIn("workflow_run.name", gate)
        self.assertIn("join(github.event.workflow_run.pull_requests.*.number", gate)

    def test_the_relay_and_the_dispatch_review_still_reach_the_review_jobs(self):
        text = self.rendered([])
        reserve = text[text.index("\n  reserve:\n"):text.index("\n    concurrency:", text.index("\n  reserve:\n"))]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", reserve)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.review", reserve)

    def test_the_install_commit_carries_the_rendered_trigger_file(self):
        listing, remote = fake_repo_workflows({"ci.yml": WORKFLOW_FILES["ci.yml"]})
        with listing, remote, mock.patch.object(cli, "required_contexts", return_value={"build"}), mock.patch.object(cli, "template_text", side_effect=lambda name: (ROOT / "templates" / name).read_text()):
            files = cli.wanted_trigger_files({"repo": "o/r", "branch": "main"})
        self.assertEqual(self.workflows_list(files["sheriff.yml"]), ["Sheriff Relay", "CI"])
        self.assertEqual(files["sheriff-relay.yml"], (ROOT / "templates/sheriff-relay.yml").read_text())


class InstallCommitTests(unittest.TestCase):
    def test_the_trigger_files_arrive_as_one_commit_and_only_our_own_install_prs_are_closed(self):
        posted, closed = [], []
        listed = [{"number": 1, "headRefName": "sheriff-install-20261122000000-abc123", "title": "Install Sheriff", "author": {"login": "me"}},
                  {"number": 2, "headRefName": "sheriff-installer-docs", "title": "Install Sheriff", "author": {"login": "me"}},
                  {"number": 3, "headRefName": "sheriff-install-20261122000000-abc123", "title": "Install Sheriff", "author": {"login": "other"}},
                  {"number": 4, "headRefName": "sheriff-install-20261122000000-abc123", "title": "Install Sheriff", "author": {"login": "me"},
                   "isCrossRepository": True}]

        def fake_json(args, stdin=None):
            if args[:2] == ["pr", "list"]:
                return listed
            if "git/ref/heads/main" in args[-1]:
                return {"object": {"sha": "base"}}
            if "git/commits/base" in args[-1]:
                return {"tree": {"sha": "basetree"}}
            if args[-3:] == ["repos/o/r/git/trees", "--input", "-"]:
                posted.append(("tree", json.loads(stdin)))
                return {"sha": "tree1"}
            if args[-3:] == ["repos/o/r/git/commits", "--input", "-"]:
                posted.append(("commit", json.loads(stdin)))
                return {"sha": "commit1"}
            return {"html_url": "https://github.com/o/r/pull/5", "number": 5}

        def fake_gh(args, stdin=None):
            if args[:2] == ["pr", "close"]:
                closed.append(args[2])
            if any(arg.endswith("/git/refs") for arg in args):
                posted.append(("ref", args))
            return "me\n" if args[:2] == ["api", "user"] else ""
        with mock.patch.object(cli, "template_text", side_effect=lambda name: name), \
             mock.patch.object(cli, "remote_file", return_value=None), \
             mock.patch.object(cli, "gh_json", side_effect=fake_json), mock.patch.object(cli, "gh", side_effect=fake_gh), \
             mock.patch.object(cli, "verify_install_pr") as verify, mock.patch("builtins.print"):
            cli.ensure_trigger_files({"repo": "o/r", "branch": "main"}, wait_merge=False)
        self.assertEqual(closed, ["1"])
        self.assertEqual([kind for kind, _ in posted], ["tree", "commit", "ref"])
        self.assertEqual(posted[0][1]["base_tree"], "basetree")
        self.assertEqual(posted[1][1]["parents"], ["base"])
        self.assertIn("sha=commit1", posted[2][1])
        self.assertEqual(verify.call_args[0][2], "commit1")


class GateKeyTests(unittest.TestCase):
    def test_a_missing_key_fails_fast_when_nobody_can_answer(self):
        with mock.patch.object(cli, "env_secret_names", return_value=set()), mock.patch("builtins.input", side_effect=AssertionError("no prompt")), \
             mock.patch("builtins.print"), self.assertRaises(cli.SheriffError) as raised:
            cli.ensure_gate_app({"repo": "acme/web", "owner": "acme", "kind": "Organization"}, False, interactive=False)
        self.assertIn("without --yes", str(raised.exception))


class InstallationTests(unittest.TestCase):
    ORG = {"owner": "acme", "repo": "acme/web", "kind": "Organization"}
    INSTALLATIONS = [{"installations": [{"id": 9, "app_slug": "acme-sheriff", "repository_selection": "selected"}]}]

    def test_a_repository_outside_the_installation_fails_fast_with_the_settings_page(self):
        with mock.patch.object(cli, "gh_json", side_effect=[self.INSTALLATIONS, [{"repositories": [{"full_name": "acme/other"}]}]]), \
             mock.patch("builtins.print"), self.assertRaises(cli.SheriffError) as raised:
            cli.ensure_installed(self.ORG, None, None, wait=False)
        self.assertIn("/organizations/acme/settings/installations/9", str(raised.exception))

    def test_an_unreadable_repository_list_leaves_the_check_to_the_first_run(self):
        with mock.patch.object(cli, "gh_json", side_effect=[self.INSTALLATIONS, cli.SheriffError("403")]), mock.patch("builtins.print"):
            cli.ensure_installed(self.ORG, None, None, wait=False)

    def test_a_covered_repository_passes(self):
        with mock.patch.object(cli, "gh_json", side_effect=[self.INSTALLATIONS, [{"repositories": [{"full_name": "acme/web"}]}]]), \
             mock.patch("builtins.print"):
            cli.ensure_installed(self.ORG, None, None, wait=False)


class AutoAddTests(unittest.TestCase):
    def workflows(self, enabled):
        return {"node": {"workflows": {"nodes": [{"name": "Auto-add sub-issues to project", "enabled": True},
                                                 {"name": "Auto-add to project", "enabled": enabled}]}}}

    def test_only_the_auto_add_workflow_counts(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), mock.patch.object(cli.WORK, "gql", return_value=self.workflows(enabled)), \
                 mock.patch("builtins.print"):
                self.assertEqual(cli.wait_for_auto_add(ORG, {"id": "P1", "url": "u"}, False), enabled)

    def test_doctor_reports_auto_add_off(self):
        def fake(query, variables=None):
            if "workflows" in query:
                return self.workflows(False)
            if "projectsV2" in query:
                return {"repositoryOwner": {"projectsV2": {"nodes": [{"id": "P1", "title": "r work", "url": "u"}]}}}
            return {"repository": repository(["Stage", "Verify by", "Next action"])}
        with mock.patch.object(cli.WORK, "gql", fake):
            self.assertEqual(cli.work_state_problems(ORG, "r work"), ["'Auto-add to project' is off on the board: turn it on at u/workflows"])


class WorkStateDoctorTests(unittest.TestCase):
    def test_missing_fields_and_project_are_reported(self):
        def fake(query, variables=None):
            return {"repositoryOwner": {"projectsV2": {"nodes": [{"title": "Other"}]}}} if "projectsV2" in query else {"repository": repository(["Stage"])}
        with mock.patch.object(cli.WORK, "gql", fake):
            problems = cli.work_state_problems(ORG, "r work")
        self.assertEqual(problems, ["no issue field 'Verify by'", "no issue field 'Next action'", "no project 'r work' owned by o"])

    def test_unreadable_projects_name_the_scope(self):
        def fake(query, variables=None):
            if "projectsV2" in query:
                raise cli.WORK.WorkError("INSUFFICIENT_SCOPES")
            return {"repository": repository(["Stage", "Verify by", "Next action"])}
        with mock.patch.object(cli.WORK, "gql", fake):
            self.assertIn("gh auth refresh -s project", cli.work_state_problems(ORG, "r work")[0])


if __name__ == "__main__":
    unittest.main()
