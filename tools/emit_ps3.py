#!/usr/bin/env python3
"""Emit a PrusaSlicer 3.0 vendor bundle from the same master everything else uses.

FIRST CUT, and deliberately scoped. PrusaSlicer 3.0.0-alpha11 ships Prusa
profiles ONLY -- third-party vendors are not loadable yet, by Prusa's stated
choice while they validate the new profile system on their own hardware. So this
cannot be tested in the application. What it CAN do, and what makes it worth
writing now rather than later, is stay honest against
`source/ps3/{preset,vendor}-schema.json`, which are vendored verbatim from the
tag and re-checked against it.

WHY THIS IS NOT emit_ps_vendor WITH A DIFFERENT WRITER
--------------------------------------------------------------------------
PS2 and PS3 share an option vocabulary and almost nothing else. See
notes/kb/prusaslicer3-profiles.md for the full review; the three that shape this
file:

  * NOZZLE DIAMETER IS NOT A PRESET DIMENSION any more. It is a `tool` feature,
    and presets branch on `tool.nozzle_diameter`. PS2 needs one printer preset
    and one process preset per nozzle -- 5 and 19 of them for us. PS3 needs one
    printer and a set of tools, with the per-nozzle differences expressed as
    conditional variants inside a single print preset.

  * A PRINTER IS A COMPOSITION: `printer` (the model) + `tool` (nozzles) +
    `sheet` (build plates) + `feeder`, assembled by a `printer_config`. We have
    never modelled sheets or feeders, so both are introduced here.

  * INHERITANCE IS A CONDITIONAL TREE. `inherits` is an array, fragments carry
    nested `variants`, and each variant may carry its own `condition`. The last
    variant in a list, the one with no condition, is the fallback.

IDS MUST BE STABLE
--------------------------------------------------------------------------
Every fragment and variant carries an `id`; Prusa's are 22-character base64,
i.e. 16 random bytes. They look like the handle for update and merge, so they are
derived here from a hash of the fragment's logical path rather than generated
fresh. Random ids per run would churn every file on every regeneration and make
the bundle undiffable -- the same failure the Orca orphan sweep taught us to care
about.
"""
import base64
import hashlib
import json
import os

import yaml


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


def build_vendor_docs(model, version):
    """vendor + printer + tools + sheet + feeder + printer_config.

    One YAML stream, matching how Prusa keep theirs: their vendor.yaml is 69
    documents of six different kinds.
    """
    machine = model["machine"]
    nozzles = machine["nozzles"]

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
    }
    for noz in nozzles:
        printer_features["supports_%s_nozzle" % _nozzle_key(noz)] = {"default": True}

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
        {
            "kind": "printer",
            "technology": "FFF",
            "id": "svzero",
            "name": machine["name"],
            "model": {"base_model": "SVZERO", "model": "SVZERO"},
            "tool_count": 1,
            "features": {
                "supports_%s_nozzle" % _nozzle_key(n): {"default": True}
                for n in nozzles
            },
        },
    ]

    for noz in nozzles:
        docs.append({
            "kind": "tool",
            "technology": "FFF",
            "id": str(noz),
            "name": str(noz),
            # Simpler than Prusa's, and honestly so: they carry MMU and
            # high-flow terms because they have those machines. Ours is one
            # feature test, and it stays a condition rather than an omission so
            # a printer that drops a nozzle size drops the tool with it.
            "condition": "printer.supports_%s_nozzle" % _nozzle_key(noz),
            "features": {"nozzle_diameter": {"default": float(noz)}},
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
            # `required`, and the schema check here passed without it -- while
            # BundleLoader.cpp:119 rejected the whole bundle with "Required
            # field 'model' not found". Prusa's own MMU3 feeder carries one.
            # First place found where the shipped schema is looser than the
            # application; there will be others, which is the argument for
            # testing every bundle against the binary and not just the schema.
            "model": {"base_model": "DIRECT", "model": "DIRECT"},
            "slot_count": 1,
        },
        {
            "kind": "printer_config",
            "id": "svzero",
            "name": machine["name"],
            "printer": "svzero",
            # legacy_printer_model is how a config claims the printer_model
            # string that older G-code and profiles use; Prusa set it on every
            # one of theirs. tool_count is dropped here: the `printer` it
            # references already declares it, and Prusa's own configs omit it.
            "legacy_printer_model": ["SVZERO"],
            # The nozzle the machine ships with. The others are reachable
            # because they are tools, not because they are presets -- that is
            # the whole change.
            "tools": [{"tool": "0.4"}],
            "sheet": "golden_pei",
        },
    ]
    return docs


def known_options(kind):
    """Option names PS3 is observed to use for this preset kind, or None.

    AUTHORITATIVE since 2026-09-05: source/ps3/ps3-known-options.json is derived
    from `--export-config-schema` on the installed alpha11 flatpak, which is the
    application's own data model rather than a guess at it.

    It replaced two worse approximations in one day. Scraping option names from
    Prusa's CORE One presets was too NARROW -- it only sees what they bother to
    set, and stripped resolution, raft_layers and xy_size_compensation. Unioning
    that with PrusaSlicer 2's --help-fff list to compensate was too WIDE: it
    re-admitted support_material_auto, which PS3 does not have at all. Both
    failure modes are silent, and in opposite directions.

    Returning None means "no vocabulary for this kind", and the caller must then
    emit everything rather than silently dropping it: an unknown vocabulary is
    not evidence that a key is invalid.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "source", "ps3",
                        "ps3-known-options.json")
    if not os.path.exists(path):
        return None
    entry = json.load(open(path, encoding="utf-8"))["options"].get(kind)
    if not entry:
        return None
    # items are what this kind declares; overrides are what it may restate from
    # another kind (tool_print declares nothing and overrides 136 print keys).
    return set(entry.get("items") or []) | set(entry.get("overrides") or [])



def filter_values(values, vocab, dropped):
    """Drop keys PS3 does not have, recording them for the caller to report.

    THE SAME TRAP THE PS2 EMITTER ALREADY HIT. nozzle-overrides.json is written
    in SuperSlicer's vocabulary, which is a superset of PrusaSlicer's, so
    first_layer_infill_extrusion_width has to be dropped rather than emitted and
    rejected -- verified absent from PS3's own CORE One presets while the other
    six extrusion widths are all present. Dropped, never silently: a key that
    disappears without a word is how a nozzle ends up inheriting the wrong
    width.
    """
    if vocab is None:
        return dict(values)
    kept = {}
    for k, v in values.items():
        if k in vocab:
            kept[k] = v
        else:
            dropped.add(k)
    return kept


def build_print_doc(model, per_nozzle_values, common_values, dropped=None):
    """One print preset whose per-nozzle differences are conditional variants.

    This is the shape that replaces 19 separate process presets. `values` holds
    what every nozzle shares; each variant narrows on tool.nozzle_diameter.
    """
    dropped = dropped if dropped is not None else set()
    vocab = known_options("print")
    variants = []
    for noz in model["machine"]["nozzles"]:
        vals = per_nozzle_values.get(str(noz))
        if not vals:
            continue
        variants.append({
            "condition": "tool.nozzle_diameter == %s" % noz,
            "id": stable_id("print", "nozzle", noz),
            "values": filter_values(vals, vocab, dropped),
        })
    # An unconditional last variant is the fallback, following Prusa's own
    # layout. Empty rather than absent: the tree must always resolve.
    variants.append({"id": stable_id("print", "fallback"), "values": {}})
    return {
        "kind": "print",
        "id": stable_id("print", "svzero", "root"),
        "name": "SV Zero",
        "values": filter_values(common_values, vocab, dropped),
        "variants": variants,
    }


def build_printer_doc(model, printer_values):
    """The printer PRESET -- distinct from vendor.yaml's `kind: printer`.

    This distinction cost a load. vendor.yaml's `kind: printer` is the HARDWARE
    MODEL: tool_count, features, which nozzles exist. It is not enough to make a
    printer appear. The settings -- bed_shape, gcode_flavor, start/end G-code --
    and the name the user actually sees live in a `kind: printer` PRESET in a
    preset-*.yaml, exactly as Prusa split preset-printer-coreone.yaml from their
    vendor.yaml entry. With only the vendor half, alpha11 loaded our bundle
    without a single error and simply listed no such printer.

    The display name goes on a variant conditioned on printer.model, which is
    how Prusa get "Prusa CORE One & CORE One+" onto one model.
    """
    machine = model["machine"]
    bed_x, bed_y = machine["bed"][0], machine["bed"][1]
    return {
        "kind": "printer",
        # NOT a *starred* id. PS2's convention -- a name wrapped in asterisks is
        # an abstract preset, never instantiated -- carries into PS3, and Prusa
        # follow it exactly: their abstract fragments are '*common*' and
        # '*default_print_C1*' while the printer preset a user can actually pick
        # carries an opaque id. Starred here, the bundle loaded with no errors
        # at all and simply produced no printer.
        "id": stable_id("printer", "svzero", "root"),
        "values": printer_values,
        "variants": [
            {
                "condition": 'printer.model == "SVZERO"',
                "id": stable_id("printer", "SVZERO"),
                "name": machine["name"],
                "values": {
                    # A SEQUENCE OF "XxY" STRINGS, not a sequence of pairs.
                    # config-schema.json reports the default as
                    # [[0,0],[200,0],...], but that is the PARSED value; the
                    # YAML wants Prusa's own on-disk form and the loader
                    # rejected the nested list with "Node type mismatch,
                    # expecting 'scalar' but got 'sequence'". A dumped default
                    # is not a serialisation example.
                    "bed_shape": ["0x0", "%gx0" % bed_x,
                                  "%gx%g" % (bed_x, bed_y), "0x%g" % bed_y],
                    "max_print_height": float(machine["height"]),
                    "printer_model": "SVZERO",
                },
            },
        ],
    }


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


def emit(model, out_dir, version, per_nozzle_values, common_values,
         printer_values, idx_lines):
    """Write the bundle and return (files_written, problems)."""
    vend = os.path.join(out_dir, "SVZero")
    os.makedirs(vend, exist_ok=True)
    # Clear before writing anything -- and that ordering is not incidental. The
    # Orca vendor assembler put its clear AFTER the machine_model write on
    # 2026-09-05 and deleted the file it had just created, killing the whole
    # bundle. Every write in this function follows this loop.
    for old in os.listdir(vend):
        if old.endswith(".yaml"):
            os.remove(os.path.join(vend, old))

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    vendor_schema = json.load(open(
        os.path.join(root, "source", "ps3", "vendor-schema.json"), encoding="utf-8"))
    preset_schema = json.load(open(
        os.path.join(root, "source", "ps3", "preset-schema.json"), encoding="utf-8"))

    vendor_docs = build_vendor_docs(model, version)
    dropped = set()
    print_doc = build_print_doc(model, per_nozzle_values, common_values, dropped)
    printer_doc = build_printer_doc(
        model, filter_values(printer_values, known_options("printer"), dropped))

    problems = check_docs(vendor_docs, vendor_schema, VENDOR_KINDS)
    problems += check_docs([print_doc, printer_doc], preset_schema, {
        k: "top-fragment" for k in ("printer", "print", "tool_print",
                                    "material", "filament")})

    write_stream(os.path.join(vend, "vendor.yaml"), vendor_docs)
    write_stream(os.path.join(vend, "preset-print-svzero.yaml"), [print_doc])
    write_stream(os.path.join(vend, "preset-printer-svzero.yaml"), [printer_doc])
    # The .idx keeps PS2's shape: min_slic3r_version, then a reverse
    # chronological changelog. Prusa's own alpha11 .idx still looks like this.
    with open(os.path.join(out_dir, "SVZero.idx"), "w", encoding="utf-8") as fh:
        fh.write("min_slic3r_version = 3.0.0-alpha11\n")
        for line in idx_lines:
            fh.write(line + "\n")
    return 4, problems, sorted(dropped)
