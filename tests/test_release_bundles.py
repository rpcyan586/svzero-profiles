"""Downloads must stand alone, exclude other slicers and reproduce exactly."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
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
        cls.products = B.products(cls.files, json.loads(cls.files["release-files.json"]))
        cls.version = cls.files["VERSION"].decode().strip()

    def test_each_slicer_contains_only_its_output_and_complete_macros(self):
        allowed = {"orcaslicer": ("bundles/orca-vendor/",),
                   "prusaslicer": ("bundles/ps-presets/", "bundles/SVZero_PrusaSlicer.ini"),
                   "superslicer": ("bundles/ss-presets/", "bundles/SVZero_SuperSlicer.ini")}
        manifest = json.loads(self.files["bundles/klipper/manifest.json"])
        for kind, prefixes in allowed.items():
            files = self.products[kind]
            with self.subTest(kind=kind):
                self.assertTrue(any(n.startswith(prefixes) for n in files))
                for name in files:
                    if name.startswith("bundles/"):
                        self.assertTrue(name.startswith((*prefixes, "bundles/klipper/")), name)
                self.assertNotIn("tools/generate.py", files)
                for name in ("tools/build-macros.py", "tools/build-release.py", "klipper/moonraker.py"):
                    self.assertIn(name, files)
                for target in manifest["targets"].values():
                    for name in target["sources"]:
                        self.assertIn(name, files)
                for name in files:
                    self.assertNotIn("personal", name)

    def test_extracted_downloads_install_and_reproduce_without_git(self):
        installers = {"orcaslicer": "orca", "prusaslicer": "prusa", "superslicer": "superslicer"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind, files in self.products.items():
                with self.subTest(kind=kind):
                    original = root / (kind + ".zip")
                    B.write_archive(original, files, self.version, kind, "abc123")
                    target = root / kind
                    with zipfile.ZipFile(original) as archive:
                        archive.extractall(target)
                    work = target / ("svzero-profiles-" + self.version)
                    self.assertFalse((work / ".git").exists())
                    for doc in work.rglob("*.md"):
                        for link in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                            link = link.strip("<>").split("#", 1)[0]
                            if not link or re.match(r"\w+://", link) or link.startswith("mailto:"):
                                continue
                            self.assertTrue((doc.parent / unquote(link)).exists(), (kind, doc.name, link))
                    if kind in installers:
                        subprocess.run([sys.executable, "tools/install-presets.py", installers[kind],
                                        str(root / (kind + "-installed"))], cwd=work,
                                       check=True, capture_output=True)
                        subprocess.run([sys.executable, "tools/build-macros.py", "--check"],
                                       cwd=work, check=True, capture_output=True)
                    with contextlib.redirect_stdout(io.StringIO()):
                        B.build(work, requested=[kind])
                    rebuilt = work / "dist" / ("svzero-profiles-" + self.version + "-" + kind + ".zip")
                    self.assertEqual(original.read_bytes(), rebuilt.read_bytes())
                    if kind != "source":
                        (work / "VERSION").write_text("changed")
                        with self.assertRaises(ValueError):
                            B.build(work)

    def test_missing_inputs_are_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            B.select_files(self.files, ["missing/"])
        with self.assertRaises(ValueError):
            B.select_files(self.files, ["missing.json"])

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


if __name__ == "__main__":
    unittest.main()
