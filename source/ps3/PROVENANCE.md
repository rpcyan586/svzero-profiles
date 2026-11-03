# PrusaSlicer 3.0 schemas and data model

## `preset-schema.json`, `vendor-schema.json`

Copied verbatim from [`prusa3d/PrusaSlicer`](https://github.com/prusa3d/PrusaSlicer)
at tag `version_3.0.0-alpha11` (published 2026-09-01), path `specs/presets/`.
PrusaSlicer is AGPLv3; these are its schema files, unmodified. Both are
blob-identical at `version_3.0.0-alpha12` and `version_3.0.0-alpha13`
(checked 2026-11-02), so they are current.

They are here for two reasons:

1. **`tools/generate.py` validates `source/presets.json` against
   `preset-schema.json` on every run.** Our master is authored in PrusaSlicer
   3.0's own fragment shape, so the schema is the contract, not a resemblance.
2. **Schema churn is the cheap trigger.** Two small files, diffable against the
   next tag. See `docs/PIPELINE.md` § New triggers.

Refresh with:

    curl -sSO https://raw.githubusercontent.com/prusa3d/PrusaSlicer/<tag>/specs/presets/preset-schema.json

They are looser than the loader in one place and stricter in another.
`vendor-schema.json` makes feeder `model` optional and the loader requires it;
it gives `printer_config` no `features`, and Prusa's own alpha13 bundle sets
them (`xl-2t` and three more). `tools/emit_ps3.py` lists each such difference
where it writes the key.

## `config-schema.json`

The application's own data model: every option, by preset kind, with its type,
enum values, limits, default and the locations that may override it. Written
by `slic3r-app-launcher --export-config-schema` from the flathub-beta flatpak
`com.prusa3d.PrusaSlicer//beta`, Release 3.0.0-alpha13 (`c17dae9a40df`), on
2026-11-03, and committed byte for byte as exported. It replaced alpha11's
export (2026-09-05). Against alpha12's export it adds three options
(`used_filament_compensation`, `wipe_tower_max_purge_speed`,
`wipe_tower_deretraction_spread_ratio`), raises `layer_height`'s minimum to 0.01,
and adds the print-mode override locations `MacroOverrides` and
`MacroSlotOverrides` to 266 options.

Refresh with:

    flatpak run --branch=beta --command=slic3r-app-launcher com.prusa3d.PrusaSlicer \
        --export-config-schema source/ps3/config-schema.json

## `ps3-known-options.json`

Not a Prusa file. The option and override names of each preset kind, derived
from `config-schema.json`: `items` are the names in a kind's `items`, in order,
and `overrides` the names in its `overrides`. Regenerate it whenever
`config-schema.json` changes; the derivation reproduces the alpha11 file
exactly from alpha11's export.
