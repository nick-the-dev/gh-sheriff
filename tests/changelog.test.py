#!/usr/bin/env python3
import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("changelog", ROOT / "scripts/changelog.py")
changelog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changelog)
SAMPLE = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-11-25

### Added

- New thing.

## [1.0.0] - 2026-11-23

- First.

[Unreleased]: https://example/compare/v1.1.0...HEAD
[1.1.0]: https://example/compare/v1.0.0...v1.1.0
"""


class ChangelogTests(unittest.TestCase):
    def test_notes_are_one_versions_entry_without_the_link_list(self):
        self.assertEqual(changelog.notes("1.1.0", SAMPLE), "### Added\n\n- New thing.")
        self.assertEqual(changelog.notes("1.0.0", SAMPLE), "- First.")

    def test_a_missing_undated_or_empty_entry_is_refused(self):
        for version in ("9.9.9", "Unreleased"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                changelog.notes(version, SAMPLE)

    def test_the_cli_version_has_a_dated_entry_and_a_compare_link(self):
        version = re.search(r'^VERSION = "([^"]+)"', (ROOT / "gh-sheriff").read_text(), re.M).group(1)
        self.assertTrue(changelog.notes(version))
        self.assertIn("\n[" + version + "]: https://github.com/nick-the-dev/gh-sheriff/", changelog.CHANGELOG.read_text())


if __name__ == "__main__":
    unittest.main()
