# Changelog

## Unreleased — walls, seams and square corner velocity

- **Arachne is set explicitly in all three slicers.** Before this, PrusaSlicer
  and OrcaSlicer used it only as their default, and SuperSlicer printed classic walls.
- **Scarf seams in PrusaSlicer** on outer contours, matching what OrcaSlicer
  already shipped. SuperSlicer has no scarf seam option.
- **Square corner velocity per tier, sent explicitly.**
  - Values: Fine 3, Optimal 4, Standard 5, Strength 5, Draft 9 mm/s.
  - PrusaSlicer and SuperSlicer name the value in the print preset's Notes, and
    the start G-code sends it after `START_PRINT`.
  - OrcaSlicer carries it in its jerk fields. Its machine jerk ceiling is raised
    to 9, because Orca clamps jerk to that ceiling.
  - `END_PRINT` and `CANCEL_PRINT` restore `printer.cfg`'s value.
  - Previously the per-tier value was declared but never sent, so every job ran
    at `printer.cfg`'s 5.
- **Standard's square corner velocity is now 5** (it was declared as 7), which
  is what Sovol's stock profile uses. Optimal moves to 4 to stay between Fine and Standard.

## Unreleased — machine mode in the handshake

- **The handshake carries the machine mode:** `SVZERO_REQUIRE VERSION=… MODE=standard`.
  - A mode mismatch refuses the job before any heater is commanded.
  - A version mismatch still only warns.
  - Jobs from older profiles name no mode and run as standard.
- **Outside standard mode**, a job that never identifies itself is refused at
  `START_PRINT`.
- **The pack ships only the standard mode.** This prepares for modes selected
  by a config include and a Klipper restart.
- `CANCEL_PRINT` clears a pending handshake, so a job cancelled during heat-up
  cannot vouch for the next one.
- Tests check that every slicer bundle emits the same handshake line. The macro
  checker now asserts refusals.

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
