#!/usr/bin/env python3
"""Convert a FRIDA simulator plan.json into pen-plotter G-code for a Klipper printer.

Canvas coords are METERS with the origin at the CENTER of the sheet, x = right,
z = up the page. Printer coords are MILLIMETERS with a configurable bed origin.
Canvas x -> printer X, canvas z -> printer Y.

Brush thickness is ignored: a pen has one width. Each distinct colour becomes a
pen-change stop rather than a palette dip, since a plotter has no palette.
"""
import argparse
import json
import math
import sys

MM = 1000.0  # meters -> mm


def _hex_of(color):
    """Normalise a colour to '#rrggbb'. Accepts hex strings or [r,g,b] 0-1 or 0-255."""
    if color is None:
        return "#000000"
    if isinstance(color, str):
        c = color.strip()
        return c if c.startswith("#") else "#" + c
    vals = list(color)[:3]
    if vals and max(vals) <= 1.0:
        vals = [v * 255.0 for v in vals]
    return "#" + "".join("%02x" % max(0, min(255, int(round(v)))) for v in vals)


def _bezier(x0, z0, rotation, length, bend, samples):
    """Sample the quadratic Bezier FRIDA defines by start/rotation/length/bend.

    The control point is offset by 2*bend so the curve's own midpoint sits at
    bend metres perpendicular to the chord, matching the schema's wording.
    """
    dx, dz = math.cos(rotation), math.sin(rotation)
    x2, z2 = x0 + length * dx, z0 + length * dz
    nx, nz = -dz, dx
    x1 = (x0 + x2) / 2.0 + 2.0 * bend * nx
    z1 = (z0 + z2) / 2.0 + 2.0 * bend * nz
    pts = []
    for i in range(samples + 1):
        t = i / float(samples)
        u = 1.0 - t
        pts.append((u * u * x0 + 2 * u * t * x1 + t * t * x2,
                    u * u * z0 + 2 * u * t * z1 + t * t * z2))
    return pts


def _parse_grid(spec):
    """Parse a 'RxC' tile grid spec into (rows, cols)."""
    try:
        r, c = spec.lower().split("x")
        r, c = int(r), int(c)
        if r < 1 or c < 1:
            raise ValueError
        return r, c
    except ValueError:
        sys.exit("error: --tile-grid wants RxC, e.g. 2x3 (got %r)" % spec)


def _reg_marks(cx, cy, half_x, half_y, size, pen_up, pen_down, z_feed, travel, draw):
    """L-shaped registration brackets at the four corners of a tile.

    Drawn by the pen itself, so they land in the same coordinate frame as the art --
    that is the point of self-drawn marks: they encode the machine's own idea of the
    tile, not the sheet's.
    """
    out = ["; --- registration marks (self-drawn) ---"]
    for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        px, py = cx + sx * half_x, cy + sy * half_y
        # two legs of the L, pointing inward
        out.append("G1 X%.3f Y%.3f F%d" % (px - sx * size, py, travel))
        out.append("G1 Z%.3f F%d" % (pen_down, z_feed))
        out.append("G1 X%.3f Y%.3f F%d" % (px, py, draw))
        out.append("G1 X%.3f Y%.3f F%d" % (px, py - sy * size, draw))
        out.append("G1 Z%.3f F%d" % (pen_up, z_feed))
    return out


def _taper_z(args, i, n):
    """Pen height at point i of n. Triangular taper: pen_down at the ends, deepest
    at the stroke's middle, emulating a brush pressing harder mid-stroke."""
    if args.z_taper <= 0.0 or n < 2:
        return args.pen_down
    frac = i / float(n - 1)
    return args.pen_down + args.z_taper * (1.0 - abs(2.0 * frac - 1.0))


def stroke_path(stroke, samples):
    """Return a stroke's point list in canvas metres, from either representation."""
    if stroke.get("path"):
        return [(float(p[0]), float(p[1])) for p in stroke["path"]]
    if "x_start" in stroke and "z_start" in stroke:
        return _bezier(float(stroke["x_start"]), float(stroke["z_start"]),
                       float(stroke.get("rotation", 0.0)),
                       float(stroke.get("length", 0.0)),
                       float(stroke.get("bend", 0.0)), samples)
    return []


def convert(plan, args):
    canvas = plan.get("canvas") or {}
    cw = float(canvas.get("width_m", 0.0)) * MM
    ch = float(canvas.get("height_m", 0.0)) * MM

    strokes = []
    for raw in plan.get("strokes", []):
        pts = stroke_path(raw, args.bezier_samples)
        if len(pts) < 1:
            continue
        strokes.append({"color": _hex_of(raw.get("color")),
                        "layer": int(raw.get("layer", 0)),
                        "pts": pts})
    if not strokes:
        sys.exit("error: plan contains no usable strokes")

    # A plotter swaps pens by hand, so drawing strokes in plan order can mean hundreds of
    # swaps. Grouping by colour collapses that to one stop per colour. Grouping is done
    # WITHIN each layer and never across layers: a layer is an ordering barrier, so no
    # stroke of layer N can be drawn after any stroke of layer N+1. Within a layer the
    # order changes, which matters only where same-layer strokes overlap.
    if args.group_colors:
        order = []
        for s in strokes:
            if s["color"] not in order:
                order.append(s["color"])
        # Stable sort on (layer, colour-first-seen) preserves layer sequence.
        strokes.sort(key=lambda s: (s["layer"], order.index(s["color"])))

    # Canvas-space extent actually used by the strokes (mm, origin = sheet centre).
    xs = [p[0] * MM for s in strokes for p in s["pts"]]
    zs = [p[1] * MM for s in strokes for p in s["pts"]]
    used = (min(xs), max(xs), min(zs), max(zs))

    # Printable area and where the sheet centre lands on the bed.
    cx = args.center_x if args.center_x is not None else (args.bed_x / 2.0)
    cy = args.center_y if args.center_y is not None else (args.bed_y / 2.0)

    # Tiling: carve the printable area into a grid and drop this frame into one cell.
    # Row 0 is the TOP row, so it gets the highest Y.
    tile = None
    if args.tile_grid:
        rows, cols = _parse_grid(args.tile_grid)
        if not (0 <= args.tile_index < rows * cols):
            sys.exit("error: --tile-index %d outside a %dx%d grid (0..%d)"
                     % (args.tile_index, rows, cols, rows * cols - 1))
        avail_w = args.bed_x - 2 * args.margin
        avail_h = args.bed_y - 2 * args.margin
        tw, th = avail_w / cols, avail_h / rows
        row, col = divmod(args.tile_index, cols)
        cx = args.margin + (col + 0.5) * tw
        cy = args.margin + (rows - 1 - row + 0.5) * th
        half_x = tw / 2.0 - args.tile_gutter
        half_y = th / 2.0 - args.tile_gutter
        if half_x <= 0 or half_y <= 0:
            sys.exit("error: gutter %.1fmm too large for a %.1fx%.1fmm tile"
                     % (args.tile_gutter, tw, th))
        tile = {"rows": rows, "cols": cols, "row": row, "col": col,
                "w": tw, "h": th, "cx": cx, "cy": cy,
                "half_x": half_x, "half_y": half_y}
    else:
        half_x = min(cx - args.margin, args.bed_x - args.margin - cx)
        half_y = min(cy - args.margin, args.bed_y - args.margin - cy)

    scale = args.scale
    if args.fit or tile is not None:
        need_x = max(abs(used[0]), abs(used[1]))
        need_y = max(abs(used[2]), abs(used[3]))
        fits = [half_x / need_x if need_x > 0 else 1.0,
                half_y / need_y if need_y > 0 else 1.0]
        scale = min(args.scale, min(fits))

    def to_bed(p):
        return (cx + p[0] * MM * scale, cy + p[1] * MM * scale)

    bx = [to_bed(p)[0] for s in strokes for p in s["pts"]]
    by = [to_bed(p)[1] for s in strokes for p in s["pts"]]
    bounds = (min(bx), max(bx), min(by), max(by))

    out = []
    w = out.append
    w("; FRIDA plan -> pen plotter G-code")
    w("; canvas %.1f x %.1f mm, strokes=%d, scale=%.4f" % (cw, ch, len(strokes), scale))
    if tile is not None:
        w("; tile %d of %dx%d (row %d, col %d), cell %.1f x %.1f mm, gutter %.1f mm"
          % (args.tile_index, tile["rows"], tile["cols"], tile["row"], tile["col"],
             tile["w"], tile["h"], args.tile_gutter))
    w("; sheet centre mapped to X%.2f Y%.2f" % (cx, cy))
    w("; bed extent used: X %.2f..%.2f  Y %.2f..%.2f" % bounds)
    w("G21")
    w("G90")
    if args.no_home:
        w("; G28 skipped (--no-home): caller already homed, saves a full re-home per frame")
    else:
        w("G28")
    w("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))

    if args.reg_marks:
        for line in _reg_marks(cx, cy, half_x, half_y, args.reg_size,
                               args.pen_up, args.pen_down, args.z_feed,
                               args.travel_feed, args.draw_feed):
            w(line)

    pen = None
    for i, s in enumerate(strokes):
        if s["color"] != pen:
            if pen is not None and not args.no_pen_change:
                w("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
                w("; ---- pen change -> %s ----" % s["color"])
                w("M117 Pen %s" % s["color"])
                if args.pause:
                    w("PAUSE")
            pen = s["color"]
        w("; stroke %d/%d  layer %d  colour %s  points %d"
          % (i + 1, len(strokes), s["layer"], s["color"], len(s["pts"])))
        n = len(s["pts"])
        x, y = to_bed(s["pts"][0])
        w("G1 X%.3f Y%.3f F%d" % (x, y, args.travel_feed))
        w("G1 Z%.3f F%d" % (_taper_z(args, 0, n), args.z_feed))
        for j, p in enumerate(s["pts"][1:], start=1):
            x, y = to_bed(p)
            if args.z_taper > 0.0:
                # Z on a drawing move makes Klipper clamp the whole move to
                # max_z_velocity * (move_d/|dz|) and max_z_accel * (move_d/|dz|).
                w("G1 X%.3f Y%.3f Z%.4f F%d"
                  % (x, y, _taper_z(args, j, n), args.draw_feed))
            else:
                w("G1 X%.3f Y%.3f F%d" % (x, y, args.draw_feed))
        w("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))

    w("G1 Z%.3f F%d" % (args.pen_up, args.z_feed))
    w("M400")
    w("M117 Plot complete")

    stats = {
        "strokes": len(strokes),
        "points": sum(len(s["pts"]) for s in strokes),
        "colors": sorted({s["color"] for s in strokes}),
        "canvas_mm": (cw, ch),
        "scale": scale,
        "bounds": bounds,
        "limits": (args.bed_x, args.bed_y),
        "margin": args.margin,
        # What we believe we are drawing, in emission order and bed coordinates.
        # verify_roundtrip.py diffs this against the G-code read back from disk.
        "emitted": [(s["color"], s["layer"], [to_bed(p) for p in s["pts"]])
                    for s in strokes],
    }
    return "\n".join(out) + "\n", stats


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", help="input plan.json")
    ap.add_argument("-o", "--out", required=True, help="output .gcode")
    ap.add_argument("--bed-x", type=float, default=235.0, dest="bed_x")
    ap.add_argument("--bed-y", type=float, default=235.0, dest="bed_y")
    ap.add_argument("--center-x", type=float, default=None, dest="center_x",
                    help="bed X the sheet centre maps to (default: bed-x/2)")
    ap.add_argument("--center-y", type=float, default=None, dest="center_y")
    ap.add_argument("--margin", type=float, default=5.0,
                    help="keep-out margin from bed edges, mm (default 5)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="uniform scale applied to canvas coords (default 1.0)")
    ap.add_argument("--fit", action="store_true",
                    help="shrink uniformly until the plan fits the bed")
    ap.add_argument("--pen-up", type=float, default=5.0, dest="pen_up")
    ap.add_argument("--pen-down", type=float, default=0.2, dest="pen_down")
    ap.add_argument("--draw-feed", type=int, default=1800, dest="draw_feed",
                    help="mm/min while drawing (default 1800)")
    ap.add_argument("--travel-feed", type=int, default=6000, dest="travel_feed")
    ap.add_argument("--z-feed", type=int, default=600, dest="z_feed")
    ap.add_argument("--bezier-samples", type=int, default=16, dest="bezier_samples")
    ap.add_argument("--pause", action="store_true",
                    help="emit PAUSE at pen changes (needs [pause_resume])")
    ap.add_argument("--no-pen-change", action="store_true", dest="no_pen_change")
    ap.add_argument("--tile-grid", default=None, dest="tile_grid",
                    help="tile the sheet as RxC (e.g. 2x3) and place this frame in one cell")
    ap.add_argument("--tile-index", type=int, default=0, dest="tile_index",
                    help="which cell, row-major from the TOP-LEFT (default 0)")
    ap.add_argument("--tile-gutter", type=float, default=3.0, dest="tile_gutter",
                    help="blank margin inside each tile, mm (default 3)")
    ap.add_argument("--reg-marks", action="store_true", dest="reg_marks",
                    help="pen-draw L brackets at the tile/page corners")
    ap.add_argument("--reg-size", type=float, default=8.0, dest="reg_size",
                    help="registration bracket leg length, mm (default 8)")
    ap.add_argument("--no-home", action="store_true", dest="no_home",
                    help="omit G28 (for frames 2..N of a sequence, already homed)")
    ap.add_argument("--z-taper", type=float, default=0.0, dest="z_taper",
                    help="vary pen Z within each stroke by this many mm (triangular, "
                         "deepest mid-stroke). 0 = flat strokes, Z only between strokes")
    ap.add_argument("--group-colors", action="store_true", dest="group_colors",
                    help="reorder strokes to group by colour: one pen stop per colour "
                         "instead of one per colour change (changes overlap layering)")
    return ap


def main():
    args = build_parser().parse_args()

    with open(args.plan) as f:
        plan = json.load(f)
    gcode, st = convert(plan, args)
    with open(args.out, "w") as f:
        f.write(gcode)

    lo_x, hi_x, lo_y, hi_y = st["bounds"]
    bed_x, bed_y = st["limits"]
    m = st["margin"]
    print("strokes      : %d  (%d points)" % (st["strokes"], st["points"]))
    print("colours      : %d  %s" % (len(st["colors"]), ", ".join(st["colors"])))
    print("canvas       : %.1f x %.1f mm" % st["canvas_mm"])
    print("scale        : %.4f" % st["scale"])
    print("bed extent   : X %.2f..%.2f   Y %.2f..%.2f" % (lo_x, hi_x, lo_y, hi_y))
    print("bed limits   : X %.1f..%.1f   Y %.1f..%.1f (margin %.1f)"
          % (m, bed_x - m, m, bed_y - m, m))

    bad = (lo_x < m or hi_x > bed_x - m or lo_y < m or hi_y > bed_y - m)
    if bad:
        need = max((hi_x - lo_x) / (bed_x - 2 * m), (hi_y - lo_y) / (bed_y - 2 * m))
        print("VERDICT      : DOES NOT FIT - exceeds the printable area")
        print("               rerun with --fit, or --scale %.3f" % (st["scale"] / need))
    else:
        print("VERDICT      : fits the printable area")
    print("wrote        : %s" % args.out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
