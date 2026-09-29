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
SVZERO_PACK_VERSION = "2.0.3-beta.1"

ORCA_PREFLIGHT_BLOCK = "\n".join([
    ";  SVZERO_REQUIRE names the pack this profile was built against. It only",
    ";  ever WARNS: on a printer without the pack it is an unknown command,",
    ";  which Klipper reports and steps over, and on a printer with a different",
    ";  pack version it prints one line. It also tells the pack that a job was",
    ";  sliced by an SV Zero profile, so START_PRINT can say when one was not.",
    "SVZERO_REQUIRE VERSION=%s" % SVZERO_PACK_VERSION,
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
    "G90",
    "G1 Z5 F600 ; clearance from the freshly calibrated bed before any XY move",
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
