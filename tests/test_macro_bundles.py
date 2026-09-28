"""Consolidation must preserve Klipper's ordered configuration and opt-ins."""
import configparser
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("macro_builder", ROOT / "tools/build-macros.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def load_config(path, cp=None):
    """Read separate buffers around includes, matching Klipper's load order."""
    if cp is None:
        cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
    buffer = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0]
        match = cp.SECTCRE.match(line)
        if match and match.group("header").startswith("include "):
            cp.read_string("\n".join(buffer))
            buffer.clear()
            load_config(path.parent / match.group("header")[8:].strip(), cp)
        else:
            buffer.append(line)
    cp.read_string("\n".join(buffer))
    return cp


def settings(cp):
    return {section: dict(cp.items(section)) for section in cp.sections()}


class MacroBundles(unittest.TestCase):
    def test_generated_configs_match_ordered_sources(self):
        outputs = B.render(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            for name in B.TARGETS:
                with self.subTest(name=name):
                    path = Path(tmp) / name
                    path.write_bytes(outputs[name])
                    source = load_config(ROOT / "klipper/config" / name)
                    combined = load_config(path)  # temp dir has no source includes
                    self.assertEqual(settings(combined), settings(source))
                    if name == "svzero-python.cfg":
                        self.assertEqual(set(combined.sections()), {"chamber_preheat", "spool_guard"})
                        continue
                    self.assertNotIn("chamber_preheat", combined)
                    self.assertNotIn("spool_guard", combined)
                    old = name == "svzero-1.3.7.cfg"
                    self.assertEqual(combined.has_section("probe_pressure"), old)
                    brush = combined["gcode_macro _BRUSH"]
                    self.assertEqual(brush["variable_probe"],
                                     '"RUN_PROBE_PRESSURE"' if old else '"RUN_PROBE_VIR_CONTACT"')
                    if not old:
                        self.assertEqual(float(brush["variable_tap_x"]), 30)
                        self.assertEqual(float(brush["variable_tap_y"]), 30)

    def test_manifest_is_complete_and_rebuild_is_current(self):
        outputs = B.build(ROOT, check=True)
        manifest = json.loads(outputs["manifest.json"])
        for name, info in manifest["targets"].items():
            self.assertEqual(info["sha256"], B.digest(outputs[name]))
            self.assertIn("klipper/config/" + name, info["sources"])
            self.assertFalse(any(Path(n).name == "svzero-personal.cfg" for n in info["sources"]))
            for source, checksum in info["sources"].items():
                self.assertEqual(checksum, B.digest((ROOT / source).read_bytes()))

    def test_includes_are_ordered_and_commented_opt_ins_stay_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.cfg").write_text(
                "[gcode_macro STATE]\nvariable_x: 1\ngcode:\n"
                "[include child.cfg] # expanded here\n"
                "[gcode_macro STATE]\nvariable_x: 3\n#[include absent.cfg]\n")
            (root / "child.cfg").write_text("[gcode_macro STATE]\nvariable_x: 2\n")
            inputs = {}
            text = B.expand(root, Path("main.cfg"), inputs)
            self.assertEqual(set(inputs), {"main.cfg", "child.cfg"})
            (root / "combined.cfg").write_text(text)
            self.assertEqual(settings(load_config(root / "main.cfg")),
                             settings(load_config(root / "combined.cfg")))
            self.assertEqual(load_config(root / "combined.cfg")["gcode_macro STATE"]["variable_x"], "3")

    def test_unsafe_or_unresolved_includes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for include in ("absent.cfg", "*.cfg", "../outside.cfg", "/tmp/outside.cfg", "main.cfg"):
                with self.subTest(include=include):
                    (root / "main.cfg").write_text("[include " + include + "]\n")
                    with self.assertRaises(ValueError):
                        B.expand(root, Path("main.cfg"), {})
            (root / "link.cfg").symlink_to("main.cfg")
            with self.assertRaises(ValueError):
                B.expand(root, Path("link.cfg"), {})

    def test_build_refuses_source_overwrite_and_detects_stale_output(self):
        with self.assertRaises(ValueError):
            B.build(ROOT, ROOT / "klipper/config")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            original = B.build(ROOT, out)
            self.assertEqual(original, B.build(ROOT, out, check=True))
            (out / B.TARGETS[0]).write_text("changed\n")
            with self.assertRaises(ValueError):
                B.build(ROOT, out, check=True)
            self.assertEqual(original, B.build(ROOT, out))


if __name__ == "__main__":
    unittest.main()
