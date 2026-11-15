#!/usr/bin/env python3
"""The PrusaSlicer 3 bundle, held to what alpha12 and alpha13 actually load.

Each test pins a failure the binary showed, not a property of the schema. The
bundle is the generated one in bundles/ps3; verify.py's clean rebuild proves it
matches the generator.
"""
import collections
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("emit_ps3", ROOT / "tools/emit_ps3.py")
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

BUNDLE = ROOT / "bundles/ps3/SVZero"
SCHEMA = E.Schema(ROOT / "source/ps3/config-schema.json")


def docs(name):
    return [d for d in yaml.safe_load_all((BUNDLE / name).read_text(encoding="utf-8")) if d]


def leaves(node, inherited=None):
    """(values, path of conditions) for every leaf of a fragment tree."""
    values = dict(inherited or {}, **node.get("values", {}))
    if not node.get("variants"):
        yield values, node
        return
    for child in node["variants"]:
        yield from leaves(child, values)


def walk_strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_strings(v)
    elif isinstance(node, str):
        yield node


def ps2_prints():
    """{(display name, nozzle, precision)} from the flattened PrusaSlicer 2 presets."""
    found = set()
    for path in (ROOT / "bundles/ps-presets/print").glob("*.ini"):
        m = re.fullmatch(r"SV Zero (\d+(?:\.\d+)?)n - (.+)", path.stem)
        compat = next(line.split(" = ", 1)[1] for line in path.read_text().splitlines()
                      if line.startswith("compatible_printers ="))
        found.add((m.group(2), float(m.group(1)), "Precision" in compat))
    return found


class Ps3BundleTests(unittest.TestCase):
    def test_every_preset_kind_alpha12_needs_is_present(self):
        # Without a tool_print, or without a filament, alpha12 aborted in
        # PresetEvaluator::evaluate (exit 134) on any query.
        kinds = collections.Counter(d["kind"] for f in BUNDLE.glob("preset-*.yaml") for d in docs(f.name))
        for kind in ("printer", "print", "tool_print", "filament"):
            self.assertGreater(kinds[kind], 0, kind)

    def test_strings_are_real_text(self):
        # The first emitter wrote INI escapes into YAML; the start G-code
        # reached alpha12 as one line with literal backslash-n in it.
        for f in BUNDLE.glob("*.yaml"):
            for s in walk_strings(docs(f.name)):
                self.assertNotIn("\\n", s, f.name)
        (printer,) = docs("preset-printer-svzero.yaml")
        starts = [v["values"]["start_gcode"] for v in printer["variants"]]
        self.assertTrue(all("\nSTART_PRINT\n" in s for s in starts))

    def test_vectors_are_lists(self):
        (printer,) = docs("preset-printer-svzero.yaml")
        for values, _ in leaves(printer):
            for key, value in values.items():
                defn = SCHEMA.items["printer"].get(key)
                if defn and defn["type"] == "Array":
                    self.assertIsInstance(value, list, key)

    def test_every_print_has_the_retraction_ps3_moved_out_of_the_printer(self):
        # retract_* is print scope in PS3. The first emitter dropped all of it.
        for doc in docs("preset-print-svzero.yaml"):
            for values, _ in leaves(doc):
                for key in ("retract_length", "retract_lift", "retract_speed", "min_layer_height"):
                    self.assertIn(key, values, doc["name"])

    def test_tool_overridable_options_are_indexed_in_expressions(self):
        # alpha12 failed the whole start G-code, "(38, [start_gcode])", on a
        # bare first_layer_extrusion_width inside {}; PS2 needs it bare.
        (printer,) = docs("preset-printer-svzero.yaml")
        for variant in printer["variants"]:
            for expr in re.findall(r"\{[^{}]*\}", variant["values"]["start_gcode"]):
                for name in SCHEMA.tool_vectors:
                    self.assertIsNone(re.search(r"\b%s\b(?!\[)" % name, expr), expr)
        self.assertEqual(E.ps3_gcode("C={a + first_layer_extrusion_width + 2} [first_layer_extrusion_width]",
                                     SCHEMA),
                         "C={a + first_layer_extrusion_width[initial_tool] + 2} [first_layer_extrusion_width]")

    def test_every_ps2_print_preset_has_its_ps3_counterpart(self):
        ps3 = set()
        for doc in docs("preset-print-svzero.yaml"):
            precision = not doc["condition"].startswith("!")
            for noz in re.findall(r"tool\.nozzle_diameter == ([\d.]+)", doc["condition"]):
                ps3.add((doc["name"], float(noz), precision))
        self.assertEqual(ps3, ps2_prints())

    def test_precision_is_a_second_printer_model(self):
        # One model with two configs named both "SV Zero 0.4", and the CLI,
        # which selects by that name, only ever found one of them.
        vendor = docs("vendor.yaml")
        printers = {d["id"]: d for d in vendor if d["kind"] == "printer"}
        configs = [d for d in vendor if d["kind"] == "printer_config"]
        self.assertEqual(sorted(printers), ["svzero", "svzero-precision"])
        self.assertEqual({p["model"]["base_model"] for p in printers.values()}, {E.MODEL_ID})
        self.assertTrue(printers["svzero-precision"]["features"]["precision"]["default"])
        self.assertEqual(len({p["name"] for p in printers.values()}), 2)
        self.assertEqual(sorted(c["printer"] for c in configs), sorted(printers))
        # And it offers only the nozzles PS2 has Precision presets for.
        expected = {"supports_%s_nozzle" % E._nozzle_key(E._g(n)) for _, n, p in ps2_prints() if p}
        supported = {k for k in printers["svzero-precision"]["features"]
                     if k.startswith("supports_") and k.endswith("_nozzle")}
        self.assertEqual(supported, expected)

    def test_values_sit_under_the_named_printer_variants(self):
        # A named variant is the preset; values in an unnamed sibling are not
        # merged in. Laid out that way, alpha13 sliced with its own default
        # start G-code and machine limits.
        (printer,) = docs("preset-printer-svzero.yaml")
        named = {v["name"]: v for v in printer["variants"]}
        self.assertEqual(set(named), {"SV Zero", "SV Zero Precision"})
        self.assertTrue(all("name" in v for v in printer["variants"]))
        self.assertIn("MODE=standard", named["SV Zero"]["values"]["start_gcode"])
        self.assertIn("MODE=precision", named["SV Zero Precision"]["values"]["start_gcode"])

    def test_local_source_zip_is_the_bundle(self):
        version = (ROOT / "VERSION").read_text().strip()
        (vendor,) = [d for d in docs("vendor.yaml") if d["kind"] == "vendor"]
        self.assertEqual(vendor["version"], version)
        with zipfile.ZipFile(ROOT / "bundles/SVZero_PrusaSlicer3.zip") as z:
            names = set(z.namelist())
            files = sorted(p.name for p in BUNDLE.iterdir())
            self.assertEqual(names, {"manifest.json", "vendor_indices.zip",
                                     "SVZero/%s/manifest.json" % version}
                             | {"SVZero/%s/%s" % (version, f) for f in files})
            manifest = json.loads(z.read("SVZero/%s/manifest.json" % version))
            for entry in manifest:
                data = z.read("SVZero/%s/%s" % (version, entry["filename"]))
                self.assertEqual(data, (BUNDLE / entry["filename"]).read_bytes())
                self.assertEqual(entry["filehash"], hashlib.sha256(data).hexdigest())
            with zipfile.ZipFile(io.BytesIO(z.read("vendor_indices.zip"))) as idx:
                self.assertEqual(idx.read("SVZero.idx"),
                                 (ROOT / "bundles/ps3/SVZero.idx").read_bytes())
            self.assertEqual(json.loads(z.read("manifest.json"))["id"], "svzero-fff")

    def test_local_source_zip_is_stored(self):
        # Deflate's bytes differ between zlib builds; stored members rebuild alike.
        with zipfile.ZipFile(ROOT / "bundles/SVZero_PrusaSlicer3.zip") as z:
            self.assertEqual({i.compress_type for i in z.infolist()}, {zipfile.ZIP_STORED})
            with zipfile.ZipFile(io.BytesIO(z.read("vendor_indices.zip"))) as idx:
                self.assertEqual({i.compress_type for i in idx.infolist()}, {zipfile.ZIP_STORED})


class ConversionTests(unittest.TestCase):
    def test_values_take_their_schema_type(self):
        p = SCHEMA.items["printer"]
        f = SCHEMA.items["filament"]
        self.assertEqual(E.convert(p["machine_max_acceleration_x"], "", "40000,1000"),
                         ([40000.0, 1000.0], None))
        self.assertEqual(E.convert(p["bed_shape"], "", "0x0,152.4x0"), (["0x0", "152.4x0"], None))
        self.assertEqual(E.convert(f["filament_shrinkage_compensation_xy"], "", "0.3%"), ("0.3%", None))
        self.assertEqual(E.convert(SCHEMA.items["print"]["support_material"], "support_material", "1"),
                         ("everywhere", None))
        self.assertIsNotNone(E.convert(SCHEMA.items["print"]["bridge_flow_ratio"], "", "95%")[1])

    def test_strings_decode_as_ini_strings_writes_them(self):
        self.assertEqual(E.decode_string("a\\nb"), "a\nb")
        self.assertEqual(E.decode_string('"; Filament gcode\\nM117"'), "; Filament gcode\nM117")

    def test_keys_move_to_the_kind_ps3_keeps_them_in(self):
        report = collections.defaultdict(set)
        out = E.typed("filament", {"filament_retract_length": "0.4", "temperature": "230"},
                      SCHEMA, report)
        self.assertEqual(out["print"], {"retract_length": 0.4})
        self.assertEqual(out["filament"], {"temperature": 230})
        out = E.typed("print", {"first_layer_infill_speed": "0"}, SCHEMA, report)
        self.assertEqual(out["print"], {"first_layer_solid_infill_speed": 0.0})


if __name__ == "__main__":
    unittest.main()
