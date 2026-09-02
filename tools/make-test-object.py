#!/usr/bin/env python3
"""Write the synthetic test object the slicer harness slices.

A Benchy is the wrong shape for this. What the harness measures is one number
per FEATURE TYPE -- perimeter, external perimeter, solid infill, top surface,
bridge, first layer -- so the object has to guarantee that every one of them
occurs, in quantity, at a known height, and nowhere else. A hull does not.

    +------------------------+  z=6   top solid infill
    |........................|        sparse infill
    |......+----------+......|  z=3   BRIDGE over the void, then solid above it
    |......|   void   |......|        perimeters: 4 outer + 4 inner walls
    |......+----------+......|  z=1
    |........................|        solid bottom
    +------------------------+  z=0   first layer

One box, one inverted box inside it. The inner shell's winding is reversed, so
the enclosed volume is empty and its ceiling has nothing beneath it -- which is
what forces a real bridge rather than a solid layer that happens to be flat.

Deterministic: same bytes every run, so a G-code diff between two slicer
versions is a slicer difference and never an object difference.
"""

import argparse
import os
import struct

OUT = "svzero-featurecube.stl"


def box(x0, y0, z0, x1, y1, z1, inward=False):
    """12 triangles. `inward` reverses the winding, making the box a void."""
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1),      # bottom  -Z
             (4, 5, 6, 7),      # top     +Z
             (0, 1, 5, 4),      # front   -Y
             (1, 2, 6, 5),      # right   +X
             (2, 3, 7, 6),      # back    +Y
             (3, 0, 4, 7)]      # left    -X
    tris = []
    for a, b, c, d in quads:
        tris += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    if inward:
        tris = [(t[0], t[2], t[1]) for t in tris]
    return tris


def normal(t):
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    m = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    return nx / m, ny / m, nz / m


def write_stl(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"SV Zero slicer harness feature cube".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<3f", *normal(t)))
            for p in t:
                fh.write(struct.pack("<3f", *p))
            fh.write(struct.pack("<H", 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", nargs="?", default=".")
    ap.add_argument("--size", type=float, default=24.0, help="outer XY, mm")
    ap.add_argument("--height", type=float, default=6.0)
    ap.add_argument("--void", type=float, default=12.0, help="inner XY, mm")
    ap.add_argument("--void-z0", type=float, default=1.0)
    ap.add_argument("--void-z1", type=float, default=3.0)
    args = ap.parse_args()

    s, h, w = args.size, args.height, args.void
    off = (s - w) / 2.0
    tris = box(0, 0, 0, s, s, h)
    tris += box(off, off, args.void_z0, off + w, off + w, args.void_z1, inward=True)
    path = os.path.join(args.outdir, OUT)
    write_stl(path, tris)
    print("%s  %d triangles  %.0fx%.0fx%.0f mm, void %.0fx%.0f from z%.1f to z%.1f"
          % (path, len(tris), s, s, h, w, w, args.void_z0, args.void_z1))


if __name__ == "__main__":
    main()
