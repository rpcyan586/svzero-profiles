# Vendored from PrusaSlicer 3.0.0-alpha11

`preset-schema.json` and `vendor-schema.json` are copied verbatim from
[`prusa3d/PrusaSlicer`](https://github.com/prusa3d/PrusaSlicer) at tag
`version_3.0.0-alpha11` (published 2026-09-01), path `specs/presets/`.
PrusaSlicer is AGPLv3; these are its schema files, unmodified.

They are here for two reasons:

1. **`tools/generate.py` validates `source/presets.json` against
   `preset-schema.json` on every run.** Our master is authored in PrusaSlicer
   3.0's own fragment shape, so the schema is the contract, not a resemblance.
2. **Schema churn is the cheap trigger.** Two small files, diffable against the
   next tag. See `docs/PIPELINE.md` § New triggers.

Refresh with:

    curl -sSO https://raw.githubusercontent.com/prusa3d/PrusaSlicer/<tag>/specs/presets/preset-schema.json

## `ps3-known-options.json`

Not a Prusa file. Extracted 2026-09-05 from PrusaSlicer's own shipped presets at
the same tag — `resources/presets/prusa-research-fff/PrusaResearch/`, files
`preset-print-coreone.yaml`, `preset-print-common.yaml` and
`preset-printer-coreone.yaml`.

**Empirical and partial**: it is what Prusa happen to *set* on those machines,
not the option list. `prusa-slicer --help-fff` is what makes
`../ps-known-options.json` authoritative for 2.9.6, and there is no equivalent
here because PrusaSlicer 3 is not installed — and could not load a third-party
bundle in alpha11 regardless.

`emit_ps3.py` therefore unions it with the authoritative PS2 list rather than
trusting it alone. On its own it stripped `resolution`, `raft_layers`,
`xy_size_compensation` and `support_material_auto`, all valid options that Prusa
simply leave at their defaults. Replace this file with a real option dump as soon
as PrusaSlicer 3 can be installed and asked.
