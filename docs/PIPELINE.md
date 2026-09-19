# Profile pipeline

The authored fragment tree and machine tables in `source/` generate each
slicer's dialect. `tools/start_gcode.py` centralizes Orca startup; the fragment
tree carries PrusaSlicer-family startup. Both retain optional macro calls,
explicit temperature waits and public skirt settings.

Generation uses pinned, locally included public inputs. No private archive or
printer connection is needed. See [DEVELOPMENT.md](DEVELOPMENT.md) for commands
and checks, and [VALIDATION.md](VALIDATION.md) for measured coverage.
