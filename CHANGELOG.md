# Changelog

## Unreleased

- Curtain fan (`fan2`) gets `off_below: 0.15`, so an auxiliary-fan request below
  its measured ~14% stall point switches it off instead of leaving it powered
  and stalled. The cooldown assist never asks for less than 0.20.
- Hotend fan tach: `tachometer_ppr: 2`. Stock says 1, so the UI showed twice the
  real speed, ~16,700 rpm at full; at 2 it reads 8,350-8,410 rpm against
  8,130-8,220 from the toolhead accelerometer. Display only.
- Exhaust fan defaults made conservative, on the curtain fan's rule of one point
  above the worst measured floor. `off_below: 0.15` on the exhaust section, so
  a request below it (a slicer `M106 P3` on 1.3.7, a UI slider) turns the fan
  off instead of stalling it powered. The chamber loop's known-good floor
  default goes from 0.13 to 0.15: one unit failed 13% in service and holds 14%.
  Adaptive floor trials (`ch_floor_autotune`) are now opt-in; they need
  `off_below: 0.00`. Saved values on an existing install are unaffected.

## 2.0.3-beta.1 — installation-only downloads

- Build only three slicer ZIPs; source and build tools remain in Git.
- Single INI imports for PrusaSlicer/SuperSlicer without duplicate loose presets.
- Three consolidated optional macro configs instead of the individual sources,
  with only the three runtime Python modules needed for preheat and Spoolman.
- Short installation guides and direct archive paths; no profile or macro
  settings changed. Existing published ZIPs retain their versioned contents.

## 2.0.2-beta.1 — installation and profile consistency

- Separate OrcaSlicer, PrusaSlicer and SuperSlicer ZIPs, each containing its own
  profiles and the optional macro pack; a complete source ZIP for development.
- One generated native macro config per firmware, with separate readable
  sources and a standard-library builder. Python enhancements remain opt-in.
- Included release builder, per-file inventory and reproducible ZIP checksums.
- Nozzle-specific layer bounds, with consistency tests across all eight sizes.
- Travel acceleration 5000 while retaining SuperSlicer's target-based deceleration.
- Derived motion values rounded to tenths, preserving geometry/calibration
  precision and native percentage relationships.

The 1.4.x stock exhaust section still needs to be disabled for the macro pack.
Physical commissioning and the existing upstream licensing question remain as
documented in INSTALL.md and NOTICE.

## 2.0.1-beta.1 — public repository candidate

- Standalone source, generator, pinned upstream inputs and automated checks.
- Eight nozzle sizes and 38 process presets, with regenerated slicer bundles.
- Public priming skirt and optional `PURGE_LINE`; explicit bed wait before stock
  startup. No separate stock/macro printer presets.
- Optional Python includes; native rear-sensor chamber wait without the helper.
- Correct firmware-specific brush probe selection and pressure-probe include.
- Missing Spoolman integration skips with a notice; configured failures refuse.
- Original schematic bed preview; no personal connections or tuning files.

This is a beta candidate, not a claim of completed physical stock validation.
See [validation](docs/VALIDATION.md) and [release procedure](docs/RELEASING.md).
