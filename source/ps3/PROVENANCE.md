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
