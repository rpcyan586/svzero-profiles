#!/usr/bin/env python3
"""Unit tests for sandbox-only slicer preset transformations."""

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


PATH = pathlib.Path(__file__).parents[1] / "tools/install-presets.py"
SPEC = importlib.util.spec_from_file_location("install_presets", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# The start G-code constants are owned by start_gcode.py, not by either tool.
sys.path.insert(0, str(PATH.parent))
import start_gcode  # noqa: E402


def render_orca_block(block, values):
    """Render one chamber block the way OrcaSlicer's placeholder parser does.

    A model of exactly the grammar these two blocks use -- `{if COND}`,
    `{else}`, `{endif}` and `{key[0]}` substitution -- not a general
    implementation of Orca's expression language.

    It exists because asserting on the block's TEXT is what let the Minimal=0
    defect ship: every string the old tests looked for was present, and the
    branch that actually fired was the wrong one. Checked against OrcaSlicer
    2.4.2 on 2026-09-03 by slicing all four cases through the real CLI; the
    emitted chamber lines matched this renderer exactly.
    """
    def truth(expression):
        expression = expression.strip()
        if ">" in expression:
            left, right = expression.split(">", 1)
            return lookup(left.strip()) > float(right.strip())
        return lookup(expression) != 0.0

    def lookup(reference):
        name, _, index = reference.partition("[")
        return float(values[name][int(index.rstrip("]"))])

    out = []
    stack = []
    position = 0
    while position < len(block):
        start = block.find("{", position)
        if start < 0:
            if all(stack):
                out.append(block[position:])
            break
        if all(stack):
            out.append(block[position:start])
        end = block.index("}", start)
        token = block[start + 1:end]
        position = end + 1
        if token.startswith("if "):
            stack.append(truth(token[3:]) if all(stack) else False)
        elif token == "else":
            # An else inside a branch whose parent is off stays off.
            stack[-1] = (not stack[-1]) if all(stack[:-1]) else False
        elif token == "endif":
            stack.pop()
        elif all(stack):
            out.append("%g" % lookup(token))
    return [line for line in "".join(out).split("\n")
            if line.strip() and not line.startswith(";")]


class InstallPresetsTest(unittest.TestCase):
    def test_generated_orca_zero_profiles_are_single_filament(self):
        machine_dir = PATH.parents[1] / "bundles/orca-vendor/SVZero/machine"
        paths = [machine_dir / "fdm_machine_common.json"]
        nozzles = json.loads((PATH.parents[1] / "source/model.json").read_text())["machine"]["nozzles"]
        paths.extend(machine_dir / f"SV Zero {nozzle} nozzle.json" for nozzle in nozzles)
        for path in paths:
            with self.subTest(path=path.name):
                profile = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(profile["single_extruder_multi_material"], "0")

    def test_generated_pla_profiles_use_20_minimum_and_33_ceiling(self):
        directory = PATH.parents[1] / "bundles/orca-vendor/SVZero/filament"
        paths = sorted(directory.glob("SV Zero PLA*.json"))
        self.assertEqual({p.stem for p in paths}, {
            "SV Zero PLA - Brass", "SV Zero PLA - Steel",
            "SV Zero PLA Silk - Brass", "SV Zero PLA Silk - Steel"})
        for path in paths:
            with self.subTest(path=path.name):
                profile = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(profile["activate_chamber_temp_control"], ["1"])
                self.assertEqual(profile["chamber_temperature"], ["33"])
                self.assertEqual(profile["chamber_minimal_temperature"], ["20"])
                self.assertNotIn("chamber_temperatures", profile)

    def test_neither_tool_keeps_its_own_copy_of_the_start_block(self):
        # generate.py emitted the block and install-presets.py searched for it,
        # each from its own literal. Adding _PREFLIGHT to one silently broke the
        # other. start_gcode.py owns the text now; assert the copies are gone
        # rather than trusting that nobody pastes one back.
        marker = "M140 S[bed_temperature_initial_layer_single]"
        for name in ("generate.py", "install-presets.py"):
            with self.subTest(tool=name):
                source = (PATH.parent / name).read_text(encoding="utf-8")
                self.assertNotIn(marker, source)
                self.assertIn("from start_gcode import", source)

    def test_shared_preamble_is_assembled_from_the_searched_block(self):
        # The text install-presets.py looks for must be, by construction, the
        # text generate.py emits.
        self.assertIn(start_gcode.ORCA_CHAMBER_BLOCK,
                      start_gcode.ORCA_START_PREAMBLE)

    def test_normal_chamber_start_matches_the_generated_bundle(self):
        # The shadow patcher locates its block by this exact text, but the text
        # is authored in generate.py. Nothing tied the two together, so adding
        # _PREFLIGHT to the preamble silently broke `sandbox.sh orca-shadow`
        # with "normal Orca chamber start block not found". Assert against a
        # real generated preset so the next drift fails here instead.
        machine_dir = PATH.parents[1] / "bundles/orca-vendor/SVZero/machine"
        paths = sorted(machine_dir.glob("SV Zero *.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                start = json.loads(path.read_text(encoding="utf-8"))["machine_start_gcode"]
                if isinstance(start, list):
                    start = start[0]
                self.assertIn(MODULE.ORCA_NORMAL_CHAMBER_START, start)

    def test_both_orca_start_blocks_gate_before_any_heater(self):
        for name, block in (
            ("normal", MODULE.ORCA_NORMAL_CHAMBER_START),
            ("shadow", MODULE.ORCA_SHADOW_CHAMBER_START),
        ):
            with self.subTest(block=name):
                lines = [line for line in block.split("\n")
                         if line and not line.startswith(";")]
                self.assertEqual(lines[0], "SVZERO_REQUIRE VERSION=" + start_gcode.SVZERO_PACK_VERSION)
                self.assertEqual(lines[1], "_PREFLIGHT")
                self.assertTrue(lines[2].startswith("M140 "))

    def test_normal_orca_start_honors_activation(self):
        start = MODULE.ORCA_NORMAL_CHAMBER_START
        self.assertIn("{if activate_chamber_temp_control[0]}", start)
        self.assertIn("M191 S{chamber_minimal_temperature[0]}", start)
        self.assertIn("M141 S{chamber_temperature[0]}", start)
        self.assertTrue(start.endswith("{else}M191{endif}"))

    def test_zero_minimal_never_substitutes_the_target_as_a_minimum(self):
        # The defect this test exists for: with Minimal = 0 the block fell
        # through to `M191 S{chamber_temperature[0]}` and waited for the
        # TARGET. An ABS job sliced Minimal=0 Target=60 held the print open
        # 13 minutes with the bed boosted to 117 C on 2026-09-03. Zero means
        # do not wait, so the target must never reach an M191 S at all.
        for name, block in (
            ("normal", MODULE.ORCA_NORMAL_CHAMBER_START),
            ("shadow", MODULE.ORCA_SHADOW_CHAMBER_START),
        ):
            with self.subTest(block=name):
                for line in block.split("\n"):
                    if "chamber_temperature[0]" in line and "M191" in line:
                        self.fail("target reaches an M191 in %s: %r"
                                  % (name, line))

    def test_both_orca_blocks_render_the_four_chamber_cases(self):
        # Structure alone proved nothing here before: the old block contained
        # every string this file asserted and still emitted the wrong branch.
        # Render each case instead and name the exact lines expected.
        cases = {
            # activation, target, minimal -> the chamber commands emitted
            ("1", "60", "45"): ["M191 S45", "M141 S60"],
            # Minimal 0: no wait, target still controlled.
            ("1", "60", "0"): ["M141 S60"],
            # Target 0: no chamber control at all, stated rather than omitted
            # so a stale ch_target cannot survive from the previous job.
            ("1", "0", "0"): ["M141 S0"],
            # Unchecked: bare M191, which is minimum 22 C / target 32 C in the
            # macro. A barebones profile still lands somewhere safe.
            ("0", "60", "45"): ["M191"],
        }
        for name, block, suffix in (
            ("normal", MODULE.ORCA_NORMAL_CHAMBER_START, ""),
            ("shadow", MODULE.ORCA_SHADOW_CHAMBER_START,
             " Z15 STIR=1 " + start_gcode.ORCA_SHADOW_BOOST),
        ):
            for (active, target, minimal), expected in cases.items():
                if suffix:
                    expected = [line + suffix if line.startswith("M191")
                                else line for line in expected]
                with self.subTest(block=name, activate=active,
                                  target=target, minimal=minimal):
                    rendered = render_orca_block(block, {
                        "activate_chamber_temp_control": [active],
                        "chamber_temperature": [target],
                        "chamber_minimal_temperature": [minimal],
                    })
                    self.assertEqual(
                        [line for line in rendered
                         if line.startswith(("M191", "M141"))],
                        expected,
                    )

    def test_orca_shadow_mode_moves_bed_before_anchor_and_nozzle_after(self):
        with tempfile.TemporaryDirectory() as directory:
            machine_dir = pathlib.Path(directory) / "system/SVZero/machine"
            machine_dir.mkdir(parents=True)
            start = "header\n" + MODULE.ORCA_NORMAL_CHAMBER_START + "\nM104 S170\ntail"
            for nozzle in ("0.4", "0.6"):
                (machine_dir / f"SV Zero {nozzle}.json").write_text(
                    json.dumps({"machine_start_gcode": start}), encoding="utf-8")

            self.assertEqual(MODULE.enable_orca_chamber_shadow_test(directory), 2)
            result = json.loads(
                (machine_dir / "SV Zero 0.4.json").read_text(encoding="utf-8")
            )["machine_start_gcode"]
            self.assertLess(result.index("M140 S"), result.index("M191 S{"))
            self.assertLess(result.index("M191 S{"), result.index("M104 S170"))
            self.assertIn("chamber_minimal_temperature[0] > 0", result)
            self.assertIn(
                "M191 S{chamber_minimal_temperature[0]} Z15 STIR=1 "
                + start_gcode.ORCA_SHADOW_BOOST,
                result,
            )
            self.assertIn("{if activate_chamber_temp_control[0]}", result)
            self.assertIn("{else}M141 S0{endif}", result)
            self.assertIn("M141 S{chamber_temperature[0]}", result)
            self.assertIn(
                "{else}M191 Z15 STIR=1 "
                + start_gcode.ORCA_SHADOW_BOOST
                + "{endif}",
                result,
            )
            self.assertNotIn("X145", result)
            self.assertIn("sandbox-only, not publication policy", result)

    def test_personal_skirts_are_disabled_only_in_installed_presets(self):
        public = PATH.parents[1] / "bundles/orca-vendor/SVZero/process/0.20mm Standard @SV Zero 0.4 nozzle.json"
        original = public.read_bytes()
        self.assertEqual(json.loads(original)["skirt_loops"], "1")
        with tempfile.TemporaryDirectory() as directory:
            dest = pathlib.Path(directory) / "system/SVZero/process"
            dest.mkdir(parents=True)
            (dest / public.name).write_bytes(original)
            (dest / "unrelated.json").write_text('{"name": "unrelated", "skirt_loops": "2"}')
            self.assertEqual(MODULE.disable_personal_skirts(directory, "orca"), 1)
            installed = json.loads((dest / public.name).read_text())
            self.assertEqual((installed["skirt_loops"], installed["min_skirt_length"]), ("0", "0"))
            self.assertEqual(json.loads((dest / "unrelated.json").read_text())["skirt_loops"], "2")
        self.assertEqual(public.read_bytes(), original)

    def test_personal_ini_skirts_are_disabled(self):
        for slicer in ("prusa", "superslicer"):
            with self.subTest(slicer=slicer), tempfile.TemporaryDirectory() as directory:
                dest = pathlib.Path(directory) / "print"
                dest.mkdir()
                p = dest / "SV Zero test.ini"
                p.write_text("skirts = 1\nmin_skirt_length = 8\nperimeters = 3\n")
                self.assertEqual(MODULE.disable_personal_skirts(directory, slicer), 1)
                self.assertEqual(p.read_text(), "skirts = 0\nmin_skirt_length = 0\nperimeters = 3\n")

if __name__ == "__main__":
    unittest.main()
