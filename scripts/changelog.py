#!/usr/bin/env python3
"""Release notes from CHANGELOG.md: `changelog.py notes X.Y.Z` prints that version's entry for `gh release create
--notes-file -`, and fails when the entry is missing, undated or empty."""
import pathlib
import re
import sys

CHANGELOG = pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md"
HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)


def entry(text, version):
    found = [m for m in HEADING.finditer(text)]
    for index, match in enumerate(found):
        if match.group(1) == version:
            end = found[index + 1].start() if index + 1 < len(found) else len(text)
            body = re.split(r"^\[[^\]]+\]: ", text[match.end():end], maxsplit=1, flags=re.M)[0].strip()
            return match.group(2), body
    return None, ""


def notes(version, text=None):
    date, body = entry(CHANGELOG.read_text() if text is None else text, version)
    if not date or not body:
        raise ValueError("CHANGELOG.md has no dated entry with notes for " + version)
    return body


def main(argv):
    if len(argv) != 3 or argv[1] != "notes":
        print("usage: changelog.py notes X.Y.Z", file=sys.stderr)
        return 2
    try:
        print(notes(argv[2].removeprefix("v")))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
