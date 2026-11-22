#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reviewers", ROOT / "scripts/reviewers.py")
reviewers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewers)


class SpecTests(unittest.TestCase):
    def test_provider_and_model_are_parsed_and_a_bare_model_means_claude(self):
        self.assertEqual(reviewers.parse_spec("opencode:opencode-go/deepseek-v4.1-flash"), ("opencode", "opencode-go/deepseek-v4.1-flash"))
        self.assertEqual(reviewers.parse_spec("claude-haiku-5-5"), ("claude", "claude-haiku-5-5"))
        self.assertIsNone(reviewers.parse_spec(""))
        for bad in ("gemini:x", "claude:", "claude:two words"):
            with self.subTest(bad=bad), self.assertRaises(reviewers.ReviewerError):
                reviewers.parse_spec(bad)


class OutputTests(unittest.TestCase):
    def test_claude_result_and_usage(self):
        raw = json.dumps({"result": "ok\nVERDICT: CLEAN", "is_error": False, "total_cost_usd": 0.5,
                          "modelUsage": {"claude-opus-5-5": {"inputTokens": 3, "outputTokens": 4, "cacheReadInputTokens": 5,
                                                             "cacheCreationInputTokens": 6, "thinkingTokens": 7}}})
        text, usage = reviewers.claude_result(raw)
        self.assertEqual(text, "ok\nVERDICT: CLEAN")
        self.assertEqual(usage, {"input": 3, "cache_read": 5, "cache_write": 6, "output": 4, "reasoning": 7, "cost_usd": 0.5})

    def test_opencode_takes_the_last_real_step_and_sums_every_step(self):
        events = [{"type": "step_start", "part": {}}, {"type": "text", "part": {"text": "draft"}},
                  {"type": "step_finish", "part": {"tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 0}}, "cost": 0.01}},
                  {"type": "text", "part": {"text": "Continue if you have next steps", "synthetic": True}},
                  {"type": "step_start", "part": {}}, {"type": "text", "part": {"text": "final\nVERDICT: BLOCKED"}},
                  {"type": "step_finish", "part": {"tokens": {"input": 5, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 4}}, "cost": 0.02}}]
        text, usage = reviewers.opencode_result("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
        self.assertEqual(text, "final\nVERDICT: BLOCKED")
        self.assertEqual((usage["input"], usage["output"], usage["cache_read"], usage["cache_write"]), (15, 3, 3, 4))
        self.assertAlmostEqual(usage["cost_usd"], 0.03)

    def test_empty_output_is_an_error(self):
        with self.assertRaises(reviewers.ReviewerError):
            reviewers.opencode_result("")
        with self.assertRaises(reviewers.ReviewerError):
            reviewers.claude_result(json.dumps({"result": "", "is_error": True}))


class LockdownTests(unittest.TestCase):
    def test_claude_gets_read_only_tools_and_denied_paths(self):
        command = reviewers.claude_command("claude-opus-5-5", "p")
        self.assertIn("Bash", command[command.index("--disallowedTools"):])
        self.assertIn("Read(//proc/**)", command)
        self.assertIn("Read(./**)", command[command.index("--allowedTools"):command.index("--disallowedTools")])

    def test_opencode_agent_denies_everything_but_reading_the_workspace(self):
        permission = reviewers.opencode_config()["agent"]["sheriff-review"]["permission"]
        for name in ("edit", "bash", "webfetch", "websearch", "task", "external_directory"):
            self.assertEqual(permission[name], "deny")
        self.assertEqual(permission["read"]["pr/.git/**"], "deny")

    def test_the_model_process_gets_only_its_own_secret(self):
        seen = {}
        fake = mock.Mock(returncode=0, stdout=json.dumps({"result": "x\nVERDICT: CLEAN", "modelUsage": {}}))
        environ = {"PATH": "/bin", "HOME": "/home/x", "CLAUDE_CODE_OAUTH_TOKEN": "t", "GH_TOKEN": "gh", "OPENCODE_API_KEY": "o"}
        with mock.patch.object(reviewers.subprocess, "run", side_effect=lambda *a, **k: seen.update(k["env"]) or fake):
            reviewers.run(("claude", "claude-opus-5-5"), "p", ".", environ)
        self.assertEqual(sorted(seen), ["CLAUDE_CODE_OAUTH_TOKEN", "HOME", "PATH"])

    def test_a_missing_secret_fails_before_running(self):
        with self.assertRaises(reviewers.ReviewerError):
            reviewers.run(("opencode", "opencode-go/x"), "p", ".", {"PATH": "/bin"})


class WebTests(unittest.TestCase):
    def test_web_modes_map_to_claude_tools(self):
        self.assertEqual(reviewers.web_tools("", ""), ("WebSearch",))
        self.assertEqual(reviewers.web_tools("search", "x.org"), ("WebSearch",))
        self.assertEqual(reviewers.web_tools("allowlist", "docs.python.org, *.github.com"),
                         ("WebSearch", "WebFetch(domain:docs.python.org)", "WebFetch(domain:*.github.com)"))
        self.assertEqual(reviewers.web_tools("any", ""), ("WebSearch", "WebFetch"))

    def test_bad_web_settings_are_refused(self):
        for mode, domains in (("all", ""), ("allowlist", ""), ("allowlist", "a.org) Bash(x"), ("allowlist", "localhost")):
            with self.subTest(mode=mode, domains=domains), self.assertRaises(reviewers.ReviewerError):
                reviewers.web_tools(mode, domains)

    def test_a_planner_gets_its_web_tools_and_turns_but_never_a_shell(self):
        command = reviewers.claude_command("m", "p", turns=40, web=("WebSearch",))
        allowed, denied = command[command.index("--allowedTools"):command.index("--disallowedTools")], command[command.index("--disallowedTools"):]
        self.assertIn("WebSearch", allowed)
        self.assertNotIn("WebSearch", denied)
        self.assertTrue({"Bash", "Write", "Edit", "NotebookEdit", "Task", "WebFetch"} <= set(denied))
        self.assertEqual(command[command.index("--max-turns") + 1], "40")

    def test_a_fetch_allowlist_keeps_bare_webfetch_off_the_deny_list(self):
        command = reviewers.claude_command("m", "p", web=("WebSearch", "WebFetch(domain:a.org)"))
        self.assertIn("WebFetch(domain:a.org)", command[:command.index("--disallowedTools")])
        self.assertNotIn("WebFetch", command[command.index("--disallowedTools"):])

    def test_the_reviewer_command_is_unchanged_by_default(self):
        command = reviewers.claude_command("m", "p")
        self.assertTrue({"WebFetch", "WebSearch"} <= set(command[command.index("--disallowedTools"):]))
        self.assertEqual(command[command.index("--max-turns") + 1], "80")

    def test_only_web_tools_can_be_granted(self):
        command = reviewers.claude_command("m", "p", web=("Bash", "Write", "WebSearch"))
        self.assertNotIn("Bash", command[:command.index("--disallowedTools")])
        self.assertNotIn("Write", command[:command.index("--disallowedTools")])
        self.assertTrue({"Bash", "Write"} <= set(command[command.index("--disallowedTools"):]))
        self.assertIn("WebSearch", command[:command.index("--disallowedTools")])

    def test_the_claude_run_ignores_user_settings(self):
        command = reviewers.claude_command("m", "p")
        self.assertEqual(command[command.index("-p") + 2:command.index("-p") + 4], ["--setting-sources", "project"])

    def test_opencode_takes_the_turn_budget_and_never_the_web(self):
        agent = reviewers.opencode_config(40)["agent"]["sheriff-review"]
        self.assertEqual((agent["steps"], agent["permission"]["webfetch"], agent["permission"]["websearch"]), (40, "deny", "deny"))

    def test_run_hands_turns_and_web_to_claude(self):
        seen, fake = [], mock.Mock(returncode=0, stdout=json.dumps({"result": "x", "modelUsage": {}}))
        with mock.patch.object(reviewers.subprocess, "run", side_effect=lambda command, **k: seen.append(command) or fake):
            reviewers.run(("claude", "m"), "p", ".", {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, turns=40, web=("WebSearch",))
        self.assertIn("WebSearch", seen[0][:seen[0].index("--disallowedTools")])
        self.assertEqual(seen[0][seen[0].index("--max-turns") + 1], "40")


if __name__ == "__main__":
    unittest.main()
