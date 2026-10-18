# Startup sequence

The released OrcaSlicer, PrusaSlicer and SuperSlicer profiles share startup
work with the installed printer macros. Reading `START_PRINT` alone omits the
early temperature commands and the final hotend heat/purge.

## Installation levels

| Installed components | Startup behavior |
|---|---|
| Profiles on stock Sovol 1.3.7 or 1.4.x | Slicer bed/nozzle waits plus stock cleaning, calibration and mesh. Missing optional commands warn and continue. The minimum skirt provides fallback priming. |
| Profiles plus the firmware-matched macro pack | The sequence below, including material-aware cleaning, native rear-sensor chamber wait and positioned purge. |
| Pack plus optional host helpers | `chamber_preheat` can qualify the unheated, stirred nozzle as an air observer, predict readiness, and run bed boost, which is on by default. Spool checks and camera tuning are separate optional integrations. |

See [Installation](../INSTALL.md) for setup, brush-geometry checks and fallback
limits. These are source behaviors, not a statement about a particular printer's
installed version or measured performance.

## Profiles plus macro pack

| Owner | Step |
|---|---|
| Slicer | Identify the profile version and invoke preflight before heating. |
| Slicer / chamber macros | Set the bed target and apply the slicer's chamber policy. Native `M191 S` waits for a rear-sensor minimum; `M141` sets a separate cooling ceiling. The optional Python helper uses its qualified observation/prediction instead. |
| Slicer | `SET_CLEAN_TEMP MATERIAL=…` selects the cleaning temperature **and starts nozzle heating**; arm material-dependent cooldown. |
| Slicer | Wait for the bed with `M190`, then call `START_PRINT`. This early wait preserves compatibility with stock Sovol macros. |
| `START_PRINT` → `CLEAN_NOZZLE` | Home if needed, park over the pad and wait for cleaning temperature. Retract, scrub through cooldown using the taught pad height, wait at probe temperature over the pad, perform the configured final probe and lift. There is no initial brush-datum tap. |
| `START_PRINT` | Run optional camera tuning, recheck the bed target with `M190`, clear the G-code Z offset and invoke firmware-specific Z calibration, then record the nozzle temperature that Z reference was taken at. Rehome Z and calibrate the bed mesh. Heater targets remain active through this mesh sequence. |
| `START_PRINT` | Release any remaining chamber-helper bed carry, park over the pad and return to the slicer. |
| Slicer | PrusaSlicer/SuperSlicer send the print tier's square corner velocity (`SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY`, from the print preset's Notes); OrcaSlicer sends it with its first printed feature, from its jerk fields. `END_PRINT` and `CANCEL_PRINT` restore `printer.cfg`'s value. |
| Slicer | All three send the print tier's X/Y stepper current, `SVZERO_XY_CURRENT CURRENT=2.4` on Fine to Strength, from the print preset's Notes. The pack only lowers the configured current; Draft sends nothing and runs `printer.cfg`'s. `END_PRINT` and `CANCEL_PRINT` restore it. |
| Slicer | All three send `SVZERO_NOZZLE_GROWTH TEMP=` with the first-layer temperature. It offsets Z for the nozzle being hotter than at the Z reference, with the pack's provisional coefficient unless the machine has stored its own or 0. See [below](#nozzle-growth-compensation). |
| Slicer | Lift to Z5, heat/wait for printing temperature, then call `PURGE_LINE` with nozzle/filament dimensions, flow limit and skirt clearance. |
| `PURGE_LINE` | Place rings or an edge line and return recorded cleaning/end-retract debt at the deposition position before purging. |
| Slicer | Continue the print; the public skirt remains enabled even with the pack installed. |

If no purge geometry fits, `PURGE_LINE` reports that no purge was placed and
returns the debt without a placed purge. The skirt remains a subsequent priming
opportunity. `_BRUSH_PRIME` is a compatibility stub; it no longer primes.

The early `M190` means the released path does not rely on overlapping cleaning
with the bed's final settling time. Removing that wait requires reviewing both
stock compatibility and the cleaner's final probe. Optional chamber prediction
and early handover do not turn the nozzle into an air observer once nozzle
heating has started.

Parking before the cleaner's `M109` controls ooze during that wait. It does not
mean all heating begins over the pad: `SET_CLEAN_TEMP` already sent `M104`.

## Firmware-specific reference work

The 1.3.7 pack uses `RUN_PROBE_PRESSURE` for the cleaner's final probe.
`START_PRINT` then calls `Z_OFFSET_CALIBRATION METHOD=force_overlay` when the
`probe_pressure` object exists. In the preserved vendor implementation, the
non-default method bypasses the already-calibrated skip and performs load-cell
reference work and eddy calibration, not just a scalar G-code offset update.

The 1.4.x pack selects `RUN_PROBE_VIR_CONTACT` at X30/Y30 for the cleaner.
Without `probe_pressure`, `START_PRINT` leaves the calibrator's method at its
default, allowing it to skip work when eddy calibration data already exist.
This is a firmware/configuration distinction; a physical load cell may still
be fitted. Both paths pass the brush's probe temperature explicitly, currently
150°C, rather than silently taking the calibrator's 130°C default.

## Nozzle growth compensation

A hotter nozzle reaches further down. Every print's Z reference is taken with
the nozzle at one temperature and the first layer is printed at another, so the
first layer sits closer to the plate than calibrated. The pack models that as
linear in the temperature difference:

    dz = k × (first-layer temperature − reference temperature)

`SVZERO_NOZZLE_GROWTH TEMP=<first-layer temperature>` applies
`SET_GCODE_OFFSET Z_ADJUST=dz MOVE=1`, where + is farther from the plate, and
reports k, both temperatures and the result. The source is
[nozzle_growth.cfg](../klipper/config/nozzle_growth.cfg).

**On as published.** The pack's k, `variable_mm_per_c` in `_NOZZLE_GROWTH`,
was measured on the operator's Zero2 and is provisional until a cleaner run
finishes. On the 1.3.7 tare-tap path, against the 150 °C probe temperature, it
moves the first layer farther from the plate by:

| First layer | Presets | Offset |
|---|---|---|
| 230 °C | PLA - Brass | +0.07 mm |
| 245 °C | PLA - Steel, PLA Silk | +0.09 mm |
| 250 °C | TPU | +0.09 mm |
| 260 °C | PCTG | +0.10 mm |
| 265 °C | PETG | +0.10 mm |
| 280 °C | ABS, PC | +0.12 mm |

k belongs to one machine's nozzle, heater block and probe, so on any other Zero
it is an estimate. Turn it off on a machine, or use that machine's own k, with
a saved variable:

    SAVE_VARIABLE VARIABLE=nozzle_growth_mm_per_c VALUE=0

**What has been measured.** On one Zero (1.3.7, 0.4 nozzle) the tare-tap read
40–45 µm higher from 150 to 190 °C while the eddy, which sees the plate and not
the nozzle, rose 10 µm: net, about 0.75–0.9 µm/°C. That is one clean step.
Above 190 °C plastic on the tip spoiled the run. A later run on the same
machine, 150 to 280 °C and back, fit 0.8–1.1 µm/°C with residue on the tip
still in play. At that rate an ABS first layer at 280 °C against a 150 °C
reference sits about 0.10–0.14 mm closer than calibrated.

| Firmware | Z reference | Reference temperature |
|---|---|---|
| 1.3.7, tare-tap (`probe_pressure`) | `Z_OFFSET_CALIBRATION METHOD=force_overlay` taps every print; `G28 Z` and the mesh follow | `_BRUSH` `probe_temp`, 150 °C, every job |
| 1.4.x, eddy only | `Z_OFFSET_CALIBRATION` returns at once when eddy data exist, so the reference is the stored calibration | Only what `SVZERO_NOZZLE_GROWTH_EDDY_REF` recorded. With no eddy data, the calibration runs during `START_PRINT` at `probe_temp` |

When the reference temperature is unknown, nothing is applied and the console
says why.

### Measuring k

1. Unload the filament and clean the tip. Plastic on the tip is what spoiled
   the measurement above.
2. Heat the bed to a usual printing temperature and let it settle until repeat
   taps at one nozzle temperature agree.
3. Home once, and do not home again: every reading must be in one Z frame.
4. Step the nozzle through several temperatures, for example 150, 190, 230 and
   270 °C, then back down. At each, wait a few minutes after it arrives, tap one
   fixed point with the contact probe several times and keep the median Z. The
   point and probe `CLEAN_NOZZLE` uses for its final tap will do.
5. k is the slope of tap Z against nozzle temperature, in mm per °C, net of
   any movement of the plate itself over the same time. The way down should
   repeat the way up; if it does not, something besides the nozzle moved.

Store it in place of the pack's, or store 0 to turn the compensation off:

    SAVE_VARIABLE VARIABLE=nozzle_growth_mm_per_c VALUE=0.0008

The saved variable takes precedence over the macro's default, so it survives
pack updates. On 1.4.x, also record the temperature of a deliberate eddy
calibration, and record it again after every calibration:

    Z_OFFSET_CALIBRATION METHOD=force BED_TEMP=100 EXTRUDER_TEMP=150
    SVZERO_NOZZLE_GROWTH_EDDY_REF TEMP=150
    SAVE_CONFIG

`SAVE_CONFIG` keeps the calibration and restarts Klipper, so run it only while
idle. `SVZERO_NOZZLE_GROWTH_EDDY_REF` with no argument reports the record, and
`CLEAR=1` forgets it.

### Limits

- **Per-material offsets become residuals.** Each filament's
  `SET_GCODE_OFFSET Z_ADJUST` runs after this and adds to it. An offset tuned
  without the compensation, with an earlier pack or with k saved as 0, already
  holds the nozzle's growth for that material and is now counted twice: return
  those offsets to 0 and tune them again.
- **Eddy coil drift is not compensated.** The coil sits beside the nozzle. In
  the same run the eddy rose 56 µm while the nozzle heated from 48 to 150 °C,
  which may be the coil warming rather than the plate moving. Stock
  `printer.cfg` configures no coil temperature sensor for Klipper's drift
  compensation. It is a second term on the 1.4.x path, not yet separated or
  measured, and not modelled. Nor has 1.4.x's virtual contact, itself an eddy
  measurement, been checked as an instrument for step 4.
- **Bounded.** An adjustment over 0.25 mm is refused with an error line, and
  nothing is applied. The hotend's whole range, 130 to 355 °C at 0.9 µm/°C, is
  0.20 mm, so a larger value is a mistyped k or a wrong reference rather than
  growth. `variable_max_adjust` in `_NOZZLE_GROWTH` sets the bound.
- **First layer only.** Later layers keep the first layer's offset; a 10 °C
  difference between them is 8 µm at 0.8 µm/°C. A second call in the same job
  applies only the difference from the first.

## Sources

- Generated slicer entry points: [Orca source](../tools/start_gcode.py),
  [PrusaSlicer bundle](../bundles/SVZero_PrusaSlicer.ini),
  [SuperSlicer bundle](../bundles/SVZero_SuperSlicer.ini).
- Macro implementations: [start](../klipper/config/start_print.cfg),
  [cleaner](../klipper/config/nozzle_brush.cfg),
  [purge](../klipper/config/purge_line.cfg),
  [chamber](../klipper/config/chamber_fan.cfg),
  [nozzle growth](../klipper/config/nozzle_growth.cfg).
- Firmware includes: [1.3.7](../klipper/config/svzero-1.3.7.cfg),
  [1.4.x](../klipper/config/svzero-1.4.x.cfg).
