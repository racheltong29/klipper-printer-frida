#!/usr/bin/env python3
"""Render plotter G-code to SVG so you can eyeball it against frida_simulator.html.

Geometry comes from pen state (same extraction as verify_roundtrip.py), so what you see
is what the machine will move, not a re-render of the plan. Colours come from the
'; stroke ... colour' comments, since pen colour is bookkeeping rather than motion.

  gcode_preview.py plan.gcode -o preview.svg
  gcode_preview.py plan.gcode -o preview.svg --travel      also show travel moves
  gcode_preview.py plan.gcode -o preview.svg --bed 350x350
"""
import argparse
import os
import re
import sys


def parse(path):
    """Yield ('draw'|'travel', colour, [(x, y), ...]) segments, plus Z extremes."""
    zs = set()
    for line in open(path):
        if line.startswith("G1"):
            m = re.search(r'Z(-?[\d.]+)', line)
            if m:
                zs.add(float(m.group(1)))
    if not zs:
        raise SystemExit("error: no Z moves in %s" % path)
    thresh = (max(zs) + min(zs)) / 2.0

    segs = []
    x = y = z = 0.0
    down = False
    colour = "#161616"
    is_reg = False
    cur = []
    cur_travel = []
    for line in open(path):
        if line.startswith(";"):
            if "registration marks" in line:
                is_reg = True
            m = re.match(r'; stroke \d+/\d+  layer \d+  colour (\S+)', line)
            if m:
                is_reg = False
                colour = m.group(1)
            continue
        if not line.startswith("G1"):
            continue
        mx = re.search(r'X(-?[\d.]+)', line)
        my = re.search(r'Y(-?[\d.]+)', line)
        mz = re.search(r'Z(-?[\d.]+)', line)
        px, py = x, y
        if mx:
            x = float(mx.group(1))
        if my:
            y = float(my.group(1))
        if mz:
            z = float(mz.group(1))
        now = z < thresh

        if now and not down:
            cur = [(x, y)]
        elif now and down:
            if mx or my:
                cur.append((x, y))
        elif down and not now:
            if len(cur) > 1:
                segs.append(("reg" if is_reg else "draw", colour, cur))
            cur = []
        elif not now and not down and (mx or my):
            cur_travel.append(((px, py), (x, y)))
        down = now
    if down and len(cur) > 1:
        segs.append(("reg" if is_reg else "draw", colour, cur))
    return segs, cur_travel


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gcode")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--bed", default="350x350", help="bed size WxH in mm (default 350x350)")
    ap.add_argument("--margin", type=float, default=5.0)
    ap.add_argument("--travel", action="store_true", help="draw travel moves faintly")
    ap.add_argument("--stroke-width", type=float, default=0.6, dest="sw")
    ap.add_argument("--px-per-mm", type=float, default=3.0, dest="ppm")
    args = ap.parse_args()

    try:
        bw, bh = (float(v) for v in args.bed.lower().split("x"))
    except ValueError:
        raise SystemExit("error: --bed wants WxH, e.g. 350x350")

    segs, travels = parse(args.gcode)
    draws = [s for s in segs if s[0] == "draw"]
    regs = [s for s in segs if s[0] == "reg"]

    W, H = bw * args.ppm, bh * args.ppm

    def pt(p):
        # SVG y grows downward; bed Y grows up.
        return "%.2f,%.2f" % (p[0] * args.ppm, (bh - p[1]) * args.ppm)

    o = []
    o.append('<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
             'viewBox="0 0 %.0f %.0f">' % (W, H, W, H))
    o.append('<rect width="100%%" height="100%%" fill="#ffffff"/>')
    # bed outline and printable margin
    o.append('<rect x="0" y="0" width="%.1f" height="%.1f" fill="none" '
             'stroke="#cccccc" stroke-width="1"/>' % (W, H))
    o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
             'stroke="#e8a0a0" stroke-width="0.8" stroke-dasharray="4 3"/>'
             % (args.margin * args.ppm, args.margin * args.ppm,
                (bw - 2 * args.margin) * args.ppm, (bh - 2 * args.margin) * args.ppm))

    if args.travel and travels:
        d = " ".join("M%s L%s" % (pt(a), pt(b)) for a, b in travels)
        o.append('<path d="%s" fill="none" stroke="#7fb3ff" stroke-width="0.25" '
                 'opacity="0.35"/>' % d)

    for kind, colour, pts in regs:
        o.append('<polyline points="%s" fill="none" stroke="#999999" '
                 'stroke-width="%.2f"/>' % (" ".join(pt(p) for p in pts), args.sw))

    # Group by colour so the SVG stays small on big plans.
    bycol = {}
    for _, colour, pts in draws:
        bycol.setdefault(colour, []).append(pts)
    for colour, polys in bycol.items():
        d = " ".join("M" + " L".join(pt(p) for p in poly) for poly in polys)
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f" '
                 'stroke-linecap="round" stroke-linejoin="round"/>'
                 % (d, colour, args.sw))

    o.append('</svg>')
    with open(args.out, "w") as f:
        f.write("\n".join(o))

    xs = [p[0] for _, _, pts in draws for p in pts]
    ys = [p[1] for _, _, pts in draws for p in pts]
    print("strokes drawn   : %d  (%d points)" % (len(draws), sum(len(p) for _, _, p in draws)))
    print("colours         : %d  %s" % (len(bycol), ", ".join(sorted(bycol))))
    print("registration    : %d marks" % len(regs))
    print("travel moves    : %d%s" % (len(travels), "" if args.travel else " (not drawn)"))
    if xs:
        print("art extent      : X %.2f..%.2f  Y %.2f..%.2f" % (min(xs), max(xs), min(ys), max(ys)))
    print("wrote           : %s  (%.1f KB)" % (args.out, os.path.getsize(args.out) / 1024.0))
    print()
    print("Open it next to frida_simulator.html with the same plan loaded and compare:")
    print("  shape and position of each stroke, and the colour groups.")
    print("Note the preview is the PLANNED path. It cannot show pen pressure, line")
    print("weight, or anything mechanical -- only hardware tells you that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
