#!/usr/bin/env python3
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE = re.compile(r"^\s{6}([a-z-]+):\s*(read|write)\s*$")
LEVEL = {"read": 1, "write": 2}


def granted(text):
    lines, grants, inside = text.splitlines(), {}, False
    for line in lines:
        if line.startswith("permissions:"):
            inside = True
            continue
        if inside:
            match = re.match(r"^\s{2}([a-z-]+):\s*(read|write)\s*$", line)
            if not match:
                break
            grants[match.group(1)] = match.group(2)
    return grants


def caller_grants(text, job):
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "  " + job + ":")
    grants, inside = {}, False
    for line in lines[start + 1:]:
        if re.match(r"^  [a-z-]+:", line):
            break
        if line == "    permissions:":
            inside = True
            continue
        if inside:
            match = re.match(r"^\s{6}([a-z-]+):\s*(read|write)\s*$", line)
            if not match:
                break
            grants[match.group(1)] = match.group(2)
    return grants or granted(text)


def requested(text):
    found = {}
    for line in text.splitlines():
        match = SCOPE.match(line)
        if match:
            name, level = match.groups()
            if LEVEL[level] > LEVEL.get(found.get(name, "read"), 0) or name not in found:
                found[name] = level
    return found


class CallerPermissionTests(unittest.TestCase):
    def test_no_shared_job_asks_for_more_than_the_trigger_file_grants(self):
        template = (ROOT / "templates/sheriff.yml").read_text()
        for workflow in ("gate.yml", "review.yml", "state.yml"):
            grants = caller_grants(template, workflow.split(".")[0])
            for name, level in requested((ROOT / ".github/workflows" / workflow).read_text()).items():
                with self.subTest(workflow=workflow, permission=name):
                    self.assertIn(name, grants, workflow + " asks for " + name + " but sheriff.yml does not grant it; that is a new major version")
                    self.assertGreaterEqual(LEVEL[grants[name]], LEVEL[level])

    def test_only_issues_are_writable_and_only_the_work_state_job_writes_them(self):
        template = (ROOT / "templates/sheriff.yml").read_text()
        self.assertNotIn("write", granted(template).values())
        self.assertNotIn("write", caller_grants(template, "gate").values())
        self.assertNotIn("write", caller_grants(template, "review").values())
        self.assertEqual({name for name, level in caller_grants(template, "state").items() if level == "write"}, {"issues"})
        for workflow in ("gate.yml", "review.yml"):
            with self.subTest(workflow=workflow):
                self.assertNotIn("write", requested((ROOT / ".github/workflows" / workflow).read_text()).values())

    def test_the_relay_holds_no_permissions(self):
        self.assertIn("permissions: {}", (ROOT / "templates/sheriff-relay.yml").read_text())


if __name__ == "__main__":
    unittest.main()
