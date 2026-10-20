# Bed preview: svzero_bed.stl and svzero_bed.svg

**svzero_bed.stl**, a slicer bed model for the Sovol Zero, made 2026-10-16 by
rpcyan.

The bed is Sovol's own CAD for the Zero's hotbed: `STEP/hotbed kit.STEP` in
https://github.com/Sovol3d/SOVOL-ZERO. It was tessellated with OpenCascade,
placed with its origin at the centre of the 152.4 mm print area and its plate
top at z = 0, stripped of faces that cannot be seen from any direction, and
simplified to about 26,500 triangles, within 0.2 mm of the CAD. Sovol's repository has
no licence file; its README describes the Zero as "fully open-source (including
hardware, firmware, and structure)" under "the open-source guidelines of Voron
and Klipper".

Added here: two 8 mm lead screws on the axes of the hotbed's nut bores, raised
by the 152.4 mm print height so they stand beside a full-height part as they do
at the end of a print. These changes are GPL-3.0; see `LICENSE` in the
repository or release archive.

N1_3D_Printing's Sovol Zero Bed Model (Printables 1251674) first put the Zero's
lead screws in a slicer bed. Nothing of it is used here: its geometry matches
Sovol's hotbed CAD, and this model is built from that CAD directly.

**svzero_bed.png**, the bed texture, is "Sovol Zero buildplate texture" by
Chinchill (https://www.printables.com/model/1263730), used unmodified under the
Creative Commons Attribution 4.0 International licence,
https://creativecommons.org/licenses/by/4.0/. It is drawn over the 152.4 mm
build area; its white border is that area's outline.

**svzero_bed_grid.png** is the same texture with a 10 mm grid added by rpcyan
(lines every 10 mm from the build area's front-left corner), also under
CC BY 4.0. PrusaSlicer and SuperSlicer use it, because they draw their own grid
only when no texture is set; OrcaSlicer draws its grid over the plain one.

**svzero_bed.svg**, an original schematic of the 152.4 mm square build area,
is kept as a fallback texture. Copyright 2026 rpcyan and contributors;
GPL-3.0.

Neither file is a printable model or a collision model.
