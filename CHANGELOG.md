# Changelog

## Unreleased

### Fixed: PrusaSlicer and SuperSlicer never ran the filament start G-code

- **The per-material Z offset never ran in PrusaSlicer or SuperSlicer**, in
  any release from 2.0.1-beta.1 to 2.1.1. Each filament's start G-code opens
  with a `;` comment. The slicers keep that setting as a list, one entry per
  extruder, and split an unquoted value at every `;`, so the extruder got an
  empty entry and `SET_GCODE_OFFSET Z_ADJUST=0.000 MOVE=1` never reached the
  G-code. OrcaSlicer was not affected.
- **PrusaSlicer never applied the filament's pressure advance** in those
  releases. PrusaSlicer has no pressure-advance setting, so the pack sends it
  on that same G-code as `SET_PRESSURE_ADVANCE`. Klipper kept the value it
  already had, printer.cfg's or the previous job's. SuperSlicer sets pressure
  advance itself and was not affected.
- **Both now run it**, written quoted as the slicers write it themselves.
  Sliced at 0.4 Standard, PLA - Brass runs `SET_GCODE_OFFSET Z_ADJUST=0.000
  MOVE=1` in both, and `SET_PRESSURE_ADVANCE ADVANCE=0.036` in PrusaSlicer.
  ABS, PC and TPU now get `ADVANCE=0` in PrusaSlicer, as in OrcaSlicer;
  SuperSlicer sends nothing for them and Klipper keeps its current value.
- **A filament preset copied from an earlier release still lacks the stub.**
  Copy it again from this release's preset.

### Fixed: SuperSlicer stopped PLA, PLA Silk and PETG jobs at the start

- **SuperSlicer: PLA, PLA Silk and PETG no longer stop at the start.** The
  printer presets set `tool_name = brass`, and SuperSlicer writes each
  filament's pressure advance as `SET_PRESSURE_ADVANCE ... EXTRUDER=brass`.
  Klipper accepts only a configured extruder's name there, so it raised an
  error and stopped the job. `tool_name` is now empty, SuperSlicer's default,
  which writes `EXTRUDER=extruder`. ABS, PC and TPU carry no pressure advance
  and were unaffected. Shipped from 2.0.1 to 2.1.1; found reading the G-code,
  not on a printer.

## 2.1.1

Profile behaviour changes in this release: accelerations scaled to the Zero's
measured input-shaper limits, Draft's speeds under the PLA flow cap, a lower
X/Y stepper current on Fine to Strength, and a slower corner on precision. The
optional macro pack gains a 5-133 Hz input-shaper test. None of it has printed
yet; see the [validation record](docs/VALIDATION.md).

### Draft no longer clips

- **Draft was Sovol's speedBenchy verbatim**, authored against the 50 mm³/s
  machine rating. With PLA's 21 mm³/s the slicer clamped it: on the 0.4 nozzle
  the outer wall ran 248 mm/s instead of 251, the inner wall 248 instead of 302
  and infill 216 instead of 302, so the outer wall ran as fast as the inner.
- **Draft is now derived like the other tiers**, against the same 21 mm³/s PLA,
  its infill asking the 21 mm³/s cap itself on every Draft nozzle, and the
  walls in the usual proportion below it. On the 0.4: outer wall 180, inner
  wall 216, infill 216 mm/s. OrcaSlicer runs exactly that. PrusaSlicer and
  SuperSlicer trim the infill 2%, to 212, which is their clamp landing on 21
  without the filament's 0.98 flow ratio; their walls run as asked. Draft is
  still the fastest tier on every nozzle it ships on.
- **speedBenchy is kept as a hidden reference**, like Sovol's stock profile: not
  in any download, there for comparison.

### Precision mode

- **Square corner velocity 1** (was 3), the lowest of any tier, so precision
  takes its corners slowest. Sent the same way as the other tiers: the
  PrusaSlicer and SuperSlicer start G-code reads `SVZERO_SCV=1mm` from the
  preset's notes, and OrcaSlicer's jerk fields are all 1. Sliced in all three
  at 0.4 nozzle 0.08 mm and 0.2 nozzle 0.04 mm: `SQUARE_CORNER_VELOCITY=1`,
  `MODE=precision`, nothing refused.

### Accelerations scaled to the Zero's measured shaper limits

Each tier is scaled as a whole, every feature keeping its proportion to the
others, so that its peak acceleration sits on the input-shaper limits measured
on a Zero (11,500 mm/s² on X and 6,000 on Y, MZV):

| Tier | Peak | Was |
|---|---:|---:|
| Precision | 2,000 | 3,000 (travel 4,000) |
| Fine | 4,000 | 6,000 |
| Optimal | 6,000 | 11,500 |
| Standard | 11,500 | 40,000 |
| Strength | 6,000 (follows Optimal) | 11,500 |
| Draft | 40,000 (unchanged) | 40,000 |

- **Standard is no longer stock's accelerations verbatim**, but keeps stock's
  proportions: default and infill 11,500, solid and bridge 5,750, inner wall
  3,450, outer 2,875, top 1,438, first layer 288.
- **Two cells hold the ladder**, which may not go backwards from Fine to
  Draft: Fine's first layer is 261 (Optimal's) rather than its scaled 333, and
  Optimal's top surface is 1,438 (Standard's) rather than its scaled 1,565.
- **Travel acceleration is per tier**, at most the peak: 2,000 on Precision,
  4,000 on Fine, 5,000 elsewhere as before.
- **Small nozzles** (0.2, 0.25, 0.3) keep the SV08 0.2 accelerations' proportions,
  scaled so their peak is the tier's.

### Input shaping

- **The resonance test sweeps 5–133 Hz** instead of Sovol's 35–45 Hz. A Zero's
  X axis resonates near 61 Hz, so the stock test returned about 40 Hz for X
  and shaped where there is little resonance. The front screen's
  Setup → Resonances runs the same test and is fixed with it.
- **Shaper damping 0.1**, the value `SHAPER_CALIBRATE` fits at, in place of
  Sovol's 0.001. Measured damping on a Zero is 0.10–0.12.
- **1.3.7's accelerometer axes are corrected** (`axes_map: x,z,y`, as Sovol
  ships from 1.3.8). Stock 1.3.7 swaps the X and Y graphs and labels; the
  fitted frequency was not affected.
- **`SVZERO_SHAPER_CALIBRATE`** runs the full sweep on X and Y, or one axis,
  and is refused in precision mode or during a job.
- **`SVZERO_SHAPER_TYPICAL`** applies typical measured Zero values (X mzv
  62.5 Hz, Y mzv 46.5 Hz) until Klipper restarts.
- **No shaper type or frequency is set by the pack**, so `SAVE_CONFIG` still
  saves your calibration: Klipper refuses to save a value an include also sets.

### Changed: lower X/Y stepper current

- **Fine, Optimal, Standard and Strength run X and Y at 2.4 A**, against the
  3.5 A in Sovol's printer.cfg. Each print preset's Notes name the current
  (`SVZERO_XY_CURRENT=2.4A`), and the start G-code hands it to the macro pack
  right after `START_PRINT`, so homing and probing keep printer.cfg's current.
  `END_PRINT` and `CANCEL_PRINT` put printer.cfg's back. Same path in all three
  slicers; OrcaSlicer reads the Notes from its start G-code too.
- **Draft keeps printer.cfg's 3.5 A.** It runs at stock's accelerations.
- **Precision mode runs 1.4 A**, down from 2.4 A, in its mode file.
- **The macro only lowers.** `SVZERO_XY_CURRENT` refuses a value above the
  configured `run_current` and the job runs at that. Without the macro pack the
  line is an unknown command, which Klipper reports and steps over, and the
  printer keeps its own current.
- **Why:** less current is quieter and cooler, since dissipation goes with its
  square. 2.4 A is corgi108's published `run_current` for the Zero's TMC5160s,
  which other owners run. **Not yet measured:** that 2.4 A holds Standard's and
  Strength's accelerations, and 1.4 A Precision's, without lost steps.

## 2.1.0

Versions are now three numbers with no beta suffix. Profile behaviour changes
in this release: accelerations, walls, seams, square corner velocity and two
new nozzle sizes, and a new precision mode. Fan defaults change in the optional
macro pack.

### Fixed: filament shrinkage was backwards in OrcaSlicer and SuperSlicer

- **Parts came out smaller instead of larger.** 2.0.2-beta.1 and 2.0.3-beta.1
  set `filament_shrink` to 100 plus the shrinkage, 100.8% for ABS. OrcaSlicer
  and SuperSlicer read that number as the size a part measures once cooled, as
  their tooltips say, and scale by 100 divided by it. So every part was shrunk
  a further 0.8% rather than enlarged: undersized by about 1.6% for ABS, 1.4%
  for PC, 1.2% for TPU, 0.8% for PETG and 0.6% for PLA. It is now 100 minus the
  shrinkage: ABS 99.2%. Parts sliced in those two with 2.1.0 come out that much
  larger than with the betas.
- **PrusaSlicer was right and is unchanged in XY.** It takes the shrinkage
  itself (`filament_shrinkage_compensation_xy`, 0.8% for ABS).
- **Z is compensated too**, by the same figure, in OrcaSlicer
  (`filament_shrinkage_compensation_z`) and PrusaSlicer. It was 0. SuperSlicer
  has no Z compensation.
- **The values are still nominal**, not measured on a Zero: PLA 0.3, PETG 0.4,
  ABS 0.8, PC 0.7, TPU 0.6%.
- How it was settled: OrcaSlicer's source divides by the setting; a 20 mm
  cube sliced in SuperSlicer came out 21.8 mm at 90% and 17.8 mm at 110%; and
  the same ABS part re-sliced in OrcaSlicer's GUI at 99.2% came out 1.6% larger
  in X and Y than at 100.8%.

### Precision mode

- **A second machine configuration for slow, quiet, fine-detail printing on the
  0.4 nozzle and below** ([docs/PRECISION.md](docs/PRECISION.md)): 64 microsteps with
  interpolation off, 2.4 A, corgi108's chopper recipe, and a 300 mm/s /
  4000 mm/s² ceiling enforced by clamping `SET_VELOCITY_LIMIT` and `M204`.
- **Its own printer in every slicer**, `SV Zero Precision 0.4n` (Orca: the
  `SV Zero Precision` printer model), with five tiers, 0.04–0.12 mm over a
  0.20 first layer: 2 mm³/s through the normal ratios, outer walls at most
  45 mm/s, Prusa Core One 0.25 DETAIL accelerations, 3 walls, ironing, no
  z-hop, no TPU.
- **And on the smaller nozzles**, `SV Zero Precision 0.3n`, `0.25n` and
  `0.2n`: 0.04–0.08 mm on the 0.3 and 0.04–0.06 on the 0.25 and 0.2, the
  0.4's 10–30% of the bore on whole Z steps. Each layer height runs the 0.4's
  speeds and the same accelerations, with the small-nozzle first layer and
  walls. Sliced, not printed.
- **Selected by a file**, `svzero-mode-precision.cfg.off` beside the pack:
  rename it and restart Klipper. The firmware files end with
  `[include svzero-mode-*.cfg]`, so printer.cfg is not edited.
- **The handshake refuses the wrong mode before heating**, naming the file to
  rename, and refuses a precision job whose mode file did not take effect.
- **Optional automatic switching** with the `svzero_mode_switch` Moonraker
  component: a refused job switches the printer once idle and cold, and starts
  again.

### Bed boost on by default; fewer extras in the pack

- **Bed boost is the default** with the preheat helper: the plate runs above
  its print target while the chamber climbs, and is back before printing.
  `M191 ... BOOST=0` turns it off for one wait, and now actually does: the
  shipped hold policy boosted the bed whatever `BOOST` said.
- **Removed from the published pack:** the orbiting stir (`chamber_orbit.cfg`,
  `CHAMBER_ORBIT_SWEEP`, `ORBIT_WHINE_SWEEP`) and the fan duty and rpm chart
  sensors (`fan_duty.py`, `exhaust_duty.cfg`).

### M191 reads S and R as Marlin does

- **`M191 R<t>` is accepted.** It was refused. As in Marlin, it sets the
  chamber target and waits whichever way the chamber has to go. Below the
  target it runs the same soak as `S`. Above it, the exhaust runs at full until
  the chamber has been within 1 °C for 10 s, or gives up and carries on if it
  falls less than 1.5 °C in 2 minutes (Marlin's defaults). With both `S` and
  `R`, `S` wins.
- **The cooling wait needs the Python preheat helper.** Without it, an `R` that
  would have to cool is refused before anything changes, rather than skipped.
  An `R` below its target still waits on the rear sensor, as `S` does.
- **`TOL` is ignored with a notice** instead of refused.
- Unchanged: `S` sets the target and waits only while heating, and a bare
  `M191` keeps the pack's compatibility soak.

### M191 console lines in plain words

- **One status line a minute, without the seconds stamp.** It was every 30 s,
  each line led by a `:37`-style stamp.
- **Words and units instead of internal names.** `noz=32.1 eta=24.1
  bed=99.9>120.0 coast=--` now reads `chamber 32.1C of 45.0C, about 24 min to
  go. Bed 99.9C, set to 120.0C`. The nozzle's settling phase says `steady for
  0 of 30 s` rather than `dwell=0/30`, and every ETA carries its unit.
- An early wipe still says the chamber is not confirmed at temperature.
- The numbers behind the lines are unchanged in the extra's status and in
  `_CH_STATE`.

### M191: an ETA learned from past waits, and less hot waiting before the brush

- **The chamber ETA comes from the printer's own past waits.** Each completed
  wait saves how many minutes it still had to go at each whole degree short of
  its minimum (`ch_eta_profile` in `save_variables`, blended across waits), and
  the next wait reads its ETA off that from the first minute, scaled by its own
  pace once the bed is hot. The live fit had nothing to go on while the bed was
  still heating: on the test printer one wait opened at 134 min, then 49, 91 and
  29, for a 17 min soak. Predicting each of six recorded waits from the other
  five, the learned profile was out by 0.67 min on average. Without
  `[save_variables]`, or before a wait has completed, the live fit runs as
  before; `eta_learn: False` turns it off.
- **The plate comes back down before the soak ends.** Under the default hold
  policy the bed stayed on its ceiling until the chamber arrived, and the
  slicer's `M190` then waited out a 20 °C descent with the nozzle already
  heating. With a learned ETA it is released when the time left matches its
  descent, so it reaches the print temperature as the soak ends.
- **How fast the plate falls is learned too.** Each wait times the released
  plate to its landing and saves the rate (`ch_bed_fall_rate`), blended over
  waits like the ETA, since it differs between printers and conditions. On the
  test printer it falls about 14 °C/min with the part fan stirring, not the
  5 °C/min assumed before: the boost was being let go 6–7 minutes before the
  soak ended instead of about 2.5, and the chamber's climb dropped from 1.2–1.5
  to under 0.6 °C/min for the rest of the wait. `bed_release_rate` is now
  only the starting value.
- **The nozzle waits at the probe temperature, not the wipe temperature,
  while the bed is still more than 3 °C off its target.** `SET_CLEAN_TEMP`
  used to heat straight to the wipe temperature ahead of that `M190`; on the
  test printer the nozzle sat at 195 °C for 1.8 min before the brush. The brush
  sequence's own heat-up takes it the rest of the way.
- **`_PREFLIGHT` calls `_SVZERO_PRINT_START_HOOK` if one is defined**, at
  every job's first line before any heating: a place for machine-specific
  start actions, such as lights, that cannot wait for `START_PRINT` once the
  M191 soak holds the G-code queue. The pack defines none.

### Cooldown: one rate, one line per degree

- **The bed cools at 2 °C/min for every material.** The material table used to
  set its own rate, 1.0 to 5.0 °C/min; it now sets only the endpoint (30 °C for
  PLA, 35 °C otherwise). 2 °C/min is about as fast as the plate can still fall
  near the endpoint with both fans at full on the test printer: at 3 it fell
  behind from about 50 °C and finished 2.5 minutes late. `variable_bed_rate` in
  `_COOLDOWN_CFG` changes it, and `COOLDOWN_ARM BED_RATE=` still overrides one
  print.
- **One console line per degree of bed**, every degree, instead of one a
  minute. Re-sampled once a minute, the recorded ABS cooldowns skipped 19 of
  their 65 degrees. The prefix is `cooldown:` rather than `cool`, so a line
  reads `cooldown: bed 73C, about 23 min to 35C, exhaust 30%` instead of
  `cool cooling to 35C: bed 73C, 23.3min left (measured) of 43.3min (bed now
  73C), assist demand ...`.
- **A steadier time left.** It is the slower of the schedule and the plate's
  own rate over at least 4 minutes, and it never runs past the point where
  the ramp gives up. The old figure, a 60-second slope, rose between lines on a
  third of them. Replayed on five recorded cooldowns, the new one never rose.
  It still cannot predict the slow last few degrees, and reads short there.
- **The other lines are plain words too**, with minutes instead of seconds. The
  assist's controller terms are gone from the progress line; fan duties are
  shown as percentages.
- **The curtain fan, once on, stays on for the rest of the ramp**, at its floor
  or above (`curtain_hold`). It used to drop out a minute or so after coming on
  and come back.
- **The curtain fan joins the assist by default, once the exhaust is at
  full.** It comes in at its lowest speed while the exhaust gives back the same
  amount of cooling, so the handover is smooth; above that the exhaust returns
  to full first and the curtain carries the rest. It used to be off, and
  when switched on it was kept off above a 60 °C bed and came in early, at a
  low combined demand. A faster ramp can now use the curtain at any bed
  temperature; `variable_bed_ceiling` puts a temperature limit back.
  `variable_curtain_enable: 0` in `_COOLDOWN_ASSIST_CFG` leaves the exhaust on
  its own.
- **Each fan counts for what it was measured to do.** On the test printer, with
  the plate held at 80 °C, the curtain cooled it 1.3 to 3.4 times as hard as
  the exhaust, most with the bed high in the box, where the curtain's jet hits
  it. The assist splits its demand by that ratio at the plate's present height,
  and in each fan's rpm rather than its PWM duty. The ratio used to be a
  placeholder 1.0. `curtain_strength_factor` is now a trim on the measured
  ratio.
- **Steadier fans: the ramp checks in proportion to its rate.** It used to
  re-evaluate every 15 s whatever the rate. At 3 °C/min a degree is due every
  20 s, so the bed target held 15, 15, then 30 s, and on the test printer the
  exhaust swung between about 50 and 80 % on that one-minute beat. The tick is
  now 15 s at 1 °C/min and shorter in proportion, 5 s at 3 °C/min, so every
  degree takes four ticks (`ramp_tick`, between `ramp_tick_min` and
  `ramp_tick_max`).
- **The ramp ends at the endpoint, with the fans at full to the last.** It
  used to stop within 1 °C of it (`finish_tolerance`, now 0), and once the
  schedule had run out the fans eased off as the plate closed in. Past the
  schedule both fans now stay at their ceiling until the plate arrives.
- **The ramp starts the moment the nozzle has cooled to the bed**, with its
  first one-degree step at once. Settling checks every 5 s instead of 15, and
  the first step used to come up to a minute later.
- **The bed is aimed ahead of the schedule, not behind it.** The whole-degree
  target now steps as soon as the schedule line reaches a degree, so it sits
  up to 1 °C below the line; it used to round, up to 0.5 °C above it. The fan
  assist aims a quarter degree ahead of the line and comes on as soon as the
  heater has let go and the bed is behind that aim. It used to wait until the
  bed was 1 °C behind, and then hold it there: on Zero2's five recorded
  cooldowns the bed ran more than 0.2 °C behind the line for 67–73% of the
  ramp, a fan at full for almost none of it.
- **The bed heater is the measure of enough cooling.** While it fires, the
  assist does not push harder, and each progress line shows its average duty
  since the last one: a heater that blips now and then means the fans are
  keeping up.

### Purge ring: a lap and a half, drawn

- **The full lap of the purge ring is drawn again.** It was one `G3` ending
  where it began, which Klipper draws as a full circle only when its computed
  sweep is exactly zero; float residue in the other direction leaves no arc and
  extrudes the whole lap in place. On the last three jobs on the test printer
  the outer ring did exactly that: 27.6 mm³ of a 40 mm³ purge went into the
  start blob, and what showed was the half lap. Each full lap is now two half
  circles, which resolve the same either way.
- **Every nozzle lays at least a lap and a half**, the start blob opposite the
  part. From 0.8 mm up the purge used to be a single half circle; those nozzles
  now lay a narrower bead, down to 1.5 × the nozzle, so the same volume makes a
  lap and a half. At that floor, 1.2 and 1.4 lay a little more than their
  volume (71.5 and 102.5 mm³, against 70.5 and 82.9). 0.2 to 0.6 are unchanged.
- **No start blob.** The filament retracted by the brush and by the last
  print's end, plus the tip, used to be pushed back with the nozzle standing at
  the ring's start, and that was the blob. It now goes back while the nozzle
  arcs along the outer circle into the start, over as many half laps (up to 4)
  as keep it no richer per mm than the ring. A 0.4 nozzle with 7 mm to return
  takes one full lap.
- **The purge geometry check now draws arcs as Klipper does**, across every
  nozzle and a grid of part positions. Its old arc measure assumed a full
  circle where Klipper drew nothing. It also fails a purge that extrudes before
  its first arc, or a prime that is richer than the ring or does not end at its
  start.

### Fewer macros in Mainsail's panel

- **Seven macros now start with an underscore**, which keeps them out of
  Mainsail's macro panel: `_CHAMBER_ON`, `_CHAMBER_OFF` (`M141` covers both),
  `_BRUSH_STATUS`, `_BRUSH_TEACH`, `_PURGE_STATUS`, `_COOLDOWN_STATUS` and
  `_COOLDOWN_CONFIG`. Type them in the console under the new names; the old
  names are gone. Nothing a slicer sends was renamed, so files already sliced
  are unaffected.

### Licensing

- **`NOTICE` states why the Sovol macro text is GPL-3.0.** Sovol's Zero
  repository has no licence, but Sovol publishes the same macro family under
  GPL-3.0 in its SV08 repository (commit `a60644875f8c`), and every Sovol line
  in the pack is found there or is a macro name, a Klipper command, a pin, the
  park position or a status message.

### Acceleration cap holds in SuperSlicer

- **`install-presets.py --accel-cap` now caps SuperSlicer's percentage
  accelerations too.** It used to skip them, and some shares are above 100%: under
  a 5000 cap, Fine's infill still asked for 6000, and Optimal's and Strength's for
  5750. Each percentage is now resolved against the capped values and written as
  the cap only where it would exceed it.

### Re-anchored acceleration ladder

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

### 0.25 and 0.3 nozzles, small-nozzle rules

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

### Accelerations honoured in every slicer

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

### 0.2 nozzle anchor, internal bridges, minimum layer time

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

### Machine mode in the handshake

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

### Fan defaults

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
