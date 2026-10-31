#!/usr/bin/env python3
"""The Brass/Steel presets carry upstream Orca's temperatures and flow caps.

source/filament-nozzle-upstream.json is read from the pinned files in
source/vendor-profiles/OrcaSlicer/ and applied after the (8M) table, on the six
Brass/Steel presets only (operator, 2026-10-18). Interim values, not
measurements; see the table's own annotations. The emitted temperatures are
the table's rebased for the PT1000 reading (source/hotend-sensor.json).
"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)

TABLE = G.load("filament-nozzle-upstream.json")
EIGHT_M = G.load("filament-8m.json")["overrides"]
KEYS = {k: v for k, v in TABLE["_keys"].items() if not k.startswith("_")}
ORCA = ROOT / "bundles/orca-vendor/SVZero/filament"
UNSPLIT = ("SV Zero ABS", "SV Zero PC", "SV Zero TPU")
SENSOR = G.load("hotend-sensor.json")


def expected(key, value):
    """The table's value as emitted: temperatures rebased for the PT1000 reading."""
    if key in SENSOR["keys"]:
        return str(G.rebased_temperature(SENSOR, value))
    return value


def derived():
    model = G.load("model.json")
    ss = G.sections_from_tree(G.load("presets.json"), model)
    G.derive(model, ss, G.load("keymap.json"))
    return ss


class NozzleUpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ss = derived()

    def test_table_matches_its_pinned_files(self):
        for preset, kv in TABLE["overrides"].items():
            src = TABLE["_source_profile"][preset]
            up = json.loads((Path(G.UPSTREAM_ORCA_FILAMENT) / (src + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(up["name"], src)
            for key, value in kv.items():
                with self.subTest(preset=preset, key=key):
                    self.assertEqual(up[KEYS[key]], [value])

    def test_a_drifted_value_stops_the_build(self):
        bad = copy.deepcopy(TABLE)
        bad["overrides"]["SV Zero PLA - Steel"]["temperature"] = "235"
        ss = G.sections_from_tree(G.load("presets.json"), G.load("model.json"))
        with self.assertRaises(SystemExit):
            G.apply_nozzle_upstream(ss, bad)

    def test_wins_over_8m_on_the_six_and_only_there(self):
        for preset, kv in TABLE["overrides"].items():
            flat = G.flatten(self.ss, "filament:" + preset)
            with self.subTest(preset=preset):
                for key, value in kv.items():
                    self.assertEqual(flat[key], expected(key, value))
                # 8M's pressure advance, flow ratio and retraction still apply.
                for key, value in EIGHT_M[preset].items():
                    if key not in kv:
                        self.assertEqual(flat[key], value)
                self.assertIn("[Orca #15756]", flat["filament_notes"])
        for preset in UNSPLIT:
            flat = G.flatten(self.ss, "filament:" + preset)
            with self.subTest(preset=preset):
                for key in ("temperature", "first_layer_temperature"):
                    self.assertEqual(flat[key], expected(key, EIGHT_M[preset][key]))
                self.assertNotIn("[Orca #15756]", flat["filament_notes"])

    def test_pairs_differ_where_upstream_differs(self):
        for material in ("PLA", "PLA Silk", "PETG"):
            brass = G.flatten(self.ss, "filament:SV Zero %s - Brass" % material)
            steel = G.flatten(self.ss, "filament:SV Zero %s - Steel" % material)
            with self.subTest(material=material):
                self.assertNotEqual(brass["temperature"], steel["temperature"])

    def test_emitted_orca_filaments_carry_the_table(self):
        for preset, kv in TABLE["overrides"].items():
            doc = json.loads((ORCA / (preset + ".json")).read_text(encoding="utf-8"))
            for key, value in kv.items():
                with self.subTest(preset=preset, key=key):
                    self.assertEqual(doc[KEYS[key]], [expected(key, value)])


if __name__ == "__main__":
    unittest.main()
