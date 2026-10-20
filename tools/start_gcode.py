"""The single definition of the Orca start G-code and its chamber block.

`generate.py` emits the publication preamble; `install-presets.py` finds the
chamber block inside an already-installed preset and swaps in the sandbox
shadow-test variant. Both need the same text, and both used to carry their own
copy of it.

That duplication broke on 2026-08-25. Adding `_PREFLIGHT` to the preamble in
`generate.py` left `install-presets.py` searching for text that no longer
existed, so `sandbox.sh orca-shadow` died with "normal Orca chamber start block
not found" and OrcaSlicer would not launch at all. The copies had no test
tying them together, because the one test that touched the block built its
fixture out of the very constant that had drifted.

So the block lives here once. `ORCA_START_PREAMBLE` is assembled from
`ORCA_CHAMBER_BLOCK`, which means the text `install-presets.py` searches for is
by construction the text `generate.py` emitted.

Anything ordered before the first heater command belongs in
`ORCA_PREFLIGHT_BLOCK`, and both the normal and shadow chamber blocks must
begin with it. `scripts/test-install-presets.py` asserts that.
"""

# Runs before any heater is commanded, so a refused job costs nothing. Inert
# unless the optional spool_guard extra is installed; see
# notes/kb/spool-preflight.md.
# Bump alongside _SVZERO_PACK.version in svzero_pack.cfg. The two are compared
# as plain strings and a mismatch only warns -- see the note in that file.
SVZERO_PACK_VERSION = "2.1.2"
# The machine mode these profiles are sliced for, compared with
# _SVZERO_PACK.mode. Unlike the version, a mismatch REFUSES the job: a mode is
# a machine configuration selected by an include and a restart, and a job
# sliced for another one asks for motion the machine was not set up for.
SVZERO_MODE = "standard"
SVZERO_REQUIRE_LINE = "SVZERO_REQUIRE VERSION=%s MODE=%s" % (SVZERO_PACK_VERSION, SVZERO_MODE)

ORCA_PREFLIGHT_BLOCK = "\n".join([
    ";  SVZERO_REQUIRE names the pack version and machine mode this profile was",
    ";  built for. On a printer without the pack it is an unknown command,",
    ";  which Klipper reports and steps over. A different pack version prints",
    ";  one warning. A different MODE refuses the job here, before any heater.",
    ";  It also tells the pack that a job was sliced by an SV Zero profile, so",
    ";  START_PRINT can say when one was not.",
    SVZERO_REQUIRE_LINE,
    ";  _PREFLIGHT is next so a rejected job costs no heating. It is inert",
    ";  unless the optional spool_guard extra is installed.",
    "_PREFLIGHT",
])

ORCA_START_HEADER = "\n".join([
    ";---SV Zero startup. See notes/incidents/2026-08-17-zero2-startup-crash.",
    ";  START_PRINT owns homing, nozzle cleaning, Z calibration and bed mesh.",
    ";  DO NOT add G28 here. Sovol's original had three homes per job.",
])

# The publication chamber policy. install-presets.py locates this exact text.
#
# Orca's two fields decide the branch, and a zero in either is a REQUEST, not a
# gap to be filled in:
#
#   Minimal > 0   M191 waits for exactly it, then M141 sets the Target.
#   Minimal = 0   no M191 at all -- zero means do not wait. The Target still
#                 goes to M141, so the chamber is controlled, just not waited
#                 for. Until 2026-09-03 this branch substituted the TARGET as
#                 the minimum, which turned a deliberate no-wait job into a
#                 full soak: an ABS job sliced Minimal=0 Target=60 emitted
#                 `M191 S60` and held the print open for 13 minutes with the
#                 bed boosted to 117 C. The comment here already claimed the
#                 fallback was bare M191; the code did something else.
#   Target  = 0   M141 S0 and nothing else. No wait, no ceiling, no chamber
#                 control -- which is what asking for a zero target means. It
#                 is emitted rather than omitted so a stale ch_target from the
#                 previous job cannot survive into this one.
#
# Unchecked activation emits bare M191. That is the barebones-profile case, and
# the macro's own defaults answer it: minimum 22 C, target 32 C. A profile that
# says nothing still lands somewhere safe instead of uncontrolled.
ORCA_CHAMBER_BLOCK = "\n".join([
    ";  Chamber control is never omitted. With activation enabled, M191 waits",
    ";  for Orca's exact Minimal temperature and M141 sets its Target. A zero",
    ";  Minimal skips the wait and keeps the Target; a zero Target turns",
    ";  chamber control off entirely. Unchecked activation uses bare M191 and",
    ";  its own 22 C minimum / 32 C target fallback.",
    ORCA_PREFLIGHT_BLOCK,
    "M140 S[bed_temperature_initial_layer_single]",
    "{if activate_chamber_temp_control[0]}",
    "{if chamber_temperature[0] > 0}",
    "{if chamber_minimal_temperature[0] > 0}",
    "M191 S{chamber_minimal_temperature[0]}",
    "{endif}",
    "M141 S{chamber_temperature[0]}",
    "{else}M141 S0{endif}",
    "{else}M191{endif}",
])

# What the start G-code says about SVZERO_NOZZLE_GROWTH, in every slicer.
NOZZLE_GROWTH_COMMENT = "\n".join([
    ";  SVZERO_NOZZLE_GROWTH offsets Z for the nozzle being hotter at the first",
    ";  layer than when START_PRINT took the Z reference. On as published on",
    ";  1.3.7, the tare-tap; off on eddy-only 1.4.x, where it applies nothing.",
    ";  SAVE_VARIABLE VARIABLE=nozzle_growth_mm_per_c VALUE=0 turns it off.",
    ";  Without the pack it is an unknown command, which Klipper reports and",
    ";  steps over.",
])

ORCA_START_TAIL = "\n".join([
    "@@CLEAN_TEMP@@",
    # Consumed by END_PRINT, armed here: the policy has to be in RAM before the
    # print finishes, and this is where the other generated material lookup
    # lives. machine_end_gcode stays the bare END_PRINT every bundle ships.
    "@@COOLDOWN@@",
    # Stock START_PRINT does not guarantee a settled bed before calibration.
    # The public profile must provide that wait even without our replacement.
    "M190 S[bed_temperature_initial_layer_single]",
    "START_PRINT",
    # The tier's X/Y current, from the print preset's Notes, generated from
    # source/model.json by tools/generate.py (xy_current_block). After
    # START_PRINT, so homing and probing keep printer.cfg's current.
    "@@XY_CURRENT@@",
    # Before any move or heat after START_PRINT, with the same placeholder as
    # the M104 below. PrusaSlicer and SuperSlicer carry the same line in
    # source/presets.json; tests/test_nozzle_growth.py holds all of them to it.
    NOZZLE_GROWTH_COMMENT,
    "SVZERO_NOZZLE_GROWTH TEMP=[nozzle_temperature_initial_layer]",
    "G90",
    "G1 Z5 F600 ; clearance from the freshly calibrated bed before any XY move",
    # The per-machine Z offset placeholder the PrusaSlicer and SuperSlicer
    # printer presets have carried since v1.0.2. Z_ADJUST adds to the current
    # offset (gcode_move.py cmd_SET_GCODE_OFFSET), and START_PRINT's
    # SET_GCODE_OFFSET Z=0 runs before it, so this composes with the
    # filament's per-material stub rather than overwriting it. Edit the value
    # in the machine preset for this printer; + means farther from the bed.
    "SET_GCODE_OFFSET Z_ADJUST=-0.00 MOVE=1",
    "M104 S[nozzle_temperature_initial_layer]",
    "M109 S[nozzle_temperature_initial_layer]",
    "PURGE_LINE VOL_MAX=[filament_max_volumetric_speed] FIL_D=[filament_diameter]"
    " NOZZLE=[nozzle_diameter] CLEAR={skirt_distance + skirt_loops * initial_layer_line_width + 2}",
])

ORCA_START_PREAMBLE = "\n".join([
    ORCA_START_HEADER,
    ORCA_CHAMBER_BLOCK,
    ORCA_START_TAIL,
])

ORCA_START_CUT = "M109 S[nozzle_temperature_initial_layer];wait for extruder temp"
ORCA_PURGE_START = "{if first_layer_print_min[1] - 6 > print_bed_min[1]}"
ORCA_PURGE_END = "{endif}"

# Sandbox only. Never written into the generated publication bundle.
# MAX_BOOST is deliberately absent: the ceiling is the extra's absolute
# max_bed_temp, and a magnitude passed here would only nerf it.
ORCA_SHADOW_BOOST = (
    "BOOST=1 COAST_RATE=1 INITIAL_BOOST=5 FILTER_ALPHA=0.25"
)

ORCA_SHADOW_CHAMBER_BLOCK = "\n".join([
    ";>>>CHAMBER_SHADOW_TEST sandbox-only, not publication policy",
    ";  Start bed heat first, apply activation/target/minimum policy, park at",
    ";  the XY center and run the full toolhead fan at Z15 before nozzle heat.",
    ";  A zero Minimal skips M191 outright -- no park, no stir, no soak, and",
    ";  no bed boost -- because zero means do not wait.",
    ";  The spool gate stays ahead of bed heat here too: a sandbox test print",
    ";  consumes the same real filament as a publication one.",
    ORCA_PREFLIGHT_BLOCK,
    "M140 S[bed_temperature_initial_layer_single]",
    "{if activate_chamber_temp_control[0]}",
    "{if chamber_temperature[0] > 0}",
    "{if chamber_minimal_temperature[0] > 0}",
    "M191 S{chamber_minimal_temperature[0]} Z15 STIR=1 " + ORCA_SHADOW_BOOST,
    "{endif}",
    "M141 S{chamber_temperature[0]}",
    "{else}M141 S0{endif}",
    "{else}M191 Z15 STIR=1 " + ORCA_SHADOW_BOOST + "{endif}",
    ";<<<CHAMBER_SHADOW_TEST",
])
