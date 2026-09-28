"""Installation downloads must be complete, small and reproducible from Git."""
import configparser
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "tools/build-release.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def source_files():
    ignored = {".git", ".venv", "dist", ".cache", "__pycache__"}
    return {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in ROOT.rglob("*")
            if p.is_file() and not (set(p.relative_to(ROOT).parts) & ignored)
            and p.suffix not in (".pyc", ".log")
            and p.name not in ("RELEASE.json", "OrcaSlicer_profile_validator")}


class ReleaseBundles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = source_files()
        cls.manifest = json.loads(cls.files["tools/release-files.json"])
        cls.products = B.products(cls.files, cls.manifest)
        cls.version = cls.files["VERSION"].decode().strip()

    def test_only_three_slicer_downloads_without_development_inputs(self):
        self.assertEqual(set(self.products), {"orcaslicer", "prusaslicer", "superslicer"})
        with self.assertRaisesRegex(ValueError, "source is available from Git"):
            B.products(self.files, self.manifest, ["source"])
        for kind, files in self.products.items():
            with self.subTest(kind=kind):
                self.assertFalse(any(n.startswith(("tools/", "source/", "tests/", "klipper/",
                                                   "bundles/", ".github/")) for n in files))
                self.assertFalse(any("personal" in n for n in files))
                self.assertEqual({n for n in files if n.endswith(".cfg")},
                                 {"optional-macros/" + n for n in
                                  ("svzero-1.3.7.cfg", "svzero-1.4.x.cfg", "svzero-python.cfg")})
                self.assertEqual({n for n in files if n.endswith(".py")},
                                 {"optional-macros/" + n for n in
                                  ("chamber_preheat.py", "spool_guard.py", "moonraker.py")})
                self.assertIn("LICENSE", files)
                self.assertIn("NOTICE", files)
                self.assertIn("LICENSES/AGPL-3.0.txt", files)
                for name in ("svzero-1.3.7.cfg", "svzero-1.4.x.cfg", "svzero-python.cfg"):
                    content = files["optional-macros/" + name]
                    self.assertEqual(content, self.files["bundles/klipper/" + name])
                    self.assertNotRegex(content.decode(), r"(?m)^\s*\[include ")
                for name in ("chamber_preheat.py", "spool_guard.py", "moonraker.py"):
                    self.assertEqual(files["optional-macros/" + name], self.files["klipper/" + name])

    def test_extracted_downloads_support_documented_copy_or_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind, files in self.products.items():
                with self.subTest(kind=kind):
                    archive = root / (kind + ".zip")
                    B.write_archive(archive, files, self.version, kind, "abc123")
                    with zipfile.ZipFile(archive) as z:
                        z.extractall(root / kind)
                    work = root / kind / ("svzero-profiles-" + self.version)
                    record = json.loads((work / "RELEASE.json").read_text())
                    self.assertEqual(record["kind"], kind)
                    self.assertEqual(record["version"], self.version)
                    for name, digest in record["files"].items():
                        self.assertEqual(B.sha((work / name).read_bytes()), digest)
                    for doc in work.rglob("*.md"):
                        for link in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                            link = link.strip("<>").split("#", 1)[0]
                            if not link or re.match(r"\w+://", link) or link.startswith("mailto:"):
                                continue
                            self.assertTrue((doc.parent / unquote(link)).exists(), (kind, doc.name, link))
                    if kind == "orcaslicer":
                        installed = root / "orca-data/system"
                        shutil.copytree(work / "profiles", installed)
                        expected = {n.removeprefix("bundles/orca-vendor/"): data for n, data in self.files.items()
                                    if n.startswith("bundles/orca-vendor/")}
                        actual = {p.relative_to(installed).as_posix(): p.read_bytes()
                                  for p in installed.rglob("*") if p.is_file()}
                        self.assertEqual(actual, expected)
                        self.assertNotIn("assets/svzero_bed.stl", files)
                        self.assertIn("SVZero/svzero_bed.CREDITS.md", actual)
                    else:
                        slicer = "PrusaSlicer" if kind == "prusaslicer" else "SuperSlicer"
                        filename = "SVZero_" + slicer + ".ini"
                        self.assertEqual({n for n in files if n.endswith(".ini")}, {filename})
                        self.assertEqual((work / filename).read_bytes(), self.files["bundles/" + filename])
                        config = configparser.ConfigParser(interpolation=None, strict=False)
                        config.read(work / filename)
                        for category in ("printer:", "print:", "filament:"):
                            self.assertTrue(any(n.startswith(category) for n in config.sections()), category)
                        for asset in ("svzero_bed.stl", "svzero_bed.svg", "svzero_bed.CREDITS.md"):
                            self.assertEqual((work / "assets" / asset).read_bytes(), self.files["assets/" + asset])
                    with self.assertRaisesRegex(ValueError, "Git clone"):
                        B.inventory(work)

    def test_clean_checkout_builds_only_three_reproducible_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "clone"
            work.mkdir()
            for name, data in self.files.items():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=work, stderr=subprocess.STDOUT)
            git("init", "-q")
            git("add", "--", *self.files)
            git("-c", "user.name=Release test", "-c", "user.email=release@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "Test release inputs")
            revision = git("rev-parse", "HEAD").decode().strip()
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            with contextlib.redirect_stdout(io.StringIO()):
                B.build(work, first)
                B.build(work, second)
            self.assertEqual({p.name for p in first.iterdir()},
                             {"SHA256SUMS", *("svzero-profiles-" + self.version + "-" + kind + ".zip"
                                              for kind in self.products)})
            for item in first.iterdir():
                self.assertEqual(item.read_bytes(), (second / item.name).read_bytes())
                if item.suffix == ".zip":
                    with zipfile.ZipFile(item) as z:
                        record = json.loads(z.read("svzero-profiles-" + self.version + "/RELEASE.json"))
                        self.assertEqual(record["revision"], revision)
            for line in (first / "SHA256SUMS").read_text().splitlines():
                digest, filename = line.split()
                self.assertEqual(digest, B.sha((first / filename).read_bytes()))

    def test_existing_output_is_preserved_including_old_source_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            old = output / "svzero-profiles-old-source.zip"
            old.write_bytes(b"previously published")
            with patch.object(B, "inventory", return_value=(self.files, "abc123")):
                with self.assertRaisesRegex(ValueError, "output must be empty"):
                    B.build(ROOT, output)
            self.assertEqual(list(output.iterdir()), [old])
            self.assertEqual(old.read_bytes(), b"previously published")

    def test_missing_or_conflicting_destinations_fail(self):
        for selection in ({"missing/": "dest/"}, {"missing.json": "file.json"},
                          {"VERSION": "same", "README.md": "same"},
                          {"VERSION": "RELEASE.json"}, {"VERSION": "directory/"}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                B.select_files(self.files, selection)

    def test_dirty_checkout_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "unreviewed.txt").write_text("uncommitted")
            with self.assertRaisesRegex(ValueError, "Commit working-tree changes"):
                B.inventory(root)

    def test_unsafe_inputs_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").write_text("data")
            (root / "link").symlink_to("real")
            for name in ("../outside", "/etc/passwd", "..\\outside", "link"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    B.read_file(root, name)
            for name in ("../outside", "/etc/passwd", "..\\outside", ""):
                with self.subTest(destination=name), self.assertRaises(ValueError):
                    B.select_files(self.files, {"VERSION": name})


if __name__ == "__main__":
    unittest.main()
