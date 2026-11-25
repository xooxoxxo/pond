"""Write optional-vectors.json: the optional-* and file-* cases of the shared
validator vectors, computed by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_optional_vectors                   # from the repo root
    python3 -m mailbox.tests.gen_optional_vectors --hook <fixture>  # also into Hook's fixture

With --hook, the same cases replace every optional-* and file-* case in Hook's
Tests/RelayCoreTests/Fixtures/instructable-vectors.json (at the place the first
one stood, or at the end), leaving every other case as it was, so the Swift
validator is held to the same answers.
"""
import argparse
import json
from pathlib import Path

from mailbox.instructable import validate

OUT = Path(__file__).parent / "optional-vectors.json"
PREFIXES = ("optional-", "file-")

TITLE = "Collect feedback on the release"
LONG_WARNING = "w" * 301


def _ask(step: dict | None = None, *, steps: list | None = None, verify: dict | None = None,
         links: list | None = None) -> dict:
    obj = {"v": 1, "title": TITLE,
           "steps": steps if steps is not None else [step or {"do": "Tell us what you think"},
                                                     {"do": "Ship it"}]}
    if verify is not None:
        obj["verify"] = verify
    if links is not None:
        obj["links"] = links
    return obj


def _sub(*subs: dict, **parent) -> dict:
    """A first step "Try the build" (with the `parent` keys) holding these sub-steps."""
    return _ask(steps=[{"do": "Try the build", **parent, "substeps": list(subs)}, {"do": "Ship it"}])


def _link(**keys) -> dict:
    return _ask(links=[{"label": "Release notes", **keys}])


OPEN = {"do": "Open the app"}
ODD = "Note anything odd"
READ = "Read the release notes"
NOTES = "~/Documents/release-notes.md"

INPUTS: list[tuple[str, dict]] = [
    # optional (protocol 4): the cases Hook and the mailbox each had
    ("optional-valid-true", _ask({"do": "Tell us what you think", "optional": True})),
    ("optional-valid-false", _ask({"do": "Tell us what you think", "optional": False})),
    ("optional-valid-null", _ask({"do": "Tell us what you think", "optional": None})),
    ("optional-valid-on-substep", _sub(OPEN, {"do": ODD, "optional": True})),
    ("optional-valid-with-question", _ask({"do": "Tell us what you think", "optional": True,
                                           "question": {"prompt": "Anything to change?",
                                                        "choices": ["No", "Yes"]}})),
    ("optional-valid-with-warning", _ask({"do": "Tell us what you think", "optional": True,
                                          "warning": "Takes a minute."})),
    ("optional-valid-true-blocking-false", _ask({"do": "Tell us what you think", "optional": True,
                                                 "blocking": False})),
    ("optional-valid-false-blocking-true", _ask({"do": "Tell us what you think", "optional": False,
                                                 "blocking": True})),
    ("optional-not-bool-string", _ask({"do": "Tell us what you think", "optional": "true"})),
    ("optional-not-bool-number", _ask({"do": "Tell us what you think", "optional": 1})),
    ("optional-not-bool-list", _ask({"do": "Tell us what you think", "optional": []})),
    ("optional-substep-not-bool", _sub(OPEN, {"do": ODD, "optional": "yes"})),
    ("optional-on-verify", _ask(verify={"do": "Check it", "optional": True})),
    ("optional-and-blocking", _ask({"do": "Tell us what you think", "optional": True, "blocking": True})),
    ("optional-and-blocking-substep", _sub({"do": ODD, "optional": True, "blocking": True})),
    ("optional-not-bool-and-blocking", _ask({"do": "Tell us what you think", "optional": "x",
                                             "blocking": True})),
    # R1: one error order, the reference's: blocking, optional, then warning
    ("optional-not-bool-long-warning", _ask({"do": "Tell us what you think", "optional": "x",
                                             "warning": LONG_WARNING})),
    ("optional-and-blocking-long-warning", _ask({"do": "Tell us what you think", "optional": True,
                                                 "blocking": True, "warning": LONG_WARNING})),
    ("optional-blocking-not-bool-long-warning", _ask({"do": "Tell us what you think", "optional": 1,
                                                      "blocking": "x", "warning": LONG_WARNING})),
    ("optional-substep-not-bool-long-warning", _sub(OPEN, {"do": ODD, "optional": "x",
                                                           "warning": LONG_WARNING})),
    # R2: an optional parent makes its sub-steps optional, so none may block
    ("optional-parent-blocking-substep", _sub({"do": "Open the app", "blocking": True}, optional=True)),
    ("optional-parent-blocking-second-substep", _sub(OPEN, {"do": ODD, "blocking": True}, optional=True)),
    ("optional-parent-optional-blocking-substep", _sub({"do": ODD, "optional": True, "blocking": True},
                                                       optional=True)),
    ("optional-parent-blocking-substep-not-bool", _sub({"do": ODD, "blocking": "x"}, optional=True)),
    ("optional-parent-blocking-substep-long-warning", _sub({"do": ODD, "blocking": True,
                                                            "warning": LONG_WARNING}, optional=True)),
    ("optional-parent-not-bool-blocking-substep", _sub({"do": ODD, "blocking": True}, optional="yes")),
    ("optional-valid-parent-blocking-false-substep", _sub({"do": ODD, "blocking": False}, optional=True)),
    ("optional-valid-parent-optional-substep", _sub({"do": ODD, "optional": True}, optional=True)),
    ("optional-valid-parent-false-blocking-substep", _sub({"do": ODD, "blocking": True}, optional=False)),
    ("optional-valid-parent-null-blocking-substep", _sub({"do": ODD, "blocking": True}, optional=None)),
    # R3: `file` on a step or sub-step
    ("file-valid-home", _ask({"do": READ, "file": NOTES})),
    ("file-valid-absolute", _ask({"do": READ, "file": "/var/tmp/release-notes.md"})),
    ("file-valid-root", _ask({"do": READ, "file": "/"})),
    ("file-valid-null", _ask({"do": READ, "file": None})),
    ("file-valid-unicode", _ask({"do": READ, "file": "~/Notizen/Übersicht 🚀.md"})),
    ("file-valid-500", _ask({"do": READ, "file": "/" + "a" * 496 + ".md"})),
    ("file-valid-on-substep", _sub(OPEN, {"do": READ, "file": NOTES})),
    ("file-valid-with-optional", _ask({"do": READ, "file": NOTES, "optional": True})),
    ("file-valid-with-url", _ask({"do": READ, "file": NOTES, "url": "https://example.com/notes"})),
    ("file-relative", _ask({"do": READ, "file": "notes/release.md"})),
    ("file-dot-relative", _ask({"do": READ, "file": "./release.md"})),
    ("file-tilde-user", _ask({"do": READ, "file": "~owner/release.md"})),
    ("file-tilde-only", _ask({"do": READ, "file": "~"})),
    ("file-url-scheme", _ask({"do": READ, "file": "file:///tmp/release.md"})),
    ("file-empty", _ask({"do": READ, "file": ""})),
    ("file-blank", _ask({"do": READ, "file": "   "})),
    ("file-not-string", _ask({"do": READ, "file": 42})),
    ("file-501", _ask({"do": READ, "file": "/" + "a" * 497 + ".md"})),
    ("file-multiline", _ask({"do": READ, "file": "/tmp/release\nnotes.md"})),
    ("file-tab", _ask({"do": READ, "file": "/tmp/release\tnotes.md"})),
    ("file-hidden", _ask({"do": READ, "file": "/tmp/release\u200bnotes.md"})),
    ("file-hidden-zwj", _ask({"do": READ, "file": "/tmp/a\u200db.md"})),
    ("file-hidden-variation-selector", _ask({"do": READ, "file": "/tmp/done\u2714\ufe0f.md"})),
    ("file-secret-like", _ask({"do": READ, "file": "/tmp/ghp_release.md"})),
    ("file-on-verify", _ask(verify={"do": "Check it", "file": NOTES})),
    ("file-substep-relative", _sub(OPEN, {"do": READ, "file": "release.md"})),
    ("file-relative-after-bad-url", _ask({"do": READ, "url": "ftp://example.com/x", "file": "release.md"})),
    ("file-relative-before-blocking", _ask({"do": READ, "file": "release.md", "blocking": "x", "optional": 1})),
    # P1: no "." or ".." segment; P2: not a network path (P1 reported first)
    ("file-dotdot-home", _ask({"do": READ, "file": "~/../x"})),
    ("file-dotdot-middle", _ask({"do": READ, "file": "/a/b/../c.md"})),
    ("file-dotdot-end", _ask({"do": READ, "file": "/a/.."})),
    ("file-dot-middle", _ask({"do": READ, "file": "/a/./b"})),
    ("file-dot-end", _ask({"do": READ, "file": "~/notes/."})),
    ("file-dotdot-on-substep", _sub(OPEN, {"do": READ, "file": "/tmp/../etc/hosts"})),
    ("file-net", _ask({"do": READ, "file": "/net/host/share/doc.md"})),
    ("file-net-root", _ask({"do": READ, "file": "/net"})),
    ("file-network", _ask({"do": READ, "file": "/Network/Servers/x.md"})),
    ("file-network-root", _ask({"do": READ, "file": "/Network"})),
    ("file-net-and-dotdot", _ask({"do": READ, "file": "/net/host/../doc.md"})),
    ("file-valid-dotdot-in-name", _ask({"do": READ, "file": "/a/..b/c.md"})),
    ("file-valid-dots-in-name", _ask({"do": READ, "file": "~/notes/v1.2/...md"})),
    ("file-valid-networks", _ask({"do": READ, "file": "/networks/x.md"})),
    ("file-valid-netlify", _ask({"do": READ, "file": "/netlify/x.md"})),
    ("file-valid-home-net", _ask({"do": READ, "file": "~/net/x.md"})),
    ("file-valid-trailing-slash", _ask({"do": READ, "file": "/var/tmp/"})),
    # N2: no first segment starting with "." (after P1); N1: the network test on the
    # lexical form: "//" collapsed, any case, /private, /private/var/automount or
    # /System/Volumes/Data stripped; /home (autofs) joins /net and /Network
    ("file-hidden-root-nofollow", _ask({"do": READ, "file": "/.nofollow/Users/x/a.md"})),
    ("file-hidden-root-vol", _ask({"do": READ, "file": "/.vol/1/2"})),
    ("file-hidden-root-resolve", _ask({"do": READ, "file": "/.resolve/1/2"})),
    ("file-hidden-root-double-slash", _ask({"do": READ, "file": "//.vol/1/2"})),
    ("file-hidden-root-and-dotdot", _ask({"do": READ, "file": "/.vol/../a.md"})),
    # N3 (after N2, before N1): the first segment of an absolute path must be ASCII
    ("file-nonascii-first-long-s", _ask({"do": READ, "file": "/\u017fystem/Volumes/Data/home/u/a.md"})),
    ("file-nonascii-first-ligature", _ask({"do": READ, "file": "/Sy\ufb06em/Volumes/Data/net/h/a.md"})),
    ("file-nonascii-first-kelvin", _ask({"do": READ, "file": "/\u212aext/a.md"})),
    ("file-link-path-nonascii-first", _link(path="/\u017fystem/Volumes/Data/home/u/a.md")),
    ("file-valid-nonascii-inner", _ask({"do": READ, "file": "/Users/Jos\u00e9/a.md"})),
    ("file-valid-home-nonascii", _ask({"do": READ, "file": "~/\u00dcn\u00efcode/a.md"})),
    ("file-net-upper", _ask({"do": READ, "file": "/NET/h/a.md"})),
    ("file-network-lower", _ask({"do": READ, "file": "/network/x/a.md"})),
    ("file-net-double-slash", _ask({"do": READ, "file": "//net/h/a.md"})),
    ("file-net-inner-double-slash", _ask({"do": READ, "file": "/net//h/a.md"})),
    ("file-net-private", _ask({"do": READ, "file": "/private/net/h/a.md"})),
    ("file-net-automount", _ask({"do": READ, "file": "/private/var/automount/net/h/a.md"})),
    ("file-home-data-volume", _ask({"do": READ, "file": "/System/Volumes/Data/home/u/a.md"})),
    ("file-net-data-volume-private", _ask({"do": READ, "file": "/System/Volumes/Data/private/net/h"})),
    ("file-net-data-volume-private-automount", _ask({"do": READ, "file": "/System/Volumes/Data/private/var/automount/net/h"})),
    ("file-net-private-private", _ask({"do": READ, "file": "/private/private/net/h"})),
    ("file-net-var-automount", _ask({"do": READ, "file": "/var/automount/net/h"})),
    ("file-home", _ask({"do": READ, "file": "/home/u/a.md"})),
    ("file-home-root", _ask({"do": READ, "file": "/home"})),
    ("file-valid-hidden-inner", _ask({"do": READ, "file": "/Users/x/.config/a.md"})),
    ("file-valid-homebrew", _ask({"do": READ, "file": "/homebrew/a.md"})),
    ("file-valid-networks-n1", _ask({"do": READ, "file": "/networks/a.md"})),
    ("file-valid-private-tmp", _ask({"do": READ, "file": "/private/tmp/a.md"})),
    ("file-valid-home-dotfile", _ask({"do": READ, "file": "~/.config/a.md"})),
    ("file-link-path-hidden-root", _link(path="/.nofollow/Users/x/a.md")),
    ("file-link-path-vol", _link(path="/.vol/1/2")),
    ("file-link-path-net-upper", _link(path="/NET/h/a.md")),
    ("file-link-path-network-lower", _link(path="/network/x/a.md")),
    ("file-link-path-net-double-slash", _link(path="//net/h/a.md")),
    ("file-link-path-net-private", _link(path="/private/net/h/a.md")),
    ("file-link-path-home-data-volume", _link(path="/System/Volumes/Data/home/u/a.md")),
    ("file-link-path-net-data-volume-private", _link(path="/System/Volumes/Data/private/net/h")),
    ("file-link-path-net-private-private", _link(path="/private/private/net/h")),
    ("file-link-path-net-var-automount", _link(path="/var/automount/net/h")),
    ("file-link-path-home", _link(path="/home/u/a.md")),
    ("file-link-path-valid-hidden-inner", _link(path="/Users/x/.config/a.md")),
    ("file-link-path-valid-homebrew", _link(path="/homebrew/a.md")),
    ("file-link-path-valid-networks-n1", _link(path="/networks/a.md")),
    ("file-link-path-dotdot", _link(path="~/../x")),
    ("file-link-path-dot", _link(path="/a/./b")),
    ("file-link-path-net", _link(path="/net/host/share/doc.md")),
    ("file-link-path-network", _link(path="/Network/Servers/x.md")),
    ("file-link-path-valid-dotdot-in-name", _link(path="/a/..b/c.md")),
    ("file-link-path-valid-networks", _link(path="/networks/x.md")),
    # R3: a link's `path`, instead of `url`
    ("file-link-path-valid", _link(path=NOTES)),
    ("file-link-path-valid-absolute", _link(path="/var/tmp/release-notes.md")),
    ("file-link-path-valid-url-null", _link(path=NOTES, url=None)),
    ("file-link-url-valid-path-null", _link(url="https://example.com/notes", path=None)),
    ("file-link-path-and-url", _link(path=NOTES, url="https://example.com/notes")),
    ("file-link-neither", _link()),
    ("file-link-both-null", _link(url=None, path=None)),
    ("file-link-path-and-bad-url", _link(path="release.md", url="ftp://example.com/x")),
    ("file-link-path-relative", _link(path="release.md")),
    ("file-link-path-empty", _link(path="")),
    ("file-link-path-not-string", _link(path=["/tmp/a.md"])),
    ("file-link-path-501", _link(path="/" + "a" * 497 + ".md")),
    ("file-link-path-multiline", _link(path="/tmp/release\nnotes.md")),
    ("file-link-path-hidden", _link(path="/tmp/release\u2028notes.md")),
    ("file-link-path-secret-like", _link(path="/tmp/ghp_release.md")),
    ("file-link-path-label-missing", _ask(links=[{"path": NOTES}])),
    ("file-link-path-second", _ask(links=[{"label": "Spec", "url": "https://example.com/spec"},
                                          {"label": "Release notes", "path": "release.md"}])),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    names = [name for name, _ in INPUTS]
    if not names or len(set(names)) != len(names):
        raise SystemExit("gen_optional_vectors: no cases, or a duplicate case name")
    cases = []
    for name, obj in INPUTS:
        assert name.startswith(PREFIXES), name
        clean, errors = validate(obj)
        cases.append({"name": name, "kind": "instructable", "input": obj, "valid": not errors,
                      "errors": errors, "cleaned": clean or None, "pyBytes": _py_bytes(obj)})
    return cases


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _ours(case: dict) -> bool:
    return str(case.get("name", "")).startswith(PREFIXES)


def write_hook(fixture: Path, cases: list[dict]) -> None:
    """Replace the optional-* and file-* cases in Hook's fixture with `cases`, at the
    place the first of them stood (or at the end); every other case stays as it was."""
    if not cases:
        raise SystemExit("gen_optional_vectors: no cases to write")
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    old = doc.get("cases")
    if not isinstance(old, list) or not old:
        raise SystemExit(f"gen_optional_vectors: {fixture} holds no cases")
    first = next((i for i, c in enumerate(old) if _ours(c)), len(old))
    kept_before = [c for c in old[:first] if not _ours(c)]
    kept_after = [c for c in old[first:] if not _ours(c)]
    doc["cases"] = kept_before + cases + kept_after
    fixture.write_text(_dump(doc), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", type=Path, help="Hook's instructable-vectors.json to update too")
    args = parser.parse_args()
    cases = build()
    OUT.write_text(_dump({"source": "mailbox/tests/gen_optional_vectors.py (optional-* and file-* cases)",
                          "cases": cases}), encoding="utf-8")
    if args.hook:
        write_hook(args.hook, cases)


if __name__ == "__main__":
    main()
