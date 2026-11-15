#!/usr/bin/env python3
"""Emit a PrusaSlicer 3.0 vendor bundle from the same presets PrusaSlicer 2 gets.

WHAT IT TAKES
--------------------------------------------------------------------------
The flattened PrusaSlicer 2 presets, exactly as `--emit ps-presets` writes them:
every translation PS2 needs has already run (absolute speeds, widths in mm,
shrinkage in PrusaSlicer's notation, pressure advance in the filament G-code).
PS3 shares that option vocabulary, so the cheapest correct input is the PS2
artefact, retyped. On alpha12 this reproduced 2.9.6's feeds within 0.4 mm/s and
widths within 0.003 mm on the harness feature cube; see
notes/kb/prusaslicer3-profiles.md in the project notes.

The first cut of this file built PS3 values from the master instead. alpha12,
the first release that could load a third-party bundle at all, rejected it six
ways, and the worst two were silent: G-code carried a literal backslash-n, and
every retraction key was dropped because PS3 moved it out of the printer.

TYPED BY THE BINARY'S OWN SCHEMA
--------------------------------------------------------------------------
source/ps3/config-schema.json is PrusaSlicer 3's `--export-config-schema`. It
says, per option, which preset kind owns it, its type and its enum values, and
that is what every value here is converted by:

  * vectors are YAML lists, not "a,b" strings ("Type mismatch" otherwise);
  * a key goes to the kind PS3 keeps it in. retract_*, wipe, min/max layer
    height and travel lift are PRINT options in PS3, so the printer INI's copy
    moves into the print presets of that printer;
  * a filament may set a print option only if the schema lists it among the
    filament overrides, and PS2's `filament_retract_length` spelling becomes
    `retract_length` there;
  * G-code is real text with real newlines.

PS2 vocabulary that does not map is reported, never silently dropped.

NOZZLE DIAMETER IS NOT A PRESET DIMENSION
--------------------------------------------------------------------------
It is a `tool` feature, and presets branch on `tool.nozzle_diameter`. PS2 needs
one printer and one print preset per nozzle; PS3 needs one printer and one print
preset per display name, with the per-nozzle differences as conditional
variants inside it.

PRECISION IS A SECOND PRINTER MODEL
--------------------------------------------------------------------------
PS2 ships separate "SV Zero Precision" printers: other machine limits, its own
start G-code, no Z lift, and print presets of their own. Here it is a second
`kind: printer` with the same base_model and the printer feature `precision`
set, and every preset that differs tests `printer.precision`. Presets that
belong to both test `printer.base_model`, as Prusa's MMU variants do.

Two things ruled out the alternatives, both measured on alpha13:

  * alpha13's preset-scope features would let one printer carry two printer
    presets. But the CLI resolves a printer config to the FIRST printer preset
    that matches it (ProfilePresetSelection.cpp), so a mode carried only by a
    printer preset could never be sliced headless, and the harness is how this
    bundle is tested.
  * A second printer_config on the same printer model, setting the feature
    itself, loaded and filtered correctly, but a config's name is composed
    from the printer model's name and its tools: both came out "SV Zero 0.4",
    and the CLI, which selects by that name, only ever found one.

A separate model is also what PS2 users already pick.

IDS MUST BE STABLE
--------------------------------------------------------------------------
Every fragment and variant carries an `id`; Prusa's are 22-character base64,
i.e. 16 random bytes. They look like the handle for update and merge, so they
are derived here from a hash of the fragment's logical path rather than
generated fresh. Random ids per run would churn every file on every
regeneration and make the bundle undiffable -- the same failure the Orca orphan
sweep taught us to care about.

INSTALLED AS A LOCAL SOURCE
--------------------------------------------------------------------------
Third-party vendors are not on Prusa's preset server yet, and
PRUSA_PRESET_REPO_URL accepts Prusa domains only. What alpha12 and later load is
a local source zip (`--preset-update-add-local`), whose layout is
write_local_source's docstring.
"""
import base64
import collections
import hashlib
import io
import json
import os
import re
import zipfile

import yaml

# vendor.yaml's base_model, shared by both printer models. Conditions test it
# as printer.base_model.
MODEL_ID = "SVZERO"

# PS2 keys with no PS3 home that are expected and not worth a report line:
# PS2's preset bookkeeping, and the nozzle, which is a tool feature here.
SKIP = frozenset((
    "inherits", "compatible_printers", "compatible_printers_condition",
    "compatible_prints", "compatible_prints_condition", "print_settings_id",
    "printer_settings_id", "filament_settings_id", "printer_variant",
    "printer_vendor", "nozzle_diameter",
))

# PS2's first_layer_infill_speed is labelled "First layer solid infill speed"
# and applies to solid infill (2.9.7 PrintConfig.cpp). PS3 split it into a
# sparse first_layer_infill_speed and a first_layer_solid_infill_speed with
# PS2's meaning, so carrying the name across would move the value onto the
# other kind of infill.
RENAMES = {"first_layer_infill_speed": "first_layer_solid_infill_speed"}

# Paths only install-presets.py can resolve; a PS3 bundle carries its own
# assets instead (`visual`), which this bundle does not have yet.
NO_PATH_KEYS = frozenset(("bed_custom_model", "bed_custom_texture"))


def stable_id(*parts):
    """A deterministic 22-char base64 id, shaped like Prusa's own.

    Hash of the logical path, so the same fragment keeps its id forever and a
    regeneration produces a byte-identical bundle when nothing has changed.
    """
    digest = hashlib.sha256("/".join(str(p) for p in parts).encode()).digest()
    return base64.b64encode(digest[:16]).decode("ascii").rstrip("=")


def _nozzle_key(noz):
    """0.4 -> '04', for feature names like supports_04_nozzle."""
    return str(noz).replace(".", "")


def _g(x):
    return "%g" % float(x)


# --- the schema --------------------------------------------------------------

class Schema:
    """config-schema.json, indexed the ways the conversion needs it."""

    KINDS = ("printer", "print", "filament")

    def __init__(self, path):
        raw = json.load(open(path, encoding="utf-8"))
        self.items = {k: {i["name"]: i for i in raw[k]["items"]} for k in self.KINDS}
        self.filament_overrides = {
            o if isinstance(o, str) else o["name"] for o in raw["filament"].get("overrides", [])}
        # A print option a tool may override is a per-tool VECTOR inside a
        # G-code expression. Found by bisecting alpha12's "(38, [start_gcode])":
        # PURGE_LINE's CLEAR={... first_layer_extrusion_width ...} failed until
        # indexed, while skirts and skirt_distance, which no tool overrides,
        # worked bare.
        self.tool_vectors = {n for n, i in self.items["print"].items()
                             if "Tool" in i.get("overrides_in", [])}

    def home(self, kind, key):
        """The kind PS3 keeps `key` in: its own kind first, then any other."""
        if key in self.items[kind]:
            return kind
        return next((k for k in self.KINDS if key in self.items[k]), None)


def decode_string(text):
    """INI single-string form to real text, as ini_strings() in generate.py reads it.

    A value already written as a serialized one-element vector ("...") is
    unquoted first. Then C-style escapes: backslash-n is a newline, and any other
    escaped character is itself.
    """
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)),
                  text, flags=re.S)


def convert(defn, key, text):
    """One INI value to the YAML value its schema type wants: (value, None) or (None, why)."""
    t, et = defn["type"], defn.get("element_type")
    try:
        if t == "Array":
            parts = [] if text == "" else text.split(",")
            if et == "Float":
                return [float(x) for x in parts], None
            if et == "Int":
                return [int(float(x)) for x in parts], None
            # Vec2d stays a sequence of "XxY" strings. config-schema.json
            # reports bed_shape's default as [[0,0],[200,0],...], but that is
            # the PARSED value; the loader rejected nested lists with "Node
            # type mismatch, expecting 'scalar' but got 'sequence'". A dumped
            # default is not a serialisation example.
            return parts, None
        if t == "Float":
            if text.endswith("%"):
                return None, "percentage into Float"
            return float(text), None
        if t in ("Int", "OptInt"):
            return int(float(text)), None
        if t == "Bool":
            return text.strip() in ("1", "true"), None
        if t == "Enum":
            if key == "support_material":
                # PS2 has a bool and a separate support_material_auto; PS3 has
                # one enum. typed() resolves the pair before it gets here.
                text = "everywhere" if text == "1" else "enforcers_only" if text == "enforcers" else "none"
            if text not in defn["enum_values"]:
                return None, "enum %r not in %s" % (text, defn["enum_values"])
            return text, None
        if t == "Percentage":
            if text.endswith("%"):
                return text, None
            f = float(text)
            return "%g%%" % (f * 100 if f <= 1 else f), None
        if t == "FloatOrPercentage":
            return (text if text.endswith("%") else float(text)), None
        if t == "String":
            return decode_string(text), None
    except ValueError:
        return None, "unparseable %r" % text[:40]
    return None, "unhandled type %s" % t


def ps3_gcode(text, schema):
    """Index every tool-overridable print option inside a {} expression.

    `[name]` placeholders are left alone: PS3 still resolves those. Only the
    expression language sees the per-tool vector.
    """
    if not schema.tool_vectors:
        return text

    def index(m):
        return re.sub(r"\b(%s)\b(?!\s*\[)" % "|".join(sorted(schema.tool_vectors)),
                      r"\1[initial_tool]", m.group(0))
    return re.sub(r"\{[^{}]*\}", index, text)


def typed(kind, kv, schema, report):
    """One PS2 preset's INI values split into {ps3 kind: values}."""
    out = collections.defaultdict(dict)
    if kind == "print" and "support_material" in kv:
        # PS2 "enforcers only" is support_material = 1 with auto off.
        if kv.get("support_material") == "1" and kv.get("support_material_auto") == "0":
            kv = dict(kv, support_material="enforcers")
    for key, text in kv.items():
        key = RENAMES.get(key, key)
        if kind == "filament" and key.startswith("filament_") and key[9:] in schema.filament_overrides:
            report["renamed filament_X to X, a filament override"].add(key)
            key = key[9:]
        if key in SKIP or key == "support_material_auto":
            continue
        if key in NO_PATH_KEYS:
            report["dropped: a path only install-presets.py resolves"].add(key)
            continue
        dest = schema.home(kind, key)
        if dest is None:
            report["dropped: not a PrusaSlicer 3 option"].add(key)
            continue
        val, why = convert(schema.items[dest][key], key, text)
        if why:
            report["PROBLEM %s %s: %s" % (dest, key, why)].add(key)
            continue
        if isinstance(val, str) and key.endswith("gcode"):
            new = ps3_gcode(val, schema)
            if new != val:
                report["indexed per-tool options in G-code expressions"].add(key)
            val = new
        if dest != kind:
            report["moved %s -> %s" % (kind, dest)].add(key)
        out[dest][key] = val
    return out


# --- presets -----------------------------------------------------------------

def _parse_printer(name, machine_name):
    m = re.fullmatch(re.escape(machine_name) + r" (?:(Precision) )?(\d+(?:\.\d+)?)n", name)
    if not m:
        raise ValueError("unrecognised printer preset %r" % name)
    return float(m.group(2)), bool(m.group(1))


def _factor(by_key):
    """Values every member shares, and each member's remainder."""
    members = list(by_key.values())
    common = {k: v for k, v in members[0].items()
              if all(k in m and m[k] == v for m in members)}
    return common, {key: {k: v for k, v in m.items() if k not in common}
                    for key, m in by_key.items()}


def _mode(precision):
    return "printer.precision" if precision else "!printer.precision"


def _nozzles(nozzles):
    conds = ["tool.nozzle_diameter == %s" % _g(n) for n in sorted(nozzles)]
    return conds[0] if len(conds) == 1 else "(%s)" % " or ".join(conds)


def _compat(printers, compatible, everyone):
    """A condition for a PS2 compatible_printers list."""
    if not compatible or set(compatible) >= everyone:
        return 'printer.base_model == "%s"' % MODEL_ID
    by_mode = collections.defaultdict(set)
    for p in compatible:
        noz, prec = printers[p]
        by_mode[prec].add(noz)
    parts = []
    for prec, nozs in sorted(by_mode.items()):
        all_of_mode = {n for (n, pr) in printers.values() if pr == prec}
        parts.append(_mode(prec) if nozs == all_of_mode
                     else "%s and %s" % (_mode(prec), _nozzles(nozs)))
    cond = parts[0] if len(parts) == 1 else " or ".join("(%s)" % p for p in parts)
    return 'printer.base_model == "%s" and %s' % (MODEL_ID, cond if len(parts) == 1 else "(%s)" % cond)


def build_presets(presets, schema, model, report):
    """printer, print, tool_print and filament documents from the PS2 presets.

    `presets` is {kind: {preset name: {key: INI text}}}.
    """
    machine_name = model["machine"]["name"]
    printers = {}            # PS2 printer name -> (nozzle, precision)
    printer_vals = {}        # (nozzle, precision) -> printer-scope values
    printer_print = {}       # (nozzle, precision) -> print-scope values it carries
    for name, kv in presets["printer"].items():
        key = _parse_printer(name, machine_name)
        printers[name] = key
        t = typed("printer", kv, schema, report)
        printer_vals[key] = t["printer"]
        printer_print[key] = t["print"]
        for k in t["filament"]:
            report["PROBLEM printer sets filament option %s" % k].add(k)

    # Printer: what all share, then what each machine mode shares, then each
    # printer's own. Factored per mode so the machine limits and start G-code
    # are written once per mode rather than once per nozzle.
    common, rest = _factor(printer_vals)
    modes = sorted({prec for (_, prec) in printer_vals})
    mode_common = {prec: _factor({k: v for k, v in rest.items() if k[1] == prec})[0]
                   for prec in modes}
    dflt_tier = model["tiers"].get("_default_print_tier", "Standard")
    groups = collections.defaultdict(dict)   # display name -> {nozzle: values}
    group_mode = {}
    for name, kv in presets["print"].items():
        m = re.fullmatch(re.escape(machine_name) + r" (\d+(?:\.\d+)?)n - (.+)", name)
        if not m:
            report["PROBLEM unrecognised print preset name"].add(name)
            continue
        display = m.group(2)
        compat = [p for p in kv.get("compatible_printers", "").split(";") if p]
        if len(compat) != 1 or compat[0] not in printers:
            report["PROBLEM print preset not tied to exactly one printer"].add(name)
            continue
        noz, prec = printers[compat[0]]
        if noz != float(m.group(1)):
            report["PROBLEM print preset nozzle disagrees with its printer"].add(name)
            continue
        if group_mode.setdefault(display, prec) != prec:
            report["PROBLEM print name used by both machine modes"].add(display)
            continue
        t = typed("print", kv, schema, report)
        for k in set(t) - {"print"}:
            report["PROBLEM print preset sets %s options" % k].update(t[k])
        vals = dict(printer_print[(noz, prec)])
        vals.update(t["print"])
        groups[display][noz] = vals

    defaults = {}
    for display, by_noz in groups.items():
        if not group_mode[display] and display.endswith("(%s)" % dflt_tier):
            for noz in by_noz:
                defaults[noz] = display
    # A NAMED variant is a preset, and only it and its descendants make it up:
    # values in an unnamed SIBLING are not merged in. The first layout put the
    # mode values beside the two named variants; alpha13 loaded it cleanly and
    # sliced with PS3's default start G-code and machine limits. Prusa's
    # preset-printer-coreone.yaml nests the same way: the named variant holds
    # the values, and the MMU3 refinement sits beneath it.
    variants = []
    for prec, label in ((False, machine_name), (True, machine_name + " Precision")):
        if prec not in modes:
            continue
        nested = []
        for noz in sorted(n for (n, p) in printer_vals if p == prec):
            own = {k: v for k, v in rest[(noz, prec)].items() if k not in mode_common[prec]}
            if not prec and noz in defaults:
                own["default_print"] = defaults[noz]
            if own:
                nested.append({"condition": "tool.nozzle_diameter == %s" % _g(noz),
                               "id": stable_id("printer", MODEL_ID, _g(noz),
                                               "precision" if prec else "standard"),
                               "values": own})
        named = {"condition": 'printer.base_model == "%s" and %s' % (MODEL_ID, _mode(prec)),
                 "id": stable_id("printer", MODEL_ID, "name", prec),
                 "name": label,
                 "values": mode_common[prec]}
        if nested:
            named["variants"] = nested
        variants.append(named)
    printer_doc = {
        "kind": "printer",
        # NOT a *starred* id. PS2's convention -- a name wrapped in asterisks is
        # an abstract preset, never instantiated -- carries into PS3, and Prusa
        # follow it exactly. Starred here, alpha11 loaded the bundle with no
        # errors at all and simply produced no printer.
        "id": stable_id("printer", MODEL_ID, "root"),
        "values": common,
        "variants": variants,
    }

    print_docs = []
    for display in sorted(groups):
        by_noz = groups[display]
        shared, own = _factor(by_noz)
        doc = {"kind": "print", "id": stable_id("print", display), "name": display,
               "condition": "%s and %s" % (_mode(group_mode[display]), _nozzles(by_noz)),
               "values": shared}
        vs = [{"condition": "tool.nozzle_diameter == %s" % _g(n),
               "id": stable_id("print", display, _g(n)), "values": own[n]}
              for n in sorted(by_noz) if own[n]]
        if vs:
            doc["variants"] = vs
        print_docs.append(doc)

    everyone = set(printers)
    filament_docs = []
    for name, kv in sorted(presets["filament"].items()):
        t = typed("filament", kv, schema, report)
        vals = dict(t["filament"])
        for k, v in t["print"].items():
            if k in schema.filament_overrides:
                vals[k] = v
            else:
                report["PROBLEM filament sets print option %s, not a filament override" % k].add(k)
        for k in t["printer"]:
            report["PROBLEM filament sets printer option %s" % k].add(k)
        compat = [p for p in kv.get("compatible_printers", "").split(";") if p]
        unknown = [p for p in compat if p not in printers]
        if unknown:
            report["PROBLEM filament compatible with unknown printers"].add(name)
        filament_docs.append({
            "kind": "filament", "id": stable_id("filament", name), "name": name,
            "condition": _compat(printers, [p for p in compat if p in printers], everyone),
            "values": vals})

    # A bundle without a tool_print or without a filament makes alpha12 abort
    # in PresetEvaluator::evaluate (exit 134) when anything is queried. The
    # Zero has one tool, so this is the single-tool stub Prusa ship, "no tool".
    tool_print_doc = {
        "kind": "tool_print", "id": stable_id("tool_print", MODEL_ID),
        "variants": [{"condition": 'printer.base_model == "%s"' % MODEL_ID,
                      "id": stable_id("tool_print", MODEL_ID, "no tool"),
                      "name": "no tool"}],
    }
    precision_nozzles = sorted(n for (n, p) in printer_vals if p)
    return printer_doc, print_docs, tool_print_doc, filament_docs, precision_nozzles


# --- vendor.yaml ---------------------------------------------------------------

def build_vendor_docs(model, version, precision_nozzles, printer_name):
    """vendor + printer + tools + sheet + feeder + printer_configs.

    One YAML stream, matching how Prusa keep theirs: their vendor.yaml is 69
    documents of six different kinds.
    """
    machine = model["machine"]
    nozzles = [float(n) for n in machine["nozzles"]]

    printer_features = {
        # Declared at vendor scope with a default, overridden per printer, which
        # is how one tool definition can serve several machines. Ours is a
        # single machine today; the shape is what matters for when it is not.
        "supports_high_flow_nozzle": {"default": False, "user_editable": False},
        "supports_non_high_flow_nozzle": {"default": True, "user_editable": False},
        # The Zero has no input shaper of its own; resonance is handled in
        # Klipper, not by the slicer. See notes/kb/input-shaping.md.
        "input_shaper": {"default": False, "user_editable": False},
        "multi_extruder": {"default": False, "user_editable": False},
        "chamber_temperature_control": {"default": True, "user_editable": False},
        # Set by the Precision printer model; see the module docstring.
        "precision": {"default": False, "user_editable": False},
    }
    # Off unless a printer model turns it on, so the Precision model offers
    # only the nozzles PS2 has Precision presets for -- there is no
    # "SV Zero Precision 0.6n".
    for noz in nozzles:
        printer_features["supports_%s_nozzle" % _nozzle_key(_g(noz))] = {"default": False}

    def printer_model(pid, name, model_id, nozzle_set, extra=None):
        features = {"supports_%s_nozzle" % _nozzle_key(_g(n)): {"default": True}
                    for n in nozzle_set}
        features.update(extra or {})
        return {"kind": "printer", "technology": "FFF", "id": pid, "name": name,
                "model": {"base_model": MODEL_ID, "model": model_id},
                "tool_count": 1, "features": features}

    docs = [
        {
            "kind": "vendor",
            "id": "SVZero",
            "name": "SV Zero FFF",
            "version": version,
            "features": {
                "printer": printer_features,
                "tool": {
                    "nozzle_diameter": {"default": 0.4, "user_editable": False},
                    "nozzle_high_flow": {"default": False, "user_editable": False},
                },
                # The Zero ships one plate: the golden PEI. `cold` exists because
                # a cryoplate profile is a live possibility -- chamber_preheat.cfg
                # already reasons about one -- and a sheet feature is where that
                # belongs rather than a bed-temperature inference.
                "sheet": {"cold": {"default": False, "user_editable": False}},
                "feeder": {"single_mode": {"default": True, "user_editable": False}},
            },
        },
        printer_model("svzero", printer_name, MODEL_ID, nozzles),
    ]
    if precision_nozzles:
        docs.append(printer_model("svzero-precision", printer_name + " Precision",
                                  MODEL_ID + "PRECISION", precision_nozzles,
                                  {"precision": {"default": True}}))

    for noz in nozzles:
        docs.append({
            "kind": "tool",
            "technology": "FFF",
            "id": _g(noz),
            "name": _g(noz),
            # Simpler than Prusa's, and honestly so: they carry MMU and
            # high-flow terms because they have those machines.
            "condition": "printer.supports_%s_nozzle" % _nozzle_key(_g(noz)),
            "features": {"nozzle_diameter": {"default": noz}},
        })

    docs += [
        {
            "kind": "sheet",
            "id": "golden_pei",
            "name": "Golden PEI",
            "type": "pei_textured",
        },
        {
            "kind": "feeder",
            "technology": "FFF",
            "id": "direct",
            "name": "Direct drive",
            "feeder_type": "manual",
            # REQUIRED BY THE LOADER, OPTIONAL IN THE SCHEMA. vendor-schema.json
            # lists `model` among feeder-def's properties but not in its
            # `required`, and BundleLoader.cpp:119 rejected the whole bundle
            # with "Required field 'model' not found". Prusa's MMU3 feeder
            # carries one.
            "model": {"base_model": "DIRECT", "model": "DIRECT"},
            "slot_count": 1,
        },
    ]
    for pid in ("svzero", "svzero-precision") if precision_nozzles else ("svzero",):
        name = printer_name + (" Precision" if pid.endswith("precision") else "")
        docs.append({
            "kind": "printer_config",
            "id": pid,
            "name": name,
            "printer": pid,
            # The printer_model PS2 profiles and their G-code carry, so a PS2
            # project finds this printer. Prusa set it on every config.
            "legacy_printer_model": [printer_name],
            # The nozzle the machine ships with. The others are offered by the
            # Add Printer wizard because they are tools, not presets.
            "tools": [{"tool": "0.4"}],
            "sheet": "golden_pei",
        })
    return docs


def write_stream(path, docs):
    """Multi-document YAML, the way Prusa's own bundle is written."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump_all(docs, fh, sort_keys=False, default_flow_style=False,
                           allow_unicode=True, width=100)


# --- schema checking -------------------------------------------------------
#
# jsonschema is not installed and generate.py already hand-rolls its own check
# against preset-schema.json for the same reason. This is the same trade: the
# vendored schemas are the contract, and the parts worth enforcing without a
# validator are `kind`, the required keys, and that no unknown key sneaks in --
# `additionalProperties: false` is set on every definition in both schemas, so an
# unknown key is a hard error in the real validator too.

def _defs(schema):
    return schema.get("$defs", {})


def check_docs(docs, schema, kind_to_def):
    problems = []
    defs = _defs(schema)
    for i, doc in enumerate(docs):
        kind = doc.get("kind")
        name = kind_to_def.get(kind)
        if name is None:
            problems.append("doc %d: unknown kind %r" % (i, kind))
            continue
        spec = defs[name]
        props = set(spec.get("properties") or {})
        # allOf-referenced parents contribute their properties too.
        for parent in spec.get("allOf", []):
            ref = parent.get("$ref", "").rsplit("/", 1)[-1]
            props |= set((defs.get(ref) or {}).get("properties") or {})
            for gp in (defs.get(ref) or {}).get("allOf", []):
                gref = gp.get("$ref", "").rsplit("/", 1)[-1]
                props |= set((defs.get(gref) or {}).get("properties") or {})
        for req in spec.get("required", []):
            if req not in doc:
                problems.append("doc %d (%s): missing required %r" % (i, kind, req))
        for key in doc:
            if key not in props:
                problems.append("doc %d (%s): unknown key %r "
                                "(additionalProperties is false)" % (i, kind, key))
    return problems


VENDOR_KINDS = {
    "vendor": "vendor-def", "printer": "printer-def", "tool": "tool-def",
    "feeder": "feeder-def", "sheet": "sheet-def",
    "printer_config": "printer-config-template",
}


# --- the local source zip --------------------------------------------------------

def _zip_bytes(members):
    """A deterministic zip: sorted names, fixed timestamps, fixed attributes.

    Stored, not deflated: deflate's bytes depend on the zlib build, and
    Fedora's zlib-ng and CI's stock zlib 1.3.1 deflate the same members
    differently, so a deflated archive committed from one fails the
    rebuild check on the other.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, data)
    return buf.getvalue()


def write_local_source(bundle_dir, zip_path, vendor, version):
    """Package the bundle as the local source zip PrusaSlicer 3 installs.

    Layout read from the alpha12 and alpha13 source, and installed by both:

      manifest.json                      name, id, url (LocalPresetUpdaterRepository)
      vendor_indices.zip                 <Vendor>.idx (stage_indices_from_repository)
      <Vendor>/<version>/manifest.json   [{filename, filehash: sha256 hex}]
      <Vendor>/<version>/<files>

    `id` names the directory the presets land in (<datadir>/presets/local/<id>).
    """
    vdir = os.path.join(bundle_dir, vendor)
    files = sorted(f for f in os.listdir(vdir) if os.path.isfile(os.path.join(vdir, f)))
    data = {f: open(os.path.join(vdir, f), "rb").read() for f in files}
    manifest = [{"filename": f, "filehash": hashlib.sha256(data[f]).hexdigest()} for f in files]
    idx = "%s.idx" % vendor
    members = {
        "manifest.json": (json.dumps({
            "name": "SV Zero profile pack", "id": "svzero-fff", "url": "",
            "description": "Sovol Zero profiles for PrusaSlicer 3"}, indent=2) + "\n").encode(),
        "vendor_indices.zip": _zip_bytes({idx: open(os.path.join(bundle_dir, idx), "rb").read()}),
        "%s/%s/manifest.json" % (vendor, version): (json.dumps(manifest, indent=2) + "\n").encode(),
    }
    for f in files:
        members["%s/%s/%s" % (vendor, version, f)] = data[f]
    with open(zip_path, "wb") as fh:
        fh.write(_zip_bytes(members))


# --- emit ------------------------------------------------------------------------

MIN_SLICER = "3.0.0-alpha12"


def emit(model, out_dir, version, presets, idx_lines, zip_path):
    """Write the bundle and its local source zip.

    Returns (files written, problems, report). `presets` is the flattened PS2
    output as INI text, {kind: {preset name: {key: text}}}. `report` maps a
    category to the keys in it; a category starting PROBLEM is also a problem.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    src = os.path.join(root, "source", "ps3")
    schema = Schema(os.path.join(src, "config-schema.json"))
    vendor_schema = json.load(open(os.path.join(src, "vendor-schema.json"), encoding="utf-8"))
    preset_schema = json.load(open(os.path.join(src, "preset-schema.json"), encoding="utf-8"))

    report = collections.defaultdict(set)
    printer_doc, print_docs, tool_print_doc, filament_docs, precision_nozzles = \
        build_presets(presets, schema, model, report)
    vendor_docs = build_vendor_docs(model, version, precision_nozzles, model["machine"]["name"])

    problems = check_docs(vendor_docs, vendor_schema, VENDOR_KINDS)
    problems += check_docs([printer_doc, tool_print_doc] + print_docs + filament_docs,
                           preset_schema, {k: "top-fragment" for k in (
                               "printer", "print", "tool_print", "material", "filament")})
    problems += ["%s: %s" % (k, ", ".join(sorted(v))) for k, v in report.items()
                 if k.startswith("PROBLEM")]

    vend = os.path.join(out_dir, "SVZero")
    os.makedirs(vend, exist_ok=True)
    # Clear before writing anything -- and that ordering is not incidental. The
    # Orca vendor assembler put its clear AFTER the machine_model write on
    # 2026-09-05 and deleted the file it had just created, killing the whole
    # bundle. Every write in this function follows this loop.
    for old in os.listdir(vend):
        if old.endswith(".yaml"):
            os.remove(os.path.join(vend, old))
    write_stream(os.path.join(vend, "vendor.yaml"), vendor_docs)
    write_stream(os.path.join(vend, "preset-printer-svzero.yaml"), [printer_doc])
    write_stream(os.path.join(vend, "preset-print-svzero.yaml"), print_docs)
    write_stream(os.path.join(vend, "preset-tool-svzero.yaml"), [tool_print_doc])
    write_stream(os.path.join(vend, "preset-filament-svzero.yaml"), filament_docs)
    # The .idx keeps PS2's shape: min_slic3r_version, then a reverse
    # chronological changelog. Prusa's own alpha13 .idx still looks like this.
    with open(os.path.join(out_dir, "SVZero.idx"), "w", encoding="utf-8") as fh:
        fh.write("min_slic3r_version = %s\n" % MIN_SLICER)
        for line in idx_lines:
            fh.write(line + "\n")
    write_local_source(out_dir, zip_path, "SVZero", version)
    return 7, problems, {k: sorted(v) for k, v in sorted(report.items())}
