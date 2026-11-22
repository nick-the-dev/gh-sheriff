#!/usr/bin/env python3
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/sheriff.yml").read_text()


def top_level(text):
    grants, inside = {}, False
    for line in text.splitlines():
        if line.startswith("permissions:"):
            inside = True
            continue
        if inside:
            match = re.match(r"^\s{2}([a-z-]+):\s*(read|write)\s*$", line)
            if not match:
                break
            grants[match.group(1)] = match.group(2)
    return grants


def jobs(text):
    found, name, inside = {}, None, False
    for line in text[text.index("\njobs:\n"):].splitlines():
        header = re.match(r"^  ([a-z-]+):\s*$", line)
        if header:
            name, inside = header.group(1), False
            found[name] = {}
            continue
        if name and line == "    permissions:":
            inside = True
            continue
        if name and inside:
            match = re.match(r"^\s{6}([a-z-]+):\s*(read|write)\s*$", line)
            if not match:
                inside = False
                continue
            found[name][match.group(1)] = match.group(2)
    return found


def block(text, job):
    start = text.index("\n  " + job + ":\n")
    following = re.search(r"\n  [a-z-]+:\n", text[start + 1:])
    return text[start:start + 1 + following.start()] if following else text[start:]


def run_blocks(text):
    lines, found = text.splitlines(), []
    for number, line in enumerate(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", line)
        if not match:
            continue
        indent, rest = len(match.group(1)), match.group(2)
        if rest not in ("|", "|-", ">", ">-"):
            found.append(rest)
            continue
        body = []
        for following in lines[number + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        found.append("\n".join(body))
    return found


def step_of(text, marker):
    return text[text[:text.index(marker)].rindex("    - name:"):text.index(marker)]


class TemplatePermissionTests(unittest.TestCase):
    def test_the_workflow_grants_only_reads(self):
        self.assertNotIn("write", top_level(TEMPLATE).values())

    def test_every_job_declares_its_permissions_and_only_work_state_and_plan_post_write(self):
        found = jobs(TEMPLATE)
        self.assertEqual(set(found), {"gate", "reserve", "review", "post", "state", "plan-reserve", "plan", "plan-post"})
        for name, grants in found.items():
            with self.subTest(job=name):
                self.assertTrue(grants)
                self.assertEqual({s for s, level in grants.items() if level == "write"}, {"state": {"issues"}, "plan-post": {"actions"}}.get(name, set()))
        self.assertEqual(found["plan"], {"contents": "read", "issues": "read"})
        self.assertEqual(found["review"].get("issues"), "read")

    def test_no_job_gets_both_the_app_key_and_a_model_key(self):
        for name in jobs(TEMPLATE):
            text = block(TEMPLATE, name)
            with self.subTest(job=name):
                app = "secrets.SHERIFF_APP_KEY_B64" in text
                model = "secrets.CLAUDE_CODE_OAUTH_TOKEN" in text or "secrets.OPENCODE_API_KEY" in text
                self.assertFalse(app and model)
                if app or model:
                    self.assertIn("      name: " + ("sheriff" if app else "sheriff-review") + "\n", text)

    def test_secrets_go_to_steps_by_name_never_inherited(self):
        self.assertNotIn("secrets: inherit", TEMPLATE)

    def test_the_relay_holds_no_permissions(self):
        self.assertIn("permissions: {}", (ROOT / "templates/sheriff-relay.yml").read_text())


class PlanWiringTests(unittest.TestCase):
    def test_plan_jobs_use_the_default_branch_environments_and_budgets(self):
        for job, environment in (("plan-reserve", "sheriff"), ("plan", "sheriff-review"), ("plan-post", "sheriff")):
            self.assertIn("      name: " + environment + "\n", block(TEMPLATE, job))
        self.assertIn("timeout-minutes: 25", block(TEMPLATE, "plan"))
        self.assertIn("vars.SHERIFF_PLAN_TURNS || '40'", block(TEMPLATE, "plan"))

    def test_planning_runs_only_when_switched_on_and_never_for_pull_requests(self):
        condition = block(TEMPLATE, "plan-reserve")
        for part in ("vars.SHERIFF_PLAN == 'on'", "!github.event.issue.pull_request", "github.event.action == 'created'",
                     "'/sheriff plan'", "'/sheriff replan'", "github.event.action == 'opened'"):
            self.assertIn(part, condition)
        self.assertIn("  issues:\n    types: [opened]\n", TEMPLATE)

    def test_issue_events_start_neither_the_gate_nor_the_work_state(self):
        for job in ("gate", "state"):
            self.assertIn("github.event_name != 'issues'", block(TEMPLATE, job))

    def test_the_planner_step_gets_the_model_keys_and_no_github_token(self):
        action = (ROOT / "actions/plan/action.yml").read_text()
        step = action[action.index("plan.py\" run") - 1500:action.index("plan.py\" run")]
        step = step[step.rindex("    - name:"):]
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", step)
        self.assertNotIn("GH_TOKEN", step)
        self.assertIn("persist-credentials: false", action)
        self.assertIn("find base -type l -delete", action)

    def test_a_persistent_workspace_is_cleared_before_any_checkout_or_prepare(self):
        for name, cleanup, prepare in (("plan", "rm -rf pr review plan", "plan.py\" prepare"), ("review", "rm -rf review plan", "review.py\" prepare")):
            action = (ROOT / "actions" / name / "action.yml").read_text()
            with self.subTest(action=name):
                self.assertIn(cleanup, run_blocks(action))
                self.assertLess(action.index("run: " + cleanup), action.index("uses: actions/checkout@"))
                self.assertLess(action.index("run: " + cleanup), action.index(prepare))

    def test_the_plan_action_takes_the_ack_list_as_an_input_through_env(self):
        action = (ROOT / "actions/plan/action.yml").read_text()
        self.assertIn("\n  ack-authors:\n", action)
        self.assertIn("SHERIFF_ACK_AUTHORS: ${{ inputs.ack-authors }}", action)
        self.assertNotIn("${{ vars.", action)
        self.assertIn("ack-authors: ${{ vars.SHERIFF_ACK_AUTHORS }}", block(TEMPLATE, "plan"))

    def test_plan_tokens_are_narrowed(self):
        for name, call in (("plan-reserve", "plan.py\" reserve"), ("plan-post", "plan.py\" post")):
            text = (ROOT / "actions" / name / "action.yml").read_text()
            mine = text[text[:text.index(call)].rindex("app-token.py"):text.index(call)]
            self.assertEqual(dict(re.findall(r"--permission (\w+)=(\w+)", mine)).get("issues"), "write")

    def test_the_reserve_and_review_actions_default_to_planning_off(self):
        for name, inputs in (("reserve", {"plan": '""'}), ("review", {"plan-issue": '"0"', "plan-version": '"0"', "app": '""'})):
            text = (ROOT / "actions" / name / "action.yml").read_text()
            for key, default in inputs.items():
                with self.subTest(action=name, input=key):
                    section = text[text.index("\n  " + key + ":\n"):]
                    self.assertIn("required: false", section[:200])
                    self.assertIn("default: " + default, section[:200])

    def test_plan_post_dispatches_a_review_for_each_linked_pull_request(self):
        text = (ROOT / "actions/plan-post/action.yml").read_text()
        self.assertIn("gh workflow run sheriff.yml", text)
        self.assertIn("-f review=true", text)
        self.assertIn("steps.post.outputs.posted == 'true'", text)

    def test_the_reserve_step_passes_the_event_action_through_env(self):
        action = (ROOT / "actions/plan-reserve/action.yml").read_text()
        name = re.search(r"\n\s+(\w+): \$\{\{ github\.event\.action \}\}\n", action).group(1)
        self.assertIn('--event "$' + name + '"', action)

    def test_only_reservations_queue_per_issue_and_later_comments_never_cancel_a_plan_run(self):
        reserve = block(TEMPLATE, "plan-reserve")
        self.assertEqual(re.findall(r"\n      group: (.+)\n", reserve), ["sheriff-plan-${{ github.event.issue.number || inputs.plan_issue }}"])
        self.assertIn("\n      cancel-in-progress: false\n", reserve)
        for job in ("plan", "plan-post"):
            with self.subTest(job=job):
                self.assertNotIn("concurrency:", block(TEMPLATE, job))

    def test_every_reservation_is_released_even_after_a_failed_timed_out_or_cancelled_plan(self):
        text = block(TEMPLATE, "plan-post")
        condition = text[text.index("\n    if:"):text.index("\n    runs-on:")]
        for part in ("always()", "needs.plan-reserve.result == 'success'", "needs.plan-reserve.outputs.skip == ''"):
            self.assertIn(part, condition)
        self.assertNotIn("needs.plan.", condition)

    def test_workflow_values_reach_action_scripts_only_through_env(self):
        for path in sorted(ROOT.glob("actions/*/action.yml")):
            for script in run_blocks(path.read_text()):
                with self.subTest(action=path.parent.name):
                    self.assertNotIn("${{", script)

    def test_the_gate_post_and_review_jobs_carry_the_plan_settings(self):
        for job in ("gate", "post", "plan-post"):
            with self.subTest(job=job):
                self.assertIn("SHERIFF_PLAN: ${{ vars.SHERIFF_PLAN }}", block(TEMPLATE, job))
                self.assertIn("SHERIFF_PLAN_SINCE: ${{ vars.SHERIFF_PLAN_SINCE }}", block(TEMPLATE, job))
        reserve, review = block(TEMPLATE, "reserve"), block(TEMPLATE, "review")
        self.assertIn("plan: ${{ vars.SHERIFF_PLAN }}", reserve)
        for output in ("plan_issue", "plan_version", "app"):
            self.assertIn(output + ": ${{ steps.reserve.outputs." + output + " }}", reserve)
        for key, output in (("plan-issue", "plan_issue"), ("plan-version", "plan_version"), ("app", "app")):
            self.assertIn(key + ": ${{ needs.reserve.outputs." + output + " }}", review)

    def test_plan_post_rechecks_the_issue_and_starts_a_plan_run_for_a_command_that_arrived_meanwhile(self):
        text = (ROOT / "actions/plan-post/action.yml").read_text()
        self.assertLess(text.index("plan.py\" post"), text.index("plan.py\" decide"))
        self.assertIn("!cancelled()", step_of(text, "plan.py\" decide"))
        self.assertIn('-f plan_issue="$ISSUE"', text)
        self.assertIn("\n      plan_issue:\n", TEMPLATE)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.plan_issue != ''", block(TEMPLATE, "plan-reserve"))
        for job in ("gate", "state"):
            with self.subTest(job=job):
                self.assertIn("!inputs.plan_issue", block(TEMPLATE, job))


class GateTokenTests(unittest.TestCase):
    def test_the_gate_token_can_read_the_check_rollup_that_open_rows_requests(self):
        source = (ROOT / "scripts/gate-status.py").read_text()
        self.assertIn("statusCheckRollup", source[source.index("def open_rows"):])
        action = (ROOT / "actions/gate/action.yml").read_text()
        narrowed = dict(re.findall(r"--permission (\w+)=(\w+)", action))
        self.assertEqual(narrowed.get("checks"), "read")
        self.assertEqual(narrowed.get("statuses"), "write")


if __name__ == "__main__":
    unittest.main()
