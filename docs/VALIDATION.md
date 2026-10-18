# Validation and known limitations

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

The basic bed preview is an original schematic square, not a mechanical model.
The beta does not include the earlier third-party bed graphics.

The optional Python modules are opt-in, not auto-installed dependencies. A
configured feature can deliberately refuse a failed check; absence of a feature
and an actual failure have different behavior. See [INSTALL.md](../INSTALL.md).

The standalone candidate also passed the full 27-slice matrix. Its three
archives have reproducible SHA-256 checksums; the extracted profiles archive
installs into fresh Orca, PrusaSlicer and SuperSlicer data directories.
[Machine-readable results](validation-2.0.1-beta.1.json) distinguish local
Python 3.14 validation from the not-yet-run hosted Python 3.11 workflow.
