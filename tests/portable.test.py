#!/usr/bin/env python3
import hashlib
import os
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIPPED = ("scripts/*.py", "actions/*/action.yml", ".github/workflows/gate.yml", ".github/workflows/review.yml",
           ".github/workflows/state.yml", "templates/*.yml", "prompts/*.md")
FILES = sorted(p for pattern in SHIPPED for p in ROOT.glob(pattern))
SELF = re.compile(r"(?:https://github\.com/)?nick-the-dev/gh-sheriff")
FORBIDDEN = (
    (re.compile(r"nick-the-dev"), "an owner other than the Sheriff repository itself"),
    (re.compile(r"https?://(?!api\.github\.com|github\.com/(?:rhysd/actionlint|zizmorcore/zizmor)/releases/)"), "a URL"),
    (re.compile(r"\b\d{6,}\b"), "an id"),
    (re.compile(r"(?<![\w-])(?:ref|branches):\s*\[?\s*main\b"), "a hard-coded branch"),
)
PRIVATE = {item.strip() for item in os.environ.get("SHERIFF_PRIVATE_HASHES", "").split(",") if item.strip()}
ALLOWED = re.compile(r"\b[0-9a-f]{40}\b|\b[0-9a-f]{64}\b")


EVERYTHING = sorted(p for pattern in ("gh-sheriff", "*.md", "tests/*.py", "scripts/*.py", "actions/*/action.yml",
                                        ".github/workflows/*.yml", "templates/*.yml", "prompts/*.md") for p in ROOT.glob(pattern))


def private_names(path):
    return [str(path.relative_to(ROOT)) + ":" + str(number) + ": a private project or account name"
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if any(hashlib.sha256(word.encode()).hexdigest()[:16] in PRIVATE for word in re.findall(r"[a-z0-9-]+", line.lower()))]


def violations(path):
    found = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        checked = ALLOWED.sub("", SELF.sub("", line))
        if path.suffix == ".py" and checked.strip().startswith("#"):
            continue
        for pattern, reason in FORBIDDEN:
            if pattern.search(checked):
                found.append(str(path.relative_to(ROOT)) + ":" + str(number) + ": " + reason)
    return found


class PortableTests(unittest.TestCase):
    def test_sheriff_names_no_project_owner_url_id_or_branch(self):
        found = [item for path in FILES for item in violations(path)]
        self.assertEqual(found, [], "\n".join(found))

    @unittest.skipUnless(PRIVATE, "set SHERIFF_PRIVATE_HASHES to comma-separated sha256 prefixes of names that must never ship")
    def test_no_file_names_a_private_project_or_account(self):
        found = [item for path in EVERYTHING for item in private_names(path)]
        self.assertEqual(found, [], "\n".join(found))

    def test_the_scan_covers_every_kind_of_file(self):
        names = {p.relative_to(ROOT).as_posix() for p in FILES}
        self.assertTrue({"scripts/gate-status.py", "scripts/work.py", "actions/review/action.yml", "actions/state/action.yml",
                         ".github/workflows/gate.yml", ".github/workflows/state.yml", "templates/sheriff.yml"} <= names)


if __name__ == "__main__":
    unittest.main()
