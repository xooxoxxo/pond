#!/usr/bin/env python3
"""Check every layer of a saved Pond image for files that must not ship.

    docker save IMAGE -o image.tar
    python3 scripts/check-image-files.py image.tar [--dockerfile mailbox/Dockerfile]

Reads each layer of the `docker save` archive (Docker or OCI layout), so a file added in one
layer and deleted in a later one still counts. Refuses (exit 1) when:
  - anything under /app is not one of the files the Dockerfile's COPY names (no tests,
    docs, README, compose file, env file or host script), or one of those is missing;
  - any layer holds a file named .env or .env.* anywhere.
"""
import argparse
import io
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

APP_DEST = "app/mailbox"


def copied_files(dockerfile_text: str) -> set[str]:
    """The source names of the COPY instruction(s) whose destination is /app/mailbox/."""
    joined = re.sub(r"\\\n", " ", dockerfile_text)
    names: set[str] = set()
    for line in joined.splitlines():
        words = line.split()
        if len(words) >= 3 and words[0].upper() == "COPY" and words[-1].rstrip("/") == "/" + APP_DEST:
            names.update(w for w in words[1:-1] if not w.startswith("--"))
    if not names:
        raise ValueError("Dockerfile: no COPY ... /app/mailbox/ instruction")
    return names


def _norm(name: str) -> str:
    """A layer path without a leading ./ or /: "./app/x" and "/app/x" are "app/x"."""
    while name.startswith("./"):
        name = name[2:]
    return str(PurePosixPath(name.lstrip("/") or "."))


def layer_listings(archive: Path) -> dict[str, list[str]]:
    """{layer member name: [paths in that layer]} for every member that is itself a tar."""
    layers: dict[str, list[str]] = {}
    with tarfile.open(archive, "r:*") as outer:
        for member in outer:
            if not member.isfile():
                continue
            data = outer.extractfile(member).read()
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as layer:
                    layers[member.name] = [_norm(m.name) for m in layer.getmembers()]
            except tarfile.TarError:
                continue  # a JSON manifest or config blob
    if not layers:
        raise ValueError(f"{archive}: no image layers found")
    return layers


def problems(layers: dict[str, list[str]], allowed: set[str]) -> list[str]:
    """Every shipped path that breaks the rules, and every runtime file that is missing."""
    found: list[str] = []
    seen: set[str] = set()
    allowed_paths = {f"{APP_DEST}/{name}" for name in allowed}
    for layer, paths in layers.items():
        for path in paths:
            base = PurePosixPath(path).name
            if base == ".env" or base.startswith(".env.") or base.startswith(".wh..env"):
                found.append(f"{layer}: env file {path}")
            if path == "app" or path.startswith("app/"):
                if path in ("app", APP_DEST):
                    continue
                if path in allowed_paths:
                    seen.add(path)
                else:
                    found.append(f"{layer}: not a runtime file {path}")
    for missing in sorted(allowed_paths - seen):
        found.append(f"missing runtime file {missing}")
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="output of docker save")
    parser.add_argument("--dockerfile", type=Path, default=Path("mailbox/Dockerfile"))
    args = parser.parse_args(argv)
    try:
        allowed = copied_files(args.dockerfile.read_text(encoding="utf-8"))
        layers = layer_listings(args.archive)
    except (OSError, ValueError, tarfile.TarError) as err:
        print(f"check-image-files: {err}", file=sys.stderr)
        return 1
    found = problems(layers, allowed)
    for problem in found:
        print(f"check-image-files: {problem}", file=sys.stderr)
    if found:
        return 1
    total = sum(len(p) for p in layers.values())
    print(f"check-image-files: ok: {len(layers)} layers, {total} paths, /app holds only the "
          f"{len(allowed)} runtime files the Dockerfile names")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
