#!/usr/bin/env python3
"""The profile/pack handshake is one line, and every slicer emits the same one.

SVZERO_REQUIRE carries the pack VERSION and the machine MODE a profile was
sliced for. A version mismatch only warns, but a mode mismatch refuses the job
(klipper/config/svzero_pack.cfg), so a profile that emitted a stale or missing
MODE would be refused on the very printer it was built for.

The Orca start G-code is built from start_gcode.py, while PrusaSlicer and
SuperSlicer carry the line as text inside source/presets.json. tools/verify.py
only checks that the version string appears somewhere in each source file.
These tests check the emitted line itself, in every generated bundle.
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import start_gcode  # noqa: E402

PACK = ROOT / "klipper/config/svzero_pack.cfg"
COMMAND = re.compile(r"^\s*SVZERO_REQUIRE\b.*$", re.M)


def pack_variable(name):
    match = re.search(r'^variable_%s:\s*"([^"]*)"\s*$' % name,
                      PACK.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise AssertionError("svzero_pack.cfg has no variable_%s" % name)
    return match.group(1)


def emitted_commands():
    """Every SVZERO_REQUIRE command line in the generated slicer bundles.

    INI, JSON and YAML all store the start G-code with escaped newlines, so
    unescape before splitting into lines. The klipper bundles define the macro
    rather than call it and are left out.
    """
    found = {}
    for path in sorted((ROOT / "bundles").rglob("*")):
        if not path.is_file() or "klipper" in path.relative_to(ROOT / "bundles").parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").replace("\\n", "\n")
        lines = [m.group(0).strip().rstrip('",') for m in COMMAND.finditer(text)]
        if lines:
            found[path.relative_to(ROOT)] = lines
    return found


class HandshakeTest(unittest.TestCase):
    def test_line_is_built_from_the_constants(self):
        self.assertEqual(start_gcode.SVZERO_REQUIRE_LINE,
                         "SVZERO_REQUIRE VERSION=%s MODE=%s"
                         % (start_gcode.SVZERO_PACK_VERSION, start_gcode.SVZERO_MODE))

    def test_pack_matches_profiles(self):
        self.assertEqual(pack_variable("version"), start_gcode.SVZERO_PACK_VERSION)
        self.assertEqual(pack_variable("mode"), start_gcode.SVZERO_MODE)

    def test_every_slicer_emits_the_same_line(self):
        found = emitted_commands()
        # Not vacuous: each slicer family has to be present.
        families = {"SVZero_PrusaSlicer.ini", "SVZero_SuperSlicer.ini", "ps-presets",
                    "ss-presets", "ps-vendor", "orca-vendor", "ps3"}
        seen = {part for path in found for part in path.parts}
        self.assertEqual(families - seen, set())
        for path, lines in found.items():
            with self.subTest(path=str(path)):
                self.assertEqual(set(lines), {start_gcode.SVZERO_REQUIRE_LINE})

    def test_source_presets_carry_the_line(self):
        text = (ROOT / "source/presets.json").read_text(encoding="utf-8").replace("\\\\n", "\n")
        lines = [m.group(0).strip() for m in COMMAND.finditer(text)]
        self.assertTrue(lines)
        self.assertEqual(set(lines), {start_gcode.SVZERO_REQUIRE_LINE})


if __name__ == "__main__":
    unittest.main()
