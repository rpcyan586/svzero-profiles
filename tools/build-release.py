#!/usr/bin/env python3
"""Build three deterministic slicer downloads from a clean Git checkout.

Downloads contain ready-to-install profiles and optional consolidated macros.
Development sources and builders stay in Git. No network operations are performed.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_file(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts:
        raise ValueError("Unsafe release path: " + name)
    src = root / name
    if any(p.is_symlink() for p in (src, *src.parents) if p != root):
        raise ValueError("Release symlink refused: " + name)
    if not src.is_file():
        raise ValueError("Missing release input: " + name)
    return src.read_bytes()


def inventory(root):
    """Build only from a committed checkout, including Git worktrees."""
    if not (root / ".git").exists():
        raise ValueError("Build releases from a Git clone; slicer downloads contain installation files only")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root).strip():
        raise ValueError("Commit working-tree changes before packaging; dist/ is ignored")
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().strip("\0").split("\0")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    return {name: read_file(root, name) for name in names}, revision


def select_files(files, paths):
    """Map explicitly selected files/trees into the download's installation layout."""
    selected = {}
    for source, destination in paths.items():
        for name in (source, destination):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts:
                raise ValueError("Unsafe release path: " + name)
        if source.endswith("/") != destination.endswith("/"):
            raise ValueError("Map directories to directories and files to files: " + source)
        matches = [n for n in files if n.startswith(source)] if source.endswith("/") else [source]
        if not matches or any(n not in files for n in matches):
            raise ValueError("Missing release selection: " + source)
        for name in matches:
            target = destination + name[len(source):] if source.endswith("/") else destination
            if target in selected or target == "RELEASE.json":
                raise ValueError("Duplicate or reserved release destination: " + target)
            selected[target] = files[name]
    return selected


def products(files, manifest, requested=None):
    definitions = manifest["products"]
    kinds = requested or list(definitions)
    result = {}
    for kind in kinds:
        if kind not in definitions:
            raise ValueError("Unknown release kind: " + kind + "; source is available from Git")
        definition = definitions[kind]
        selected = select_files(files, manifest["common"])
        specific = select_files(files, definition["files"])
        overlap = selected.keys() & specific.keys()
        if overlap or "README.md" in selected or "README.md" in specific:
            raise ValueError("Duplicate release destination in " + kind)
        selected.update(specific)
        selected["README.md"] = files[definition["readme"]]
        result[kind] = selected
    return result


def write_archive(path, files, version, kind, revision):
    record = {"format": 1, "version": version, "kind": kind, "revision": revision,
              "files": {n: sha(data) for n, data in sorted(files.items())}}
    payload = dict(files)
    payload["RELEASE.json"] = (json.dumps(record, indent=2) + "\n").encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo("svzero-profiles-" + version + "/" + name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)


def build(root=ROOT, output=None, requested=None):
    root = Path(root).resolve()
    files, revision = inventory(root)
    version = files["VERSION"].decode().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", version):
        raise ValueError("Invalid release version")
    chosen = products(files, json.loads(files["tools/release-files.json"]), requested)
    output = Path(output) if output is not None else root / "dist"
    if output.is_symlink() or (output.exists() and
                              (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Release output must be empty; use --output with a new directory")
    output.mkdir(parents=True, exist_ok=True)
    sums = []
    for kind, members in chosen.items():
        name = "svzero-profiles-" + version + "-" + kind + ".zip"
        path = output / name
        write_archive(path, members, version, kind, revision)
        sums.append(sha(path.read_bytes()) + "  " + name)
        print(name, len(members) + 1, "files")
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", action="append", help="Build only this product (repeatable); default: all")
    parser.add_argument("--output", type=Path, help="Destination directory; default: dist/")
    args = parser.parse_args()
    try:
        build(output=args.output, requested=args.kind)
    except (ValueError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
