# Changelog

## Unreleased — acceleration cap holds in SuperSlicer

- **`install-presets.py --accel-cap` now caps SuperSlicer's percentage
  accelerations too.** It used to skip them, and some shares are above 100%: under
  a 5000 cap, Fine's infill still asked for 6000, and Optimal's and Strength's for
  5750. Each percentage is now resolved against the capped values and written as
  the cap only where it would exceed it.

## Unreleased — re-anchored acceleration ladder

- **The 0.4 tiers keep their ramp, now anchored on outside references** (mm/s²):

  | Tier | Default | Inner | Outer | Solid | Infill | Top | Bridge | First layer | Anchor |
  |---|---|---|---|---|---|---|---|---|---|
  | Fine | 3,000 | 2,500 | 1,500 | 4,000 | 6,000 | 2,000 | 1,500 | 500 | Prusa Core One STRUCTURAL |
  | Optimal | 10,000 | 6,000 | 5,000 | 10,000 | 11,500 | 3,000 | 5,000 | 500 | The Zero's measured resonance limits |
  | Standard | 40,000 | 12,000 | 10,000 | 20,000 | 40,000 | 5,000 | 20,000 | 1,000 | Sovol stock, exactly |
  | Draft | 40,000 | 13,800 | 11,500 | 20,000 | 40,000 | 5,000 | 20,000 | 1,000 | Just above stock |

  - Strength uses Optimal's values.
  - SuperSlicer keeps them as percentages of each other; PrusaSlicer and
    OrcaSlicer receive the same numbers.
- **Nozzles below 0.4 now use every acceleration Sovol's SV08 0.2 profile
  sets:** default 20,000, walls 12,000 / 8,000, infill 20,000, top 12,000,
  bridges 8,000, first layer 3,000.

## Unreleased — 0.25 and 0.3 nozzles, small-nozzle rules

- **New nozzle sizes: 0.25 and 0.3 mm**, each with Fine, Optimal and Standard.
  - Layers follow the 0.2 ladder (25 / 50 / 75% of the bore), rounded to whole
    Z steps: 0.06 / 0.12 / 0.18 on the 0.25, and 0.08 / 0.16 / 0.22 on the 0.3.
- **Every nozzle below 0.4 gets the same three rules:**
  - a first layer at 80% of the nozzle (0.16 / 0.20 / 0.24 mm)
  - 3 walls
  - the Sovol SV08 0.2 speed and acceleration reference: inner walls capped at
    200 mm/s, accelerations 20,000 / 12,000 / 8,000 mm/s²
- A first layer or wall count set directly on a preset still wins, and is
  checked like any other value.

## Unreleased — accelerations honoured in every slicer

- **PrusaSlicer and OrcaSlicer now request the accelerations the master asks for.**
  - The master is written in SuperSlicer's terms, where an acceleration
    percentage is measured against a neighbour: outer wall against inner wall,
    infill and top surfaces against solid infill, internal bridges against
    bridges.
  - The PrusaSlicer and OrcaSlicer outputs read every percentage against the
    default instead. They ran outer walls about 2.9× higher and infill and top
    surfaces 2× higher than SuperSlicer from the same profile.
  - Standard's outer wall now comes out at 9,960 mm/s² in all three, which is
    stock's 10,000 (it was 33,200 in PrusaSlicer and OrcaSlicer).
  - Each slicer keeps its own features; only the requested values change.

## Unreleased — 0.2 nozzle anchor, internal bridges, minimum layer time

- **The 0.2 nozzle now runs Sovol SV08's 0.2 wall speed and accelerations.**
  - Inner walls 200 mm/s; outer walls 166 with our 83% ratio.
  - Accelerations: 20,000 default, 12,000 inner walls, 8,000 outer walls.
  - This replaces the 500 mm/s motion cap and the 26k–40k tier accelerations.
    Every other speed keeps its proportion.
- **Internal bridges at 150% of bridge speed in SuperSlicer and OrcaSlicer.**
  - SuperSlicer previously got 400% (stock's 200/50 ratio), which asked for
    twice the tier's base speed and was clamped by the filament flow limit:
    668.5 mm/s on a 0.2 nozzle.
  - Orca, never given the setting, ran its default 150%.
- **Minimum layer time is at least 8 s for every filament,** after Prusa's Core
  One PLA. PETG keeps its 10 s.


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
