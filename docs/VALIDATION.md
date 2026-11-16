# Validation and known limitations

## Candidate 2.2.0

The offline suite passes **316 tests**, with macro rendering on Jinja 2.11.3
and a clean deterministic rebuild. PS vendor `2.2.0`, Orca `02.02.00.00`.
Profile behaviour changes: nozzle temperatures rebased to the PT1000 reading,
the settings OrcaSlicer 2.5.0 reads from the vendor's base files now stated for
every slicer, a per-tier Z current, and a higher square corner velocity at the
fine end. The macro pack reads the hotend as a PT1000, sets Sovol's mesh flag on
1.4.6+, adds `camera_auto`, and keeps the chamber loop running while anything
heats the chamber (see the [changelog](../CHANGELOG.md)).

- **Sliced.** The full matrix produced G-code in SuperSlicer 2.7.62,
  PrusaSlicer 2.9.7 and OrcaSlicer 2.5.1, with no refused key. It covers 30
  slices, every nozzle, tier, filament and test object at least once. Every
  row matched the baseline, which was re-recorded in this cycle under
  OrcaSlicer 2.5.1.
- **Shrinkage enlarges parts.** A 20 mm PLA cube comes out 20.060 mm in the
  G-code at 0.3% in PrusaSlicer 3, SuperSlicer and OrcaSlicer 2.5.0, and every
  other filament within 0.001 mm of its own figure.
- **The PT1000 reading on both Zeros.** Klipper reports `PT1000`, 10000 and
  344, with Sovol's pin and PID unchanged and no config warnings. At room
  temperature the 1.4.7 Zero's extruder read 24.0 °C against bed 23.8 and
  chamber 24.7, where Sovol's curve read 22.5.
- **A whole print on a 1.3.7 Zero on the PT1000 reading.** An ABS temperature
  tower at Standard printed to completion on 2026-11-08, on this release's
  1.3.7 macros less two additions that do nothing on that machine: the 1.4.6+
  mesh flag and the `camera_auto` hooks. `END_PRINT` put X and Y back at
  3.50 A and Z at 1.33 A, and the cooldown took over. The tower's first layer
  was set at 280 °C, 2.1.2's ABS value, so the rebased temperatures themselves
  have not printed.
- **Called live on that Zero.** `SVZERO_Z_CURRENT CURRENT=1.0` took the Z
  driver from 1.331 to 0.998 A. The mesh-flag lines, deployed there, render
  and skip, since 1.3.7 has no such flag. Both rendered under its own
  klippy-env (Jinja 2.11.3, Python 3.9.2) first.
- **On a 1.4.7 Zero, without motion.** Its loaded `BED_MESH_CALIBRATE` wrapper
  is Sovol's shipped one, and the flag reads False at runtime. Rendered in
  Klipper's environment, the wrapper homes X and Y and forces a calibration
  when the flag is False, and only meshes when it is True.
- **`camera_auto` live on that Zero's stock camera** under stock crowsnest: a
  start, end and start cycle in 4.8, 7.7 and 2.5 s; a desk lamp moved in and
  out between phases; the stored controls applied again after a crowsnest
  restart; 14–24% of one core during the end phase.
- **Not run on a printer:**
  - A print at the rebased temperatures. Nothing hot has been checked against a
    reference thermometer here; the evidence is pi.r's measurement inside a
    Zero's block, torch's on an SV08 MAX, and room temperature.
  - Any print on 1.4.x with this release, the mesh flag included. The
    maintainer's 1.4.7 Zero cannot move.
  - `camera_auto` in a print: its `_PREFLIGHT` and `END_PRINT` hooks, and the
    end phase's clip limit on a real part. The stored line with automatic
    white balance has not been through a restart.
  - The per-tier Z current inside a print, Precision at 1.8 A, and whether the
    currents hold each tier's accelerations without lost steps.
  - The raised square corner velocity, which needs an A/B print.
  - OrcaSlicer 2.5.0's stated support, overhang and fan settings, and its
    inward wipe.
  - The chamber loop staying on through a mid-print dip.
  - PrusaSlicer 3's menu install by hand. Its bundle is in Git, not a
    download.

## Candidate 2.1.2

The offline suite passes **218 tests**, with macro rendering on Jinja 2.11.3
and a clean deterministic rebuild. PS vendor `2.1.2`, Orca `02.01.02.00`.
Profile behaviour changes: the filament start G-code now runs in PrusaSlicer
and SuperSlicer, SuperSlicer's tool name, scarf seams, upstream OrcaSlicer's
Brass and Steel temperatures and flow, PCTG, and the per-machine Z line in
OrcaSlicer's start G-code. The macro pack adds nozzle growth compensation,
the runout check and the nozzle-clean dwell (see the [changelog](../CHANGELOG.md)).

- **Sliced.** The full matrix produced G-code in SuperSlicer 2.7.62,
  PrusaSlicer 2.9.6 and OrcaSlicer 2.4.2, with no refused key. It covers 30
  slices, every nozzle, tier, filament and test object at least once. Every
  row matched the baseline recorded for this release.
- **Printed on a 1.3.7 Zero.** Single-layer ABS squares sliced with this
  release's PrusaSlicer presets (0.4 Standard, SV Zero ABS) printed through
  the 2.1.2 start G-code on 2.1.1-era macros. `SVZERO_NOZZLE_GROWTH` and
  `SVZERO_XY_CURRENT` were unknown there, and Klipper stepped over them as
  documented.
- **Nozzle growth measured on that machine** (see the
  [startup guide](STARTUP.md#nozzle-growth-compensation)):
  - Tap pairs beside the tare-tap's sensor gave 1.13 µm/°C.
  - Squares printed with lifts for k = 0.67 and 1.0 came out over-squished.
    The square for 1.3 was flat at 0.273 mm for a 0.25 mm layer.
  - The published k is 1.1.
- **Fixed on a 1.4.7 Zero.** The nozzle-clean dwell ended a "Probe triggered
  prior to movement" stop for an owner who applied it by hand.
- **Bed preview** shown in all three slicers.
- **Deployed to that 1.3.7 Zero and called live.** The macros rendered under
  its own klippy-env (Jinja 2.11.3, Python 3.9) first.
  - `START_PRINT` ran end to end: the nozzle clean with its dwell, then Sovol's
    `Z_OFFSET_CALIBRATION` with the tare-tap's median-of-7 sampling, then the
    mesh. It recorded the growth reference: 150 °C, tare-tap.
  - `SVZERO_NOZZLE_GROWTH TEMP=280` then applied `Z_ADJUST=0.143`. A second
    call applied nothing more.
  - With no reference it applies nothing and says why.
  - `SVZERO_XY_CURRENT` lowered X and Y to 2.4 A and refused 4.0 A.
  - `SVZERO_SHAPER_TYPICAL` applied its values.
  - `_PREFLIGHT` passed with filament at the sensor.
- **A whole print on that Zero.** A 3DBenchy in ABS, sliced with this
  release's OrcaSlicer presets (0.4 Draft, 0.25 mm layers), printed to
  completion in 46 minutes on the 2.1.2 macros, with no per-material Z offset:
  the spool check, the boosted M191 soak, `START_PRINT`, the lift
  (`Z_ADJUST=0.143`), the purge ring, `END_PRINT` and the cooldown's hand-off.
  Draft sends no X/Y current, so `END_PRINT`'s restore ran with nothing
  lowered.
- **A cancelled job on that Zero.** A file that only called
  `SVZERO_XY_CURRENT CURRENT=2.4` and dwelt was started and cancelled from
  Moonraker. X and Y read 2.41 A while it ran and 3.50 A after
  `CANCEL_PRINT`, which also stopped the running cooldown and skipped its lift
  and park on the unhomed machine.
- **Not run on a printer:**
  - `END_PRINT` restoring a lowered X/Y current, which needs a Fine to
    Strength job.
  - `_PREFLIGHT` refusing with the sensor empty.
  - `SVZERO_SHAPER_CALIBRATE`.
  - The eddy-only off path, which has run only in rendering.

## Candidate 2.1.1

The offline suite passes **195 tests**, with macro rendering on Jinja 2.11.3
and a clean deterministic rebuild. PS vendor `2.1.1`, Orca `02.01.01.00`.
Profile behaviour changes: accelerations, Draft's speeds, X/Y stepper current,
precision's corner velocity (see the [changelog](../CHANGELOG.md)).

- **Sliced, not printed.** Each 0.4 tier in SuperSlicer 2.7.62, PrusaSlicer
  2.9.6 and OrcaSlicer 2.4.2 emits the same peak acceleration, square corner
  velocity, X/Y current and handshake line in all three:

  | Tier | Peak, mm/s² | SCV | X/Y current sent |
  |---|---:|---:|---|
  | Precision (0.08) | 2,000 | 1 | none, the mode file's 1.4 A |
  | Fine | 4,000 | 3 | 2.4 A |
  | Optimal | 6,000 | 4 | 2.4 A |
  | Standard | 11,500 | 5 | 2.4 A |
  | Strength | 6,000 | 5 | 2.4 A |
  | Draft | 40,000 | 9 | none, printer.cfg's 3.5 A |

  The full matrix (30 slices, every nozzle, tier, filament and test object at
  least once) produced G-code in all three with no refused key, and every
  row's filament matched the 2.1.0 baseline.
- **Draft on the PLA cap.** A 120 mm box at 0.4/0.25 in PLA: OrcaSlicer runs
  the preset's outer wall 179.5, inner 216.2 and infill 216.2 mm/s, extruding
  20.99 mm³/s of infill. PrusaSlicer 2.9.6 and SuperSlicer 2.7.62 run the walls
  as asked and clamp infill to 211.9, which is 21 mm³/s over the bead without
  the 0.98 flow ratio. Across Draft's seven nozzles the infill asks 20.97-21.00
  in OrcaSlicer's model. `tests/test_draft_flow.py` holds infill on the cap and
  the walls under it.
- **Macros rendered on the printer's own interpreter.** The three firmware
  files and the precision mode file render under a 1.3.7 Zero's klippy-env
  (Jinja 2.11.3, Python 3.9): 419 scenarios, among them `SVZERO_XY_CURRENT`
  lowering, refusing a raise and finding no TMC5160 sections, and
  `SVZERO_SHAPER_CALIBRATE` refusing in precision mode and mid-print.
- **Not run on a printer:** `SVZERO_XY_CURRENT`, `SVZERO_SHAPER_CALIBRATE` and
  `SVZERO_SHAPER_TYPICAL` have not been deployed or called. No part has
  printed at the 2.1.1 accelerations or currents. Whether 2.4 A holds
  Standard's 11,500 mm/s² and Strength's 6,000 without lost steps, and 1.4 A
  precision's 2,000, is not measured. The nearest report is a 48 V Zero that
  lost steps at 2.4 A and 35,000 mm/s² and ran at 3 A; Draft, at 40,000, keeps
  printer.cfg's 3.5 A.
- **Accelerometer map on 1.3.7:** the pack sets `axes_map: x,z,y`, which Sovol
  ships in 1.3.8 and 1.4.x. Stock 1.3.7's `z,x,y` swaps the per-axis graphs.

## Candidate 2.1.0

The offline suite passes **182 tests**, with macro rendering on Jinja 2.11.3
and a clean deterministic rebuild. Unlike 2.0.3-beta.1, **profile behaviour
changes**: accelerations, walls, seams, square corner velocity, two new
nozzle sizes and filament shrinkage (see the [changelog](../CHANGELOG.md)). PS
vendor `2.1.0`, Orca `02.01.00.00`.

- **Sliced and measured** with `tools/slice-harness.py` on SuperSlicer 2.7.62,
  PrusaSlicer 2.9.6 and OrcaSlicer 2.4.2. The full matrix, every nozzle, tier,
  filament and test object at least once (30 slices), produced G-code in all
  three with no refused key. Its baseline was re-recorded for this release:
  the one row it held moved 11–17 %, and in SuperSlicer, putting back the
  2.0.x shrink, two walls, 62.5 % first layer and classic walls in turn
  returned exactly the old figure. The 0.4 tiers and a 0.25 Optimal emit the
  same per-feature accelerations, square corner velocity and handshake line in
  all three.
- **Filament shrinkage:** the direction was settled from OrcaSlicer's and
  PrusaSlicer's source and by slicing. In SuperSlicer, a 20 mm cube came out
  21.8 mm at 90 % and 17.8 mm at 110 %. In OrcaSlicer's GUI, an ABS part
  re-sliced at 99.2 % came out ×1.0165 in X and ×1.0163 in Y against 100.8 %,
  where ×1.0161 is expected. Run from the command line, OrcaSlicer and
  PrusaSlicer apply no XY shrinkage at all, so the slice harness cannot check
  it. An ABS part printed from OrcaSlicer with the corrected profile was
  measured by the operator against the same part from the 2.0.x values, and
  came out right.
- **Macro handshake called live** on a 1.3.7 Zero: `MODE=standard` accepted, a
  version mismatch warns and runs, a job naming another mode refused before
  heating, no `MODE` accepted as standard.
- **Precision mode:** sliced in all three slicers at 0.04 and 0.08 mm with
  identical per-feature speeds, peak acceleration 4000, outer walls 45.0 at
  0.04, `MODE=precision`, SCV 3 and no z-hop. On a 1.3.7 Zero the mode file was
  deployed and switched into and out of automatically: driver registers read
  back 64 microsteps and the chopper values, and both clamps held. One part was
  printed in precision mode from the pre-generator prototype profiles. The
  0.2, 0.25 and 0.3 precision printers were sliced in all three slicers (0.2 at
  0.04, 0.25 at 0.06, 0.3 at 0.08, ABS): the 0.4's speeds at each layer height,
  peak acceleration 4000, `MODE=precision`, SCV 3, nothing refused. None has
  printed.
  1.4.x has not run it.
- **Cooldown, offline:** the per-degree progress and time-left logic was
  replayed tick by tick on five recorded Zero2 cooldowns (1.5 and 2.5 °C/min,
  curtain on): no degree skipped, no rise in the time left, within 0.2 min on
  the two that kept to schedule and 7 to 14 min short at the end of the three
  that ran over. The fan blend renders on Jinja 2.11.3 against an independent
  model of its arithmetic at three plate heights.
- **Cooldown, on a printer:** two ABS ramps on a 1.3.7 Zero, plate at Z 24.
  The first, at 3 °C/min with the curtain held off above 60 °C, fell 1–2 °C
  behind below 75 °C, stopped at 35.6 °C with the fans easing off, and its
  exhaust hunted between 50 and 80 % on a one-minute cycle. The second, with
  the fixes since (the curtain in once the exhaust is at full, the run to
  35.0 °C at full, a tick scaled to the rate), held the schedule to 60 °C,
  finished at 35.0 °C 147 s late, and its exhaust wandered about half as much.
  With both fans at full the plate could fall 2.0 °C/min at 39 °C and 1.4 at
  36 °C in both runs, so the published rate is 2 °C/min, which holds to about
  39 °C on a short part. It has not been run at 2 °C/min, and taller parts,
  with a weaker curtain, are predicted to fall behind sooner.
- **Preheat release:** the bed's fall after an early release was measured on
  the two waits that had one, 80 and 84 s for 120 → 100.5 °C, and the learned
  rate that replaces the fixed 5 °C/min is covered by five offline tests,
  including a fall timed after the wait has handed over. Not yet run on a
  printer.
- **Purge ring:** the rolled prime ran on the same print (0.4 nozzle, ABS):
  7.0 mm returned over 2 half laps, 0.117 mm of filament per mm against the
  ring's 0.186, with no extrusion before the first arc. The operator found
  no start blob on the ring.
- **Physical prints:** one ABS part on a 1.3.7 Zero with the 0.4 Standard
  profile from OrcaSlicer. The 0.25 and 0.3 nozzle profiles have been sliced,
  never printed.
- **Publication gates:** `scripts/check-publishable.py` over every tracked
  file, 0 blocks; the Sovol licence check finds only the names, pins, idioms and
  messages recorded in `NOTICE`.

## Candidate 2.0.3-beta.1

The offline suite passes **133 tests**, including seven release-packaging tests.
The builder produces only three slicer ZIPs from a clean committed checkout;
two independent builds match byte for byte. Extracted-content tests check the
single INI import bundles, complete Orca vendor copy, optional macro/module
dependencies, bed assets, licences, local documentation links and file hashes.
These tests check the installation files; they do not automate a GUI import.

The downloads contain 80 files for Orca and 16 each for PrusaSlicer and
SuperSlicer, including exactly three consolidated CFGs in each. Source configs,
builders and the individual PS/SS preset trees remain in Git. Profile behavior,
macro settings and runtime helper code are unchanged; the
release identifiers advance to 2.0.3-beta.1 (PS vendor 2.0.3, Orca 02.00.03.01).
The release notes record final-archive verification separately.

## Published 2.0.2-beta.1

The release passed 131 tests. The profile repairs retain the native
SuperSlicer travel-deceleration option
and add layer-limit/precision checks. Macro consolidation compares all effective
settings and renders both firmware configurations under Jinja 2.11.3. Release
tests extracted each slicer ZIP, installed into a fresh isolated data directory,
checked macro rebuilds and reproduced the original ZIP byte for byte without Git.
That release's complete source archive also reproduced from its extracted contents.

These are offline checks. Physical-print and platform limitations below remain.


## Candidate 2.0.1-beta.1

Recorded 2026-09-18. The standalone public candidate passes 113 unit tests,
macro/purge checks, local documentation links and a byte-identical rebuild from
a temporary tree containing no generated bundles. The source repair was tested with OrcaSlicer 2.4.2,
PrusaSlicer 2.9.6 and SuperSlicer 2.7.62.0-beta2.

| Check | Evidence |
|---|---|
| Cross-slicer matrix | 27 slices; eight nozzles, five tiers, nine filaments and four test objects; no refused keys |
| Normal 0.4 mm Standard ABS | All three slicers emit G-code with the skirt and `PURGE_LINE` |
| Personal configuration regression | Separate test slice omitted skirt, retained purge and respected its reduced acceleration cap; personal settings are not distributed |
| Native/Python fallback | Include-dependency checks, Jinja 2.11.3 rendering, helper/service failure tests |
| Missing optional commands | Captured Sovol 1.3.7 and 1.4.7 dispatchers warn and continue; a missing target of a known mux command still errors |

The archived-dispatch check was performed privately; its captured sources are
not shipped and that check is not counted as a public CI test. Public tests
exercise the distributed source and generated profiles. Automated render tests
do not instantiate every hardware object or verify mechanical clearances.

## Still pending

- A watched physical print of the latest **profiles-only stock startup**, with
  the default skirt, on each advertised firmware family.
- Feedback on manual installation in Windows and macOS.
- Physical flow/temperature calibration of added nozzle sizes; slicing alone
  cannot establish nozzle suitability or print quality.
- Measured filament shrinkage on a Zero. The shipped figures are nominal for
  each material class: right in direction, not in magnitude. PCTG takes the PETG class's value, nominally.
- Optional macro-pack commissioning on other machines, especially brush pad
  geometry and the 1.4.x fan/probe path.

The bed preview is Sovol's hotbed CAD, simplified, with the original schematic
square kept as a fallback texture. Neither is a collision model.

The optional Python modules are opt-in, not auto-installed dependencies. A
configured feature can deliberately refuse a failed check; absence of a feature
and an actual failure have different behavior. See [INSTALL.md](../INSTALL.md).

The standalone candidate also passed the full 27-slice matrix. Its three
archives have reproducible SHA-256 checksums; the extracted profiles archive
installs into fresh Orca, PrusaSlicer and SuperSlicer data directories.
[Machine-readable results](validation-2.0.1-beta.1.json) distinguish local
Python 3.14 validation from the not-yet-run hosted Python 3.11 workflow.
