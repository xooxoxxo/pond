#!/usr/bin/env python3
"""Pre-publish check for a Pond release tag.

    python3 scripts/check-release.py v1.15.0 [--root .]

Refuses (exit 1, one line per problem on stderr) unless:
  - the tag is vMAJOR.MINOR.PATCH,
  - it names mailbox.VERSION (read from mailbox/__init__.py as text, never imported:
    `mailbox` is also a standard-library module name),
  - CHANGELOG.md has a `## MAJOR.MINOR.PATCH` section with some text in it.

On success it prints the version (no `v`) on stdout, so a workflow can use it.
"""
import argparse
import re
import sys
from pathlib import Path

TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
VERSION_LINE = re.compile(r'^VERSION\s*=\s*"([^"]*)"\s*$', re.MULTILINE)


def package_version(init_text: str) -> str:
    """The one `VERSION = "..."` line of mailbox/__init__.py."""
    found = VERSION_LINE.findall(init_text)
    if len(found) != 1:
        raise ValueError(f"mailbox/__init__.py: expected one VERSION line, found {len(found)}")
    return found[0]


def changelog_section(changelog_text: str, version: str) -> str:
    """The body of the `## <version>` section, stripped; '' when the section is empty.
    Raises ValueError when there is no such heading."""
    lines = changelog_text.splitlines()
    heading = f"## {version}"
    starts = [i for i, line in enumerate(lines)
              if line.rstrip() == heading or line.startswith(heading + " ")]
    if not starts:
        raise ValueError(f"CHANGELOG.md: no '{heading}' section")
    if len(starts) > 1:
        raise ValueError(f"CHANGELOG.md: '{heading}' appears {len(starts)} times")
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def check(tag: str, init_text: str, changelog_text: str) -> list[str]:
    """Every problem with releasing `tag` from this tree; [] when it may be released."""
    problems = []
    if not TAG.fullmatch(tag):
        return [f"tag {tag!r} is not vMAJOR.MINOR.PATCH"]
    version = tag[1:]
    try:
        package = package_version(init_text)
        if package != version:
            problems.append(f"tag {tag} does not match mailbox.VERSION {package!r}")
    except ValueError as err:
        problems.append(str(err))
    try:
        if not changelog_section(changelog_text, version):
            problems.append(f"CHANGELOG.md: the '## {version}' section is empty")
    except ValueError as err:
        problems.append(str(err))
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, e.g. v1.15.0")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    init_text = (root / "mailbox" / "__init__.py").read_text(encoding="utf-8")
    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    problems = check(args.tag, init_text, changelog_text)
    for problem in problems:
        print(f"check-release: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(args.tag[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
