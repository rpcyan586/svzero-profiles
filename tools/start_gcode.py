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
ORCA_PREFLIGHT_BLOCK = "\n".join([
    ";  _PREFLIGHT is first so a rejected job costs no heating. It is inert",
    ";  unless the optional spool_guard extra is installed.",
    "_PREFLIGHT",
])

ORCA_START_HEADER = "\n".join([
    ";---SV Zero startup. See notes/incidents/2026-08-17-zero2-startup-crash.",
    ";  START_PRINT owns homing, nozzle cleaning, Z calibration and bed mesh.",
    ";  DO NOT add G28 here. Sovol's original had three homes per job.",
])

# The publication chamber policy. install-presets.py locates this exact text.
ORCA_CHAMBER_BLOCK = "\n".join([
    ";  Chamber control is never omitted. With activation enabled, M191 waits",
    ";  for Orca's exact Minimal temperature and M141 sets its Target. Unchecked",
    ";  or incomplete activation uses bare M191 and the S32/22C fallback.",
    ORCA_PREFLIGHT_BLOCK,
    "M140 S[bed_temperature_initial_layer_single]",
    "{if activate_chamber_temp_control[0]}",
    "{if chamber_temperature[0] > 0}",
    "{if chamber_minimal_temperature[0] > 0}",
    "M191 S{chamber_minimal_temperature[0]}",
    "{else}M191 S{chamber_temperature[0]}",
    "{endif}",
    "M141 S{chamber_temperature[0]}",
    "{else}M191{endif}",
    "{else}M191{endif}",
])

ORCA_START_TAIL = "\n".join([
    "@@CLEAN_TEMP@@",
    "M190 S[bed_temperature_initial_layer_single] ; bed hot BEFORE calibration and mesh",
    "START_PRINT",
    "G90",
    "G1 Z5 F600 ; clearance from the freshly calibrated bed before any XY move",
    "M104 S[nozzle_temperature_initial_layer]",
    "M109 S[nozzle_temperature_initial_layer]",
    "BRUSH_PRIME ; give back exactly what CLEAN_NOZZLE retracted; it could not be",
    "; primed earlier because the clean and probe sequence runs below",
    "; min_extrude_temp, where Klipper refuses any E move",
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
ORCA_SHADOW_BOOST = (
    "BOOST=1 MAX_BOOST=10 COAST_RATE=1 INITIAL_BOOST=5 FILTER_ALPHA=0.25"
)

ORCA_SHADOW_CHAMBER_BLOCK = "\n".join([
    ";>>>CHAMBER_SHADOW_TEST sandbox-only, not publication policy",
    ";  Start bed heat first, apply activation/target/minimum policy, park at",
    ";  the XY center and run the full toolhead fan at Z15 before nozzle heat.",
    ";  The spool gate stays ahead of bed heat here too: a sandbox test print",
    ";  consumes the same real filament as a publication one.",
    ORCA_PREFLIGHT_BLOCK.split("\n")[-1],
    "M140 S[bed_temperature_initial_layer_single]",
    "{if activate_chamber_temp_control[0]}",
    "{if chamber_temperature[0] > 0}",
    "{if chamber_minimal_temperature[0] > 0}",
    "M191 S{chamber_minimal_temperature[0]} Z15 STIR=1 " + ORCA_SHADOW_BOOST,
    "{else}M191 S{chamber_temperature[0]} Z15 STIR=1 " + ORCA_SHADOW_BOOST,
    "{endif}",
    "M141 S{chamber_temperature[0]}",
    "{else}M191 Z15 STIR=1 " + ORCA_SHADOW_BOOST + "{endif}",
    "{else}M191 Z15 STIR=1 " + ORCA_SHADOW_BOOST + "{endif}",
    ";<<<CHAMBER_SHADOW_TEST",
])
