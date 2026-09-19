# Installation and optional upgrades

**Start with the slicer profiles. No printer changes or Python modules are
required for basic printing on stock Sovol Zero 1.3.7 or 1.4.x.** Here “stock”
means Sovol's shipped Klipper configuration and macros, including `START_PRINT`
and `END_PRINT`; this is not a universal profile for a bare upstream Klipper
installation or another printer.

## What each installation gives you

The same slicer profiles serve all three levels. There is no separate Stock or
Macro pack preset to maintain.

| Install | What you gain | What happens without it |
|---|---|---|
| Slicer profiles only | Nozzle-scaled widths and tiers, material settings, configured supports, bed/nozzle waits and a priming skirt | Stock Sovol cleaning, calibration, mesh and end-of-print behavior. No pack chamber soak, spool check, positioned purge or controlled cooldown |
| Firmware-matched `.cfg` macro pack | Chamber exhaust regulation, a basic rear-sensor chamber wait, material-aware wiping, positioned/flow-limited purge, corrected retract bookkeeping, safer cancellation and a bed cooldown ramp | The profiles still print using the stock path above |
| `chamber_preheat.py` + `moonraker.py`, enabled by `chamber_preheat.cfg` | Qualifies the stirred, unheated nozzle as a chamber-air observer; can use recent temperature history and predict readiness. Adds optional bed boost and bounded early handover | `M191` waits on the rear chamber sensor using native `TEMPERATURE_WAIT`. No nozzle qualification, boost or early handover |
| `spool_guard.py` + `moonraker.py`, enabled by `spool_guard.cfg` | Checks the active Spoolman spool's remaining mass and material before heating; supplies material information to other macros | No automatic spool validation. A missing Spoolman integration is skipped with a notice |
| `fan_duty.py` + `exhaust_duty.cfg` | Adds commanded exhaust duty and normalized measured RPM traces to the temperature chart | Fan regulation and tachometer checks still work; only the extra chart traces are absent |
| `camera_tune.py` + `moonraker.py`, enabled by `camera_tune.cfg`, plus the separately configured local camera service and lighting macros | Optional bounded exposure/lighting tuning during startup | Printing continues with existing camera settings; this is not a basic profile requirement |

The macro pack changes printer behavior, not MCU firmware. None of these
steps require a firmware flash, OTA or Klipper update.

## 1. Import the slicer profiles

- **OrcaSlicer:** install the generated vendor bundle under `bundles/orca-vendor`.
- **PrusaSlicer / SuperSlicer:** File → Import → Import Config Bundle, choosing
  the corresponding generated INI under `bundles/`.
- Select the nozzle that is physically installed. The pack covers
  0.2/0.4/0.5/0.6/0.8/1.0/1.2/1.4 mm; a preset does not establish that a
  particular replacement nozzle fits or has been calibrated.
- Add your own printer connection. PrusaSlicer/SuperSlicer use `moonraker`;
  Orca uses `octoprint` against Moonraker. Public profiles contain no host address.
- If needed, select `svzero_bed.stl` and `svzero_bed.svg` for the schematic bed preview.

Public startup sets the bed target, makes the optional pack calls, waits for the
bed with `M190` **before** `START_PRINT`, then waits for nozzle temperature before
purging/printing. The early bed wait is deliberate: stock `START_PRINT` does not
promise the replacement macro's settle-before-calibration sequence. It also
means public profiles do not depend on overlapping the nozzle wipe with the
bed's final settling time.

**Public profiles retain `PURGE_LINE`.** On stock Klipper an unknown ordinary
command produces a console warning and execution continues. A minimum one-loop
skirt, extended to extrude at least **8 mm of filament** (not 8 mm of path),
provides baseline priming when that macro is absent. With the pack installed,
you get the positioned purge **and** the skirt. Skirts occupy build-plate space;
leave room in the arrangement and inspect the preview. Disabling them on stock
firmware removes this fallback unless you provide another priming sequence.

The other optional calls (`SVZERO_REQUIRE`, `_PREFLIGHT`, `M141`/`M191`,
`SET_CLEAN_TEMP`, `COOLDOWN_ARM`) likewise do not require stub macros on stock
Sovol firmware. Expect several unknown-command messages, not one combined
warning. Unknown commands being harmless does **not** make errors in an
installed macro harmless; nor does it apply to commands such as
`SET_GCODE_VARIABLE` naming a nonexistent target.

For an isolated trial, use the public installer with a new data directory;
see [the quick start](README.md#try-the-profiles). The public installer
keeps skirts enabled. Configure printer connections in your slicer.

## 2. Optional macro pack, without Python

Back up the current configuration. Copy the pack's `.cfg` files to
`~/printer_data/config/svzero/`, retaining their relative paths. Do not copy
someone else's `svzero-personal.cfg`; personal overrides are no longer loaded
automatically.

Near the bottom of `printer.cfg`, after Sovol's `[include Macro.cfg]` **and after
all stock fan/sensor definitions**, add exactly one firmware-matched include:

```ini
# Stock firmware 1.3.7:
[include svzero/svzero-1.3.7.cfg]
```

or:

```ini
# Stock firmware 1.4.x (checked against 1.4.7):
[include svzero/svzero-1.4.x.cfg]
```

These include the native macros and hardware configuration only. No added
Python modules are required. The 1.3.7-only pressure-probe sampling override
lives in `probe_pressure_1.3.7.cfg`; the 1.4.x include must not load it.
The 1.4.x include instead selects `RUN_PROBE_VIR_CONTACT` at Sovol's stock
X30/Y30 plate-contact location; 1.3.7 keeps its load-cell command/location.

### Required 1.4.x edit

Back up and comment out the **entire** stock `[temperature_fan exhaust_fan]`
section, including all its options. The pack replaces it with a generic exhaust
fan on PB0 and a chamber sensor on PC4. Keeping both definitions prevents
Klipper from starting because the pins are claimed twice. Do not remove
`[fan_generic fan3]`: on 1.4.x that is different hardware.

No such removal is needed on 1.3.7: the pack merges overrides into the existing
`fan_generic fan3` and uses the existing `temperature_sensor chamber_temp`.

Restart only while the printer is idle; restarting aborts a print. Validate changed macros
with `tools/check-macros.py` in the target's Jinja environment before deployment.

Review the brush geometry before using the replacement wipe. `nozzle_brush.cfg`
contains pad bounds measured on the author's machine; use `BRUSH_STATUS` and the
file's documented `BRUSH_TEACH` / dry-run procedure to check your own machine.
Adding the pack is not a substitute for checking physical clearances.

### Native fallback and its limits

Without `[chamber_preheat]`, `M191` parks/stirs and waits for the requested
minimum on the **rear chamber thermistor**. The fan command is flushed before
the blocking wait. The bed stays at the print target; there is no boost or early
handover and the macro does not label the nozzle as a qualified air sensor.
The rear sensor is a less representative observation of air at the part than
the qualified nozzle method. An unattainable chamber minimum still needs to be
changed or the job cancelled; missing Python is not permission to ignore the
requested minimum.

The native cooldown ramps the **bed target** down; the public chamber-rate
settings are zero on a heaterless Zero. Exhaust assist is available; curtain-fan
cooldown assist is **off by default**. These policies and rates are starting
points, not measured guarantees against warping. `COOLDOWN_CONFIG ENABLE=0`
disables the ramp; `COOLDOWN_STATUS` reports it. `COOLDOWN_ABORT` stops the ramp
without turning heaters off; use `CANCEL_PRINT` for shutdown and cancellation.

With another slicer's profile, missing material information falls back to the
spool, remembered temperature or configured default where supported. That is
not a guarantee that arbitrary start G-code is compatible: it still needs bed
and nozzle waits and sufficient priming, especially with the pack's wipe and
end-retract bookkeeping.

## 3. Optional Python enhancements

Install only the helpers you want. For adaptive preheat **and** Spoolman:

```sh
cp ~/printer_data/config/svzero/klipper/chamber_preheat.py ~/klipper/klippy/extras/
cp ~/printer_data/config/svzero/klipper/spool_guard.py ~/klipper/klippy/extras/
cp ~/printer_data/config/svzero/klipper/moonraker.py ~/klipper/klippy/extras/
```

Then add, **after** the firmware-matched include:

```ini
[include svzero/svzero-python.cfg]
```

For just one feature, include `svzero/chamber_preheat.cfg` or
`svzero/spool_guard.cfg` instead, and copy that feature's module plus
`moonraker.py`. Do not also include `svzero-python.cfg`. Apply your own personal
settings last, after any sections they override.

Once the files and includes are ready and the printer is demonstrably idle,
restart the **Klipper service** to load new Python code. `RESTART` and
`FIRMWARE_RESTART` do not reload an already-imported module. Check that no job is active immediately before restarting the service.

**Klipper cannot conditionally ignore a missing configured Python module.**
If you enable `[chamber_preheat]` or `[spool_guard]` without installing its
module/dependencies, configuration fails before macros can run. The fallback
works by leaving those optional includes disabled, not by disguising a broken
installation. To remove Python, remove its include first and restart while idle.
The native macro path then takes over.

### What Python improves, and what still fails deliberately

- **Adaptive preheat:** qualifies a previously hot nozzle before trusting it as
  an air sensor. Moonraker temperature history can shorten that qualification;
  if history is absent or unavailable it takes fresh samples instead. Bed boost
  is enabled explicitly with `M191 ... BOOST=1`, bounded by the configured bed
  ceiling (120 °C in the public config). Standard generated calls do not enable
  boost. Timeouts, invalid parameters or a failed qualification remain errors;
  the macro must not pretend the requested chamber condition was reached.
- **Spool guard:** a 404 specifically on Moonraker's Spoolman endpoint means
  “not configured” and skips the check with one notice per session. A configured
  integration with no active spool, missing metadata, insufficient filament or
  a material mismatch refuses before heating. Server failures/timeouts also
  refuse. The configured one-shot retry override is described in
  `spool_guard.cfg`; it is an explicit override, not automatic success.
- **Fan charts:** copy `fan_duty.py` before including `exhaust_duty.cfg`. Select
  the correct fan name for your firmware in that file. The module only adds
  telemetry; it is not required for chamber control.
- **Camera tuning:** the local camera service and lighting macros are separate
  dependencies, not included by the basic macro pack. Service failure is caught
  and printing continues with current/default camera settings. Configure this
  only if that camera/lighting setup exists on your machine.

## Removing or downgrading

- **Python → macros only:** remove `svzero-python.cfg` (or the individual
  feature includes) and any personal sections that declare those extras.
- **Macros → stock:** remove the firmware-matched include and all optional
  feature/personal includes. On **1.4.x restore the complete original
  `[temperature_fan exhaust_fan]` section** from the backup. Merely deleting the
  include would leave stock exhaust control missing.
- Restart only while idle. Copied files can remain unused on disk. Restore any
  pre-existing extras you replaced from your backup if they were used elsewhere.
- Keep the public skirt settings when returning to stock. Re-slice old projects
  that retained earlier startup or skirt settings.

## Validation boundary

The 2026-09-18 compatibility repair was checked offline against preserved Sovol
1.3.7 and 1.4.7 sources: unknown-command dispatch, include dependencies, macro
rendering with Jinja 2.11.3, generated bundles, actual slicing, and optional
service failure tests. This is not a fresh physical print test on untouched
stock hardware. No live printer configuration or Python module was changed by
that review.
