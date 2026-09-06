# Installing

Two steps on the printer, one in the slicer. Nothing needs editing.

I have two Sovol Zeros - one on 1.4.7 and one still factory-original on 1.3.7 - and the pack works out of the box on both. Where they differ the macros check at runtime rather than asking you which you have.

## Printer

Copy the pack into its own directory beside `printer.cfg`, so `~/printer_data/config/svzero/` holds the `.cfg` files and the `klipper/` folder that came with them.

Copy the host extras and restart the service:

```
cp ~/printer_data/config/svzero/klipper/*.py ~/klipper/klippy/extras/
sudo systemctl restart klipper
```

That has to be a service restart. `RESTART` and `FIRMWARE_RESTART` reuse the running Python process and keep the old modules, so a fresh copy of an extra will not be picked up by either.

Copy all four even if you do not want all of them. The `.cfg` files declare sections that need them, and Klipper refuses to start when a section names an extra that is not installed.

Then add one line to `printer.cfg`, near the bottom, after Sovol's `[include Macro.cfg]`:

```
[include svzero/svzero-1.3.7.cfg]
```

or, on 1.4.x:

```
[include svzero/svzero-1.4.x.cfg]
```

Pick the one that matches your firmware and include only that one. They differ solely in which hardware file they pull in - the exhaust fan is a different pin and a different object between the two, and the name `fan3` means different hardware on each.

That is the whole install. The include order inside those files is deliberate and load-bearing, which is why they are an explicit list rather than a wildcard: Klipper applies includes linearly and a later duplicate section wins, which is how this overrides Sovol's macros without editing `Macro.cfg`. Their `factory_resets.sh` can overwrite their file without taking these with it.

## Slicer

Import the bundle for whichever slicer you use. Orca gets a vendor bundle, PrusaSlicer and SuperSlicer get config bundles.

The profiles do not carry a printer address. Add your own physical printer once, in the dialog built for it.

## What runs without what

The profiles and the macros are two downloads and they can drift apart. Neither direction aborts a print.

A profile on a printer without the pack: every command the start G-code adds is an ordinary macro call, and Klipper answers an unknown one with a console warning and carries on. You lose the chamber wait, the pre-flight and the cooldown ramp. Nothing stops.

The pack on a printer running somebody else's profile: `END_PRINT` and `CLEAN_NOZZLE` override Sovol's own and close the retract cycle with no slicer involvement at all, chamber control infers a target from the bed temperature when nothing was declared, and the cooldown falls back to its material table. `START_PRINT` says so once when a job arrives without identifying itself.

Version drift between the two prints one line naming both versions. It is a warning, not a refusal. A pack newer than the profile is the normal state and is almost always fine.

## Optional pieces

Spoolman. If Moonraker has `[spoolman]` configured, jobs are checked for enough filament and the right material before any heater is commanded. If it does not, the check is skipped and says so once per session. You do not have to run Spoolman and you do not have to turn anything off.

Exhaust duty on the temperature chart. Uncomment `[include exhaust_duty.cfg]` in the file you included above. It puts the exhaust fan's commanded duty and its measured RPM on Mainsail's chart as two traces, normalised to the same 0-100 scale. They do not track each other - at 10% duty the rotor is turning at about 29% of full speed - and the gap is widest at the bottom of the range, which is where the cooling assist works.

## Probing

1.3.7 has a bed load cell and an eddy sensor. 1.4.0 dropped the load cell, so a 1.4.x machine ships the file but never configures it.

`START_PRINT` checks for `[probe_pressure]` and forces the load-cell overlay only where there is one. Without it the eddy path decides for itself whether it has work to do, which lets it skip when it already has good data.

This matters more than it looks. Passing `METHOD=force_overlay` on 1.4.7 does not fail - their `z_offset_calibration.py` skips the routine only when `METHOD` is exactly `default`, so any other value bypasses the skip and runs a full heat-up and a contact probe on every print. That is the split 0dysseusRex settled on in [Rex-Sovol-Zero-Mainline](https://github.com/0dysseusRex/Rex-Sovol-Zero-Mainline): eddy for homing and mesh, load cell for fine Z offset where it exists, eddy-only where it does not.

## Removing it

Delete the one include line. The `.cfg` files and the extras can stay where they are; nothing outside that directory was modified.
