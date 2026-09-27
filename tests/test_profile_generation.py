#!/usr/bin/env python3
"""Regression checks for incomplete nozzle additions and generated geometry."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


class ProfileGenerationTests(unittest.TestCase):
    def test_speed_chain_rounds_only_after_computation(self):
        source = {"default_speed": "31.1", "perimeter_speed": "68%",
                  "external_perimeter_speed": "83%", "travel_speed": "1000",
                  "first_layer_speed": "10.5%"}
        resolved = G.resolve_speeds(source)
        self.assertEqual(resolved["perimeter_speed"], "21.1")
        # 31.1 * .68 * .83 = 17.55284; rounding the middle value gives 17.5.
        self.assertEqual(resolved["external_perimeter_speed"], "17.6")
        self.assertEqual(resolved["travel_speed"], "1000")
        self.assertEqual(source["perimeter_speed"], "68%")
        ps = dict(source, default_acceleration="26301", perimeter_acceleration="83%")
        G.ps_absolutise_speeds(ps)
        self.assertEqual(ps["external_perimeter_speed"], "17.6")
        self.assertEqual(ps["perimeter_acceleration"], "21829.8")
        self.assertEqual(ps["first_layer_speed"], "10.5%")
        self.assertEqual(G.resolve_speeds({"default_speed": "500", "perimeter_speed": "68%"})
                         ["perimeter_speed"], "340.0")

    def test_orca_first_layer_uses_unrounded_feature_speed(self):
        model = G.load("model.json")
        sections = G.sections_from_tree(G.load("presets.json"), model)
        name = "print:SV Zero 0.4n - 0.20mm (Standard)"
        sections[name].update(default_speed="31.1", perimeter_speed="68%",
                              first_layer_speed="75%", first_layer_infill_speed="0")
        with tempfile.TemporaryDirectory() as tmp:
            G.emit_orca(model, sections, G.load("keymap.json"), tmp)
            preset = json.loads((Path(tmp) / "0.20mm Standard @SV Zero 0.4 nozzle.json").read_text())
        self.assertEqual(preset["inner_wall_speed"], "21.1")
        # 21.148 * .75 = 15.861, rather than 21.1 * .75 = 15.825.
        self.assertEqual(preset["initial_layer_speed"], "15.9")

    def test_motion_rounding_preserves_geometry_and_calibration(self):
        preset = {"first_layer_height": "62.5%", "perimeter_extrusion_width": "112.5%",
                  "bridge_flow_ratio": "95%", "extrusion_multiplier": "0.98",
                  "filament_pressure_advance": "0.036", "filament_diameter": "1.75",
                  "resolution": "0.012", "min_layer_height": "20%", "max_layer_height": "80%"}
        G.ps_percent_to_mm(preset, "SV Zero 0.5n")
        self.assertEqual(preset, {"first_layer_height": "0.3125", "perimeter_extrusion_width": "0.5625",
                                 "bridge_flow_ratio": "0.95", "extrusion_multiplier": "0.98",
                                 "filament_pressure_advance": "0.036", "filament_diameter": "1.75",
                                 "resolution": "0.012", "min_layer_height": "0.1", "max_layer_height": "0.4"})

    def test_emitted_motion_precision_and_native_percentages(self):
        vendor = ROOT / "bundles/orca-vendor/SVZero/process"
        derived = ("inner_wall_speed", "outer_wall_speed", "sparse_infill_speed",
                   "internal_solid_infill_speed", "top_surface_speed", "small_perimeter_speed",
                   "gap_infill_speed", "initial_layer_speed", "initial_layer_infill_speed",
                   "bridge_speed", "outer_wall_acceleration", "top_surface_acceleration")
        presets = [json.loads(p.read_text()) for p in vendor.glob("* @SV Zero *.json")]
        self.assertEqual(len(presets), len(G.expected_presets(G.load("model.json"))))
        for preset in presets:
            for key in derived:
                with self.subTest(preset=preset["name"], key=key):
                    self.assertRegex(preset[key], r"^\d+\.\d$")
        ss = G.parse_ini(ROOT / "bundles/SVZero_SuperSlicer.ini")
        ps = G.parse_ini(ROOT / "bundles/SVZero_PrusaSlicer.ini")
        for name in ss:
            if not name.startswith("print:SV Zero "):
                continue
            native = G.flatten(ss, name)
            self.assertEqual(native["perimeter_speed"], "68%")
            self.assertEqual(native["external_perimeter_speed"], "83%")
            self.assertEqual(native["first_layer_speed"], "10.5%")
            self.assertRegex(native["default_speed"], r"^\d+\.\d$")
            converted = G.flatten(ps, name)
            self.assertRegex(converted["perimeter_speed"], r"^\d+\.\d$")
            self.assertRegex(converted["external_perimeter_speed"], r"^\d+\.\d$")
            self.assertEqual(converted["first_layer_speed"], "10.5%")

    def test_travel_acceleration_is_5000_in_every_shipped_process(self):
        expected = len(G.expected_presets(G.load("model.json")))
        bundles = ROOT / "bundles"
        for relative in ("SVZero_SuperSlicer.ini", "SVZero_PrusaSlicer.ini", "ps-vendor/SVZero.ini"):
            sections = G.parse_ini(bundles / relative)
            names = [n for n in sections if n.startswith("print:SV Zero ")]
            self.assertEqual(len(names), expected)
            for name in names:
                with self.subTest(bundle=relative, preset=name):
                    self.assertEqual(float(G.flatten(sections, name)["travel_acceleration"]), 5000)
                    if relative == "SVZero_SuperSlicer.ini":
                        self.assertEqual(G.flatten(sections, name).get("travel_deceleration_use_target", "1"), "1")
        for relative in ("ps-presets/print", "ss-presets/print"):
            files = list((bundles / relative).glob("*.ini"))
            self.assertEqual(len(files), expected)
            for path in files:
                values = dict(line.split(" = ", 1) for line in path.read_text().splitlines()
                              if " = " in line and not line.startswith("#"))
                self.assertEqual(float(values["travel_acceleration"]), 5000, str(path))
                if relative == "ss-presets/print":
                    self.assertEqual(values.get("travel_deceleration_use_target", "1"), "1")
        for relative in ("orca", "orca-vendor/SVZero/process"):
            processes = [json.loads(p.read_text()) for p in (bundles / relative).glob("*.json")]
            processes = [p for p in processes if "@SV Zero " in p.get("name", "")]
            self.assertEqual(len(processes), expected)
            for process in processes:
                self.assertEqual(float(process["travel_acceleration"]), 5000, process["name"])
        # PS3 has one conditional print tree rather than 38 separate presets.
        document = yaml.safe_load((bundles / "ps3/SVZero/preset-print-svzero.yaml").read_text())
        def check_tree(node, inherited):
            values = inherited | node.get("values", {})
            self.assertEqual(float(values["travel_acceleration"]), 5000)
            for child in node.get("variants", []):
                check_tree(child, values)
        check_tree(document, {})

    def test_layer_limits_cover_every_emitted_process_in_all_slicers(self):
        model = G.load("model.json")
        # Read the shipped INIs, not the generator's conversion helper.
        bundles = {s: G.parse_ini(ROOT / "bundles" / f"SVZero_{s}.ini")
                   for s in ("SuperSlicer", "PrusaSlicer")}
        vendor = ROOT / "bundles/orca-vendor/SVZero"
        processes = [json.loads(p.read_text()) for p in (vendor / "process").glob("*.json")]

        def mm(value, nozzle):
            return float(value[:-1]) / 100 * float(nozzle) if value.endswith("%") else float(value)

        for nozzle in model["machine"]["nozzles"]:
            machine = json.loads((vendor / "machine" / f"SV Zero {nozzle} nozzle.json").read_text())
            bounds = tuple(float(machine[k][0]) for k in ("min_layer_height", "max_layer_height"))
            # The public product installs the self-contained vendor bundle;
            # host-dependent private loose machine presets are not emitted.
            # Pin both extremes so a shared conversion error cannot pass by agreement.
            if nozzle in ("0.2", "1.4"):
                self.assertEqual(bounds, {"0.2": (0.04, 0.16), "1.4": (0.28, 1.12)}[nozzle])
            heights = []
            for slicer, sections in bundles.items():
                printer = G.flatten(sections, f"printer:SV Zero {nozzle}n")
                for i, key in enumerate(("min_layer_height", "max_layer_height")):
                    self.assertAlmostEqual(mm(printer[key], nozzle), bounds[i], msg=(slicer, nozzle, key))
                for name in sections:
                    if name.startswith(f"print:SV Zero {nozzle}n - "):
                        preset = G.flatten(sections, name)
                        for key in ("layer_height", "first_layer_height"):
                            heights.append((slicer, name, key, mm(preset[key], nozzle)))
            for preset in processes:
                if f"SV Zero {nozzle} nozzle" in preset.get("compatible_printers", []):
                    for key in ("layer_height", "initial_layer_print_height"):
                        heights.append(("Orca", preset["name"], key, float(preset[key])))
            count = sum(n == nozzle for n, _, _ in G.expected_presets(model))
            self.assertEqual(len(heights), count * 3 * 2)
            for slicer, name, key, height in heights:
                with self.subTest(slicer=slicer, preset=name, key=key):
                    self.assertGreaterEqual(height + 1e-9, bounds[0])
                    self.assertLessEqual(height, bounds[1] + 1e-9)

    def test_invalid_machine_limits_are_rejected(self):
        model = G.load("model.json")
        for low, high in (("0", "80%"), ("80%", "20%"), ("20%", "101%"), ("20%", "nan")):
            with self.subTest(low=low, high=high):
                sections = G.sections_from_tree(G.load("presets.json"), model)
                sections["printer:SV Zero 0.2n"].update(min_layer_height=low, max_layer_height=high)
                errors = G.check_layer_limits(model, sections)
                self.assertTrue(any("0.2 nozzle: invalid layer limits" in e for e in errors), errors)

    def test_invalid_layers_cannot_overwrite_bundles(self):
        for nozzle, key, value in (("0.2", "layer_height", "0.03"),
                                   ("1.4", "layer_height", "1.13"),
                                   ("0.2", "first_layer_height", "0.2"),
                                   ("1.4", "first_layer_height", "81%"),
                                   ("0.2", "first_layer_height", "nan")):
            with self.subTest(nozzle=nozzle, key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                sections = G.sections_from_tree(G.load("presets.json"), G.load("model.json"))
                name = next(n for n in sections if n.startswith(f"print:SV Zero {nozzle}n - "))
                sections[name][key] = value
                sentinel = Path(tmp) / "bundles/SVZero_SuperSlicer.ini"
                sentinel.parent.mkdir()
                sentinel.write_text("previous working bundle\n")
                shutil.copytree(ROOT / "source", Path(tmp) / "source")
                output = io.StringIO()
                with patch.object(G, "sections_from_tree", return_value=sections), \
                        patch.object(G, "ROOT", tmp), \
                        patch.object(sys, "argv", ["generate.py", "--emit", "ss"]), \
                        contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
                    G.main()
                self.assertEqual(error.exception.code, 1)
                self.assertIn(key, output.getvalue())
                self.assertEqual(sentinel.read_text(), "previous working bundle\n")

    def test_incomplete_nozzle_cannot_overwrite_bundles(self):
        source = {p.name: json.loads(p.read_text()) for p in (ROOT / "source").glob("*.json")}
        printers = next(f for f in source["presets.json"]["fragments"] if f["kind"] == "print")
        printers["variants"] = [v for v in printers["variants"]
                                if v.get("condition") != "tool.nozzle_diameter == 0.5"]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(ROOT / "source", Path(tmp) / "source")
            bundle = Path(tmp) / "bundles/SVZero_SuperSlicer.ini"
            bundle.parent.mkdir()
            bundle.write_text("previous working bundle\n")
            with patch.object(G, "ROOT", tmp), patch.object(G, "load", source.__getitem__), \
                    patch.object(sys, "argv", ["generate.py", "--emit", "ss,ps"]), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                G.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(bundle.read_text(), "previous working bundle\n")
            self.assertEqual(list(bundle.parent.iterdir()), [bundle])

    def test_every_model_pair_has_sliceable_dimensions(self):
        model = G.load("model.json")
        vendor = ROOT / "bundles/orca-vendor/SVZero"
        processes = [json.loads(p.read_text()) for p in (vendor / "process").glob("*.json")]
        for nozzle, tier, height in G.expected_presets(model):
            with self.subTest(nozzle=nozzle, tier=tier):
                matches = [p for p in processes if p.get("name", "").endswith(
                    f" {tier} @SV Zero {nozzle} nozzle")]
                self.assertEqual(len(matches), 1)
                p = matches[0]
                self.assertEqual(float(p["layer_height"]), height)
                self.assertGreaterEqual(float(p["initial_layer_speed"]), 1.0)
                self.assertGreaterEqual(float(p["initial_layer_infill_speed"]), 1.0)
                self.assertAlmostEqual(float(p["outer_wall_line_width"]), float(nozzle))
                self.assertGreaterEqual(float(p["tree_support_tip_diameter"]), float(nozzle))
                self.assertGreaterEqual(float(p["tree_support_branch_diameter_organic"]),
                                        2 * float(nozzle))


if __name__ == "__main__":
    unittest.main()
