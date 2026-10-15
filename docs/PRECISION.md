# Precision mode

A second machine configuration for slow, quiet, fine-detail printing on the
0.4 mm nozzle and the smaller ones, 0.3, 0.25 and 0.2. It is optional, and it is a mode rather than a quality tier:
Klipper runs differently in it, so a restart is needed to enter or leave it, and
it has its own printer profile in each slicer.

## What changes

| | Standard | Precision |
|---|---|---|
| X/Y microsteps | 16 | 64, interpolation off |
| X/Y current | 2.4 A on Fine to Strength, set per job; Draft runs printer.cfg's 3.5 A | 1.4 A |
| X/Y chopper | Klipper's defaults | TBL 3, TOFF 3, HSTRT 7, HEND 3, TPFD 5 |
| Velocity / acceleration ceiling | printer.cfg's | 300 mm/s, 4000 mm/s², enforced |
| Layers | Fine to Draft tiers | 0.4 nozzle: 0.04, 0.06, 0.08, 0.10, 0.12 mm over a 0.20 first layer. 0.3: 0.04, 0.06, 0.08 over 0.24. 0.25: 0.04, 0.06 over 0.20. 0.2: 0.04, 0.06 over 0.16 |
| Walls, tops | per tier | 3 walls, ironed top surfaces |
| Speeds | per tier | 2 mm³/s on the 0.4 through the normal speed ratios; outer walls at most 45 mm/s. A smaller nozzle runs the 0.4's speeds at the same layer height |
| Accelerations | per tier | Prusa Core One 0.25 DETAIL: 3000 default, 800 outer, 1200 inner, 2500 infill, 2000 solid, 1000 top, 500 first layer, 4000 travel |
| Square corner velocity | per tier | 3 |
| Z-hop | on | off |
| Filaments | all | all except TPU |

Why these values:

- **64 microsteps with interpolation off** is Klipper's own recommendation for
  positional accuracy; spreadCycle is kept, because stealthChop trades accuracy
  for quiet. Homing is unaffected: the Zero's X and Y home on physical switches.
- **300 mm/s** is the step-rate budget. Peak per-motor rate on CoreXY is
  5 × microsteps × v × √2 steps/s; stock's 1200 mm/s at 16 microsteps is about
  136k steps/s, and the same rate at 64 microsteps allows 300 mm/s. Klipper's
  `SET_VELOCITY_LIMIT` and `M204` assign limits rather than clamping them, so
  the mode file wraps both and clamps every request to the ceiling, saying so on
  the console when it does.
- **The chopper settings** are corgi108's published recipe for the Zero's
  TMC5160 drivers, with TPFD at 5 as he later suggested for plain Klipper.
- **1.4 A** is lower than corgi108's 2.4 A, which the standard tiers now run
  (operator, 2026-10-14). Lower current is a noise and heat choice:
  dissipation goes with the square of the current. The precision tiers ask for
  4000 mm/s² at most; that 1.4 A holds them without lost steps is not yet
  measured on a Zero.

## Install

1. Install the firmware-matched macro pack (INSTALL.md section 2). Its
   include must come after printer.cfg's stepper sections, where INSTALL.md
   puts it.
2. Copy `optional-macros/svzero-mode-precision.cfg.off` into the pack's
   `svzero/` directory, unchanged. With the `.off` the printer stays in standard
   mode.
3. Import the slicer profiles as usual. Precision appears as its own printer:
   `SV Zero Precision 0.4n` (and `0.3n`, `0.25n`, `0.2n`) in PrusaSlicer and
   SuperSlicer, and the separate `SV Zero Precision` printer model in
   OrcaSlicer (enable it and its nozzles in printer selection beside
   `SV Zero`).

## Switching

**By hand.** Rename `svzero-mode-precision.cfg.off` to
`svzero-mode-precision.cfg` and restart Klipper to enter precision mode; rename
it back and restart to leave. The pack's firmware file ends with
`[include svzero-mode-*.cfg]`, so nothing in printer.cfg changes. `SVZERO_MODE`
in the console reports the current mode.

**Automatically, with the optional Moonraker component.** Copy
`optional-macros/svzero_mode_switch.py` to `~/moonraker/moonraker/components/`,
add a section to `moonraker.conf`:

    [svzero_mode_switch]

and restart Moonraker. When a job sliced for the other mode is started, it is
refused on its first line, before any heater, and the component then:

- waits for the refused job to end, and requires the printer idle and cold
  (no job, heater targets 0), checked again immediately before the restart;
- renames the mode file and restarts Klipper, reverting the rename if Klipper
  does not come back;
- starts the same file again once Klipper reports the requested mode.

It acts once per file per 15 minutes, so a job cannot make the printer switch
back and forth. `SVZERO_MODE MODE=precision` (or `standard`) asks for a switch
with no job. Its log is `svzero_mode_switch.log` beside Moonraker's.

## What refuses

Only the mode itself:

- A job sliced for the other mode is refused before heating. Without the
  component the message names the exact file to rename.
- In precision mode, a job whose start G-code does not say which mode it was
  sliced for is refused at `START_PRINT`.
- If the mode file is selected but its microsteps did not take effect -- because
  printer.cfg's own stepper sections come after the pack include -- a precision
  job is refused rather than printed at the wrong resolution.

## Limits

- **0.4 mm nozzle and below.** Layers on the smaller nozzles keep the 0.4's
  share of the bore, 10–30%, on whole Z steps and no thinner than 0.04 mm. On
  the Zero a nozzle swap is a hardware modification, and precision on the 0.3,
  0.25 and 0.2 has been sliced, not printed.
- **Verified on firmware 1.3.7 only.** 1.4.x has the same TMC5160 sections,
  endstops and current, and its driver code accepts every key the mode file
  sets, but no 1.4.x printer has run it yet.
- Slicing is verified in all three slicers; print quality is what you are
  testing. Watch the first print for `Timer too close` and lost steps.
