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

# Three shapes, because one cannot force three different code paths.
#
# The cube alone carried the harness until 2026-09-06, and on that day the slice
# matrix found that no 0.8 or 1.0 nozzle profile could be sliced by anything --
# the organic support tip and branch diameters were below the support extrusion
# width. The cube DID catch it, but only because the slicer validates support
# settings when it loads them, whether or not the object needs support. Nothing
# here had ever asked a slicer to actually BUILD a support. That is luck, not
# coverage.
#
#   featurecube  perimeters, solid infill, top surface, a real bridge over a
#                void, first layer. What the per-feature speed and width table
#                is measured from.
#   overhang     an L that cantilevers 18 mm over nothing, so support material
#                must be generated and its geometry exercised rather than only
#                validated.
#   tower        10 x 10 x 40, ~200 layers at 0.2. Layer time falls below
#                slowdown_below_layer_time, so the cooling path runs -- which is
#                the one value the (8M) merge deliberately did NOT take from the
#                SV08 MAX, and therefore the one worth exercising.
SHAPES = ("featurecube", "overhang", "tower", "smallpins")
STL_NAME = {"featurecube": OUT,
            "overhang": "svzero-overhang.stl",
            "tower": "svzero-tower.stl",
            "smallpins": "svzero-smallpins.stl"}


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


def prism_xz(poly, faces, y0, y1):
    """Extrude a 2-D profile in the XZ plane along Y into a closed solid.

    `poly` is the outline in order; `faces` indexes it into triangles, supplied
    rather than computed because the only profile here is a non-convex L and a
    fan triangulation would fold over its notch.
    """
    lo = [(x, y0, z) for x, z in poly]
    hi = [(x, y1, z) for x, z in poly]
    tris = []
    for a, b, c in faces:
        tris.append((lo[a], lo[b], lo[c]))     # -Y cap
        tris.append((hi[a], hi[c], hi[b]))     # +Y cap
    n = len(poly)
    for i in range(n):                          # the skirt
        j = (i + 1) % n
        tris.append((lo[i], hi[j], lo[j]))
        tris.append((lo[i], hi[i], hi[j]))
    return tris


def overhang(width=24.0, depth=24.0, leg=6.0, shelf_z=8.0, height=10.0):
    """An L on its side: a leg, and a shelf cantilevered over nothing."""
    poly = [(0.0, 0.0), (leg, 0.0), (leg, shelf_z),
            (width, shelf_z), (width, height), (0.0, height)]
    faces = [(0, 1, 2), (0, 2, 5), (2, 3, 4), (2, 4, 5)]
    return prism_xz(poly, faces, 0.0, depth)


def cylinder(cx, cy, r, z0, z1, seg=32):
    """A closed vertical cylinder: wall, plus a fan cap at each end."""
    import math
    ring = [(cx + r * math.cos(2 * math.pi * i / seg),
             cy + r * math.sin(2 * math.pi * i / seg)) for i in range(seg)]
    tris = []
    for i in range(seg):
        (ax, ay), (bx, by) = ring[i], ring[(i + 1) % seg]
        lo_a, lo_b, hi_a, hi_b = (ax, ay, z0), (bx, by, z0), (ax, ay, z1), (bx, by, z1)
        tris += [(lo_a, lo_b, hi_b), (lo_a, hi_b, hi_a)]           # wall, outward
        tris += [((cx, cy, z0), lo_b, lo_a)]                        # bottom
        tris += [((cx, cy, z1), hi_a, hi_b)]                        # top
    return tris


def smallpins(radii=(1.0, 1.5, 2.5, 4.0, 8.0), height=4.0, pitch=22.0):
    """Free-standing pins, sized to straddle the small-perimeter threshold.

    The one feature none of the other three objects has, and the one that broke
    a real print on 2026-09-07: PrusaSlicer applies `small_perimeter_speed` to
    any loop of radius <= 6.5 mm, and its unset default of 15 mm/s ran half of
    every layer at a tenth of the intended speed. The feature cube, the overhang
    and the tower contain no loop small enough to reach that path at all, so 27
    green slices said nothing about it.

    PINS RATHER THAN A PLATE WITH HOLES. Holes need the top and bottom faces
    triangulated as annuli; the first attempt skipped that and shipped a mesh
    with 240 unpaired edges that PrusaSlicer silently auto-repaired -- a fixture
    whose behaviour depends on a repair heuristic is not a fixture. Each pin is
    a closed cylinder, manifold by construction, and four of the five radii are
    under the threshold while 8.0 is over it.
    """
    tris = []
    for i, r in enumerate(radii):
        tris += cylinder(pitch * (i + 1), pitch, r, 0.0, height)
    return tris


def tower(side=10.0, height=40.0):
    """Small footprint, many layers -- short layer times, so cooling engages."""
    return box(0, 0, 0, side, side, height)


def normal(t):
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    m = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    return nx / m, ny / m, nz / m


def write_stl(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"SV Zero slicer harness synthetic test object".ljust(80, b" "))
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
    ap.add_argument("--shape", default="featurecube",
                    choices=SHAPES + ("all",))
    ap.add_argument("--size", type=float, default=24.0, help="outer XY, mm")
    ap.add_argument("--height", type=float, default=6.0)
    ap.add_argument("--void", type=float, default=12.0, help="inner XY, mm")
    ap.add_argument("--void-z0", type=float, default=1.0)
    ap.add_argument("--void-z1", type=float, default=3.0)
    args = ap.parse_args()

    def build(shape):
        if shape == "featurecube":
            s, h, w = args.size, args.height, args.void
            off = (s - w) / 2.0
            t = box(0, 0, 0, s, s, h)
            t += box(off, off, args.void_z0, off + w, off + w, args.void_z1, inward=True)
            return t, "%.0fx%.0fx%.0f mm, void %.0fx%.0f from z%.1f to z%.1f" % (
                s, s, h, w, w, args.void_z0, args.void_z1)
        if shape == "overhang":
            return overhang(), "L, 18 mm cantilever at z8 -- forces support"
        if shape == "smallpins":
            return smallpins(), ("pins r=1.0/1.5/2.5/4.0/8.0 x 4 mm -- straddles "
                                 "the 6.5 mm small-perimeter threshold")
        return tower(), "10x10x40 mm tower -- ~200 layers, short layer times"

    for shape in (SHAPES if args.shape == "all" else (args.shape,)):
        tris, note = build(shape)
        path = os.path.join(args.outdir, STL_NAME[shape])
        write_stl(path, tris)
        print("%s  %d triangles  %s" % (path, len(tris), note))


if __name__ == "__main__":
    main()
