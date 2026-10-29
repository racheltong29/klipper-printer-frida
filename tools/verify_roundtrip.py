#!/usr/bin/env python3
"""Check that generated G-code still says what the FRIDA plan said.

Reads the .gcode back and rebuilds the strokes from pen state alone -- a stroke is
whatever the pen draws between going down and coming up -- then compares that against
the plan.json it came from. Geometry extraction never looks at the '; stroke' comments,
so this tests the moves the machine will actually make, not the bookkeeping.

Reports per-point deviation in canvas millimetres. Anything above a few microns means
the coordinate transform is wrong, not that the printer is imprecise.

  verify_roundtrip.py plan.json out.gcode [converter args used to build it...]

The converter args must match what produced the G-code (--group-colors, --fit, --tile-*,
--bed-x ...), because they decide stroke order and scale.
"""
import argparse
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_converter():
    path = os.path.join(HERE, "frida_plan_to_gcode.py")
    spec = importlib.util.spec_from_file_location("fptg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_gcode(path, pen_up_hint=None):
    """Rebuild drawn polylines from pen state. Returns (strokes, header).

    A point is recorded only while the pen is down. Pen-down is decided by comparing Z
    against the midpoint of the two Z levels the file actually uses, so it works for
    tapered strokes too (where Z varies continuously during a stroke).
    """
    header = {}
    zs = set()
    for line in open(path):
        if line.startswith(";"):
            m = re.search(r'scale=([\d.]+)', line)
            if m:
                header["scale"] = float(m.group(1))
            m = re.search(r'centre mapped to X([-\d.]+) Y([-\d.]+)', line)
            if m:
                header["cx"], header["cy"] = float(m.group(1)), float(m.group(2))
            continue
        if line.startswith("G1"):
            mz = re.search(r'Z(-?[\d.]+)', line)
            if mz:
                zs.add(float(mz.group(1)))
    if not zs:
        raise SystemExit("error: no Z moves in %s" % path)
    z_hi, z_lo = max(zs), min(zs)
    thresh = (z_hi + z_lo) / 2.0 if pen_up_hint is None else pen_up_hint

    strokes = []
    cur = []
    x = y = z = 0.0
    down = False
    in_reg = False
    for line in open(path):
        if line.startswith(";"):
            # Registration marks are machine furniture, not plan content.
            if "registration marks" in line:
                in_reg = True
            elif re.match(r'; stroke \d+', line):
                in_reg = False
            continue
        if not line.startswith("G1"):
            continue
        mx = re.search(r'X(-?[\d.]+)', line)
        my = re.search(r'Y(-?[\d.]+)', line)
        mz = re.search(r'Z(-?[\d.]+)', line)
        if mx:
            x = float(mx.group(1))
        if my:
            y = float(my.group(1))
        if mz:
            z = float(mz.group(1))
        now_down = z < thresh
        if in_reg:
            down = now_down
            continue
        if now_down and not down:
            cur = [(x, y)]            # pen just landed
        elif now_down and down:
            if mx or my:
                cur.append((x, y))    # drawing
        elif down and not now_down:
            if len(cur) >= 1:
                strokes.append(cur)
            cur = []
        down = now_down
    if down and cur:
        strokes.append(cur)
    return strokes, header


def expected_strokes(plan_path, argv, conv):
    """Ask the converter itself what strokes it means to draw, in emission order."""
    ap = conv.build_parser() if hasattr(conv, "build_parser") else None
    if ap is None:
        raise SystemExit("error: converter has no reusable parser; update verify_roundtrip")
    args = ap.parse_args(list(argv) + [plan_path, "-o", os.devnull])
    import json
    plan = json.load(open(plan_path))
    gcode, stats = conv.convert(plan, args)
    return stats, args


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("gcode")
    ap.add_argument("--tol-mm", type=float, default=0.002,
                    help="per-point tolerance in bed mm (default 0.002 = 2 microns)")
    ap.add_argument("conv_args", nargs=argparse.REMAINDER,
                    help="the converter flags used to build the gcode")
    args = ap.parse_args()

    conv = _load_converter()
    conv_argv = [a for a in args.conv_args if a != "--"]

    # What the converter intends, straight from its own pipeline.
    cap = conv.build_parser()
    cargs = cap.parse_args(conv_argv + [args.plan, "-o", os.devnull])
    import json
    plan = json.load(open(args.plan))
    _, stats = conv.convert(plan, cargs)
    want = stats["emitted"]          # [(colour, layer, [(bedx, bedy), ...]), ...]

    got, header = parse_gcode(args.gcode)

    print("plan        : %s" % args.plan)
    print("gcode       : %s" % args.gcode)
    print("header      : scale=%s centre=(%s, %s)"
          % (header.get("scale"), header.get("cx"), header.get("cy")))
    print()
    print("strokes expected from plan : %d" % len(want))
    print("strokes found in gcode     : %d" % len(got))
    if len(want) != len(got):
        print("MISMATCH in stroke count -- cannot compare point by point")
        return 2

    npts_w = sum(len(w[2]) for w in want)
    npts_g = sum(len(g) for g in got)
    print("points expected / found    : %d / %d" % (npts_w, npts_g))

    worst = 0.0
    worst_at = None
    total = 0.0
    n = 0
    bad_counts = 0
    for i, (w, g) in enumerate(zip(want, got)):
        if len(w[2]) != len(g):
            bad_counts += 1
            continue
        for j, ((wx, wy), (gx, gy)) in enumerate(zip(w[2], g)):
            d = ((wx - gx) ** 2 + (wy - gy) ** 2) ** 0.5
            total += d
            n += 1
            if d > worst:
                worst, worst_at = d, (i, j)
    if bad_counts:
        print("strokes with wrong point count: %d" % bad_counts)
    if n:
        print("per-point deviation        : mean %.6f mm, max %.6f mm%s"
              % (total / n, worst,
                 "  at stroke %d point %d" % worst_at if worst_at else ""))

    # Colour and layer sequence must survive the transform too.
    seq_w = [(w[0], w[1]) for w in want]
    layers = [w[1] for w in want]
    inversions = sum(1 for k in range(1, len(layers)) if layers[k] < layers[k - 1])
    changes = sum(1 for k in range(1, len(seq_w)) if seq_w[k][0] != seq_w[k - 1][0])
    print("colour changes in order    : %d" % changes)
    print("cross-layer inversions     : %d" % inversions)

    ok = (len(want) == len(got) and not bad_counts
          and worst <= args.tol_mm and inversions == 0)
    print()
    print("VERDICT: %s" % ("round-trip faithful" if ok else "ROUND-TRIP MISMATCH"))
    if not ok and worst > args.tol_mm:
        print("  max deviation %.6f mm exceeds tolerance %.6f mm" % (worst, args.tol_mm))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
