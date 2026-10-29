#!/usr/bin/env python3
"""Generate pen-plotter bring-up tests for a real printer, safest first.

Each test answers one question and nothing else, so a failure tells you where to look.
Run them in order; do not skip to a real plan.

  make_calibration.py zladder -o z.gcode --z-start 1.0 --z-stop 0.0 --steps 11
  make_calibration.py square  -o sq.gcode --size 100
  make_calibration.py repeat  -o rp.gcode --passes 5
  make_calibration.py speed   -o sp.gcode
  make_calibration.py all     -o all.gcode

Tests:
  zladder  short lines at descending pen Z. The first one that marks evenly is your
           pen offset. ALWAYS run this first, and never below --z-stop.
  square   a size x size square with both diagonals. Measure with calipers: side
           lengths catch scale error, diagonals catch skew, corners catch backlash.
  repeat   the same small cross drawn N times, lifting and going away between each.
           If the N crosses do not sit on top of each other you have a repeatability
           problem (belts, QGL, or a loose pen mount).
  speed    parallel lines at rising feedrates. Shows where line quality falls apart.
"""
import argparse
import sys


def _hdr(args, title):
    return [
        "; %s" % title,
        "; pen plotter bring-up test -- NO heaters, NO extrusion",
        "; bed %gx%g, pen up Z%g" % (args.bed_x, args.bed_y, args.pen_up),
        "; Z here is RELATIVE to your current gcode offset: set the pen offset with",
        ";   SET_GCODE_OFFSET Z=<value>  before running, and verify with a zladder.",
        "G21", "G90",
        "M104 S0", "M140 S0",           # make sure nothing heats
        "G28",
        "G1 Z%.3f F%d" % (args.pen_up, args.z_feed),
    ]


def _end(args):
    return ["G1 Z%.3f F%d" % (args.pen_up, args.z_feed),
            "G1 X%.1f Y%.1f F%d" % (args.margin, args.bed_y - args.margin, args.travel),
            "M400", "M117 test done"]


def zladder(args):
    """Descending-Z short lines. The pen offset is whichever rung marks cleanly."""
    g = _hdr(args, "Z LADDER: find the pen offset")
    n = args.steps
    if n < 2:
        sys.exit("error: --steps must be >= 2")
    x0 = args.bed_x / 2.0 - 40.0
    y0 = args.bed_y / 2.0 + (n - 1) * args.spacing / 2.0
    g.append("; %d rungs from Z%.3f down to Z%.3f, %.1fmm apart"
             % (n, args.z_start, args.z_stop, args.spacing))
    g.append("; rung 1 is TOPMOST (highest Y) and highest Z")
    for i in range(n):
        z = args.z_start + (args.z_stop - args.z_start) * i / float(n - 1)
        y = y0 - i * args.spacing
        g.append("; rung %d  Z%.3f" % (i + 1, z))
        g.append("M117 rung %d Z%.2f" % (i + 1, z))
        g.append("G1 X%.3f Y%.3f F%d" % (x0, y, args.travel))
        g.append("G1 Z%.3f F%d" % (z, args.z_feed))
        g.append("G1 X%.3f Y%.3f F%d" % (x0 + 80.0, y, args.draw))
        g.append("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    g += _end(args)
    return g


def square(args):
    """Square + diagonals for scale, skew and corner behaviour."""
    s = args.size
    cx, cy = args.bed_x / 2.0, args.bed_y / 2.0
    lo_x, hi_x = cx - s / 2.0, cx + s / 2.0
    lo_y, hi_y = cy - s / 2.0, cy + s / 2.0
    g = _hdr(args, "SQUARE %gmm: scale, skew, corners" % s)
    g.append("; measure all four sides (scale) and both diagonals (skew).")
    g.append("; diagonals should both be %.2f mm" % (s * 2 ** 0.5))
    g.append("M117 square %gmm" % s)
    g.append("G1 X%.3f Y%.3f F%d" % (lo_x, lo_y, args.travel))
    g.append("G1 Z%.3f F%d" % (args.pen_down, args.z_feed))
    for x, y in ((hi_x, lo_y), (hi_x, hi_y), (lo_x, hi_y), (lo_x, lo_y)):
        g.append("G1 X%.3f Y%.3f F%d" % (x, y, args.draw))
    g.append("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    for a, b in (((lo_x, lo_y), (hi_x, hi_y)), ((lo_x, hi_y), (hi_x, lo_y))):
        g.append("G1 X%.3f Y%.3f F%d" % (a[0], a[1], args.travel))
        g.append("G1 Z%.3f F%d" % (args.pen_down, args.z_feed))
        g.append("G1 X%.3f Y%.3f F%d" % (b[0], b[1], args.draw))
        g.append("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    g += _end(args)
    return g


def repeat(args):
    """Same cross N times, with a detour between, to expose repeatability error."""
    cx, cy = args.bed_x / 2.0, args.bed_y / 2.0
    r = 6.0
    g = _hdr(args, "REPEATABILITY: %d passes over one cross" % args.passes)
    g.append("; all %d crosses should land on each other. Any visible thickening or" % args.passes)
    g.append("; doubling is mechanical error, not a software problem.")
    for i in range(args.passes):
        g.append("M117 pass %d/%d" % (i + 1, args.passes))
        # go somewhere else first so each pass re-approaches from a distance
        g.append("G1 X%.3f Y%.3f F%d" % (args.margin + 10, args.margin + 10, args.travel))
        for (ax, ay), (bx, by) in (((cx - r, cy), (cx + r, cy)),
                                   ((cx, cy - r), (cx, cy + r))):
            g.append("G1 X%.3f Y%.3f F%d" % (ax, ay, args.travel))
            g.append("G1 Z%.3f F%d" % (args.pen_down, args.z_feed))
            g.append("G1 X%.3f Y%.3f F%d" % (bx, by, args.draw))
            g.append("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    g += _end(args)
    return g


def speed(args):
    """Parallel lines at rising feedrates: where does line quality break down."""
    feeds = [600, 1200, 1800, 3000, 6000, 9000, 12000]
    x0 = args.bed_x / 2.0 - 50.0
    y0 = args.bed_y / 2.0 + len(feeds) * args.spacing / 2.0
    g = _hdr(args, "SPEED: line quality vs feedrate")
    g.append("; one line per feedrate, top to bottom. Look for wobble, rounded ends,")
    g.append("; and skipped starts. Pick the fastest line that still looks clean.")
    for i, f in enumerate(feeds):
        y = y0 - i * args.spacing
        g.append("; F%d = %d mm/s" % (f, f // 60))
        g.append("M117 F%d" % f)
        g.append("G1 X%.3f Y%.3f F%d" % (x0, y, args.travel))
        g.append("G1 Z%.3f F%d" % (args.pen_down, args.z_feed))
        g.append("G1 X%.3f Y%.3f F%d" % (x0 + 100.0, y, f))
        g.append("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    g += _end(args)
    return g


TESTS = {"zladder": zladder, "square": square, "repeat": repeat, "speed": speed}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("test", choices=sorted(TESTS) + ["all"])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--bed-x", type=float, default=350.0, dest="bed_x")
    ap.add_argument("--bed-y", type=float, default=350.0, dest="bed_y")
    ap.add_argument("--margin", type=float, default=5.0)
    ap.add_argument("--pen-up", type=float, default=5.0, dest="pen_up")
    ap.add_argument("--pen-down", type=float, default=0.2, dest="pen_down")
    ap.add_argument("--z-start", type=float, default=1.0, dest="z_start",
                    help="zladder: highest (safest) pen Z")
    ap.add_argument("--z-stop", type=float, default=0.0, dest="z_stop",
                    help="zladder: lowest pen Z. Going below 0 risks the pen/bed")
    ap.add_argument("--steps", type=int, default=11, help="zladder rungs")
    ap.add_argument("--size", type=float, default=100.0,
                    help="square: side length in mm (default 100)")
    ap.add_argument("--spacing", type=float, default=8.0, help="mm between lines")
    ap.add_argument("--passes", type=int, default=5, help="repeat: number of passes")
    ap.add_argument("--draw", type=int, default=1800)
    ap.add_argument("--travel", type=int, default=6000)
    ap.add_argument("--z-feed", type=int, default=600, dest="z_feed")
    args = ap.parse_args()

    if args.z_stop < -0.2:
        sys.exit("error: --z-stop %.2f is low enough to crash the pen into the bed.\n"
                 "  Z here is relative to your gcode offset; start high and work down."
                 % args.z_stop)
    if args.z_start < args.z_stop:
        sys.exit("error: --z-start must be above --z-stop (ladder descends)")

    names = sorted(TESTS) if args.test == "all" else [args.test]
    lines = []
    for i, n in enumerate(names):
        if i:
            lines.append("")
            lines.append("; ======================================================")
        lines += TESTS[n](args)
    body = "\n".join(lines) + "\n"
    with open(args.out, "w") as f:
        f.write(body)

    # Bounds report, so you know before sending.
    import re
    xs = [float(m.group(1)) for m in
          (re.search(r'X(-?[\d.]+)', l) for l in lines if l.startswith("G1")) if m]
    ys = [float(m.group(1)) for m in
          (re.search(r'Y(-?[\d.]+)', l) for l in lines if l.startswith("G1")) if m]
    zs = [float(m.group(1)) for m in
          (re.search(r'Z(-?[\d.]+)', l) for l in lines if l.startswith("G1")) if m]
    print("test(s)     : %s" % ", ".join(names))
    print("moves       : %d" % sum(1 for l in lines if l.startswith("G1")))
    if xs:
        print("X range     : %.2f .. %.2f   (bed %.0f, margin %.0f)"
              % (min(xs), max(xs), args.bed_x, args.margin))
        print("Y range     : %.2f .. %.2f" % (min(ys), max(ys)))
    if zs:
        print("Z range     : %.3f .. %.3f" % (min(zs), max(zs)))
    bad = (xs and (min(xs) < args.margin or max(xs) > args.bed_x - args.margin)) or \
          (ys and (min(ys) < args.margin or max(ys) > args.bed_y - args.margin))
    print("bounds      : %s" % ("OUTSIDE the printable area" if bad else "inside"))
    print("wrote       : %s" % args.out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
