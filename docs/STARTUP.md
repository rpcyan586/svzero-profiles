# Startup sequence

The released OrcaSlicer, PrusaSlicer and SuperSlicer profiles share startup
work with the installed printer macros. Reading `START_PRINT` alone omits the
early temperature commands and the final hotend heat/purge.

## Installation levels

| Installed components | Startup behavior |
|---|---|
| Profiles on stock Sovol 1.3.7 or 1.4.x | Slicer bed/nozzle waits plus stock cleaning, calibration and mesh. Missing optional commands warn and continue. The minimum skirt provides fallback priming. |
| Profiles plus the firmware-matched macro pack | The sequence below, including material-aware cleaning, native rear-sensor chamber wait and positioned purge. |
| Pack plus optional host helpers | `chamber_preheat` can qualify the unheated, stirred nozzle as an air observer and provide prediction/optional boost. Spool checks and camera tuning are separate optional integrations. |

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
| `START_PRINT` | Run optional camera tuning, recheck the bed target with `M190`, clear the G-code Z offset and invoke firmware-specific Z calibration. Rehome Z and calibrate the bed mesh. Heater targets remain active through this mesh sequence. |
| `START_PRINT` | Release any remaining chamber-helper bed carry, park over the pad and return to the slicer. |
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

## Sources

- Generated slicer entry points: [Orca source](../tools/start_gcode.py),
  [PrusaSlicer bundle](../bundles/SVZero_PrusaSlicer.ini),
  [SuperSlicer bundle](../bundles/SVZero_SuperSlicer.ini).
- Macro implementations: [start](../klipper/config/start_print.cfg),
  [cleaner](../klipper/config/nozzle_brush.cfg),
  [purge](../klipper/config/purge_line.cfg),
  [chamber](../klipper/config/chamber_fan.cfg).
- Firmware includes: [1.3.7](../klipper/config/svzero-1.3.7.cfg),
  [1.4.x](../klipper/config/svzero-1.4.x.cfg).
