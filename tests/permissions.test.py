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


class TemplatePermissionTests(unittest.TestCase):
    def test_the_workflow_grants_only_reads(self):
        self.assertNotIn("write", top_level(TEMPLATE).values())

    def test_every_job_declares_its_permissions_and_only_the_work_state_job_writes(self):
        found = jobs(TEMPLATE)
        self.assertEqual(set(found), {"gate", "reserve", "review", "post", "state"})
        for name, grants in found.items():
            with self.subTest(job=name):
                self.assertTrue(grants, name + " declares no permissions")
                writes = {scope for scope, level in grants.items() if level == "write"}
                self.assertEqual(writes, {"issues"} if name == "state" else set())

    def test_secrets_go_to_steps_by_name_never_inherited(self):
        self.assertNotIn("secrets: inherit", TEMPLATE)

    def test_the_relay_holds_no_permissions(self):
        self.assertIn("permissions: {}", (ROOT / "templates/sheriff-relay.yml").read_text())


if __name__ == "__main__":
    unittest.main()
