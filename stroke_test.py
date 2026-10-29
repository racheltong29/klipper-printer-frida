import math
import requests

PRINTER = "http://10.22.15.19"   # your printer's IP (Menu > Tune > Show IP)

def gcode(script):
    r = requests.post(f"{PRINTER}/printer/gcode/script",
                      json={"script": script}, timeout=600)
    r.raise_for_status()

def stroke(points, feed=1800):
    """points: list of (x, y, z). z=0 touches paper, negative presses harder."""
    x0, y0, z0 = points[0]
    lines = ["G90", "G1 Z5 F600",
             f"G1 X{x0:.2f} Y{y0:.2f} F6000",
             f"G1 Z{z0:.2f} F600"]
    for x, y, z in points[1:]:
        lines.append(f"G1 X{x:.2f} Y{y:.2f} Z{z:.2f} F{feed}")
    lines.append("G1 Z5 F600")
    gcode("\n".join(lines))          # whole stroke in one request

# Ten curved strokes; pressure swells in the middle and lightens at the ends
for i in range(10):
    y = 120 + i * 10
    pts = []
    for k in range(31):
        t = k / 30
        x = 100 + 150 * t
        yy = y + 5 * math.sin(t * math.pi * 2)
        z = 0.2 - 1.5 * math.sin(t * math.pi)   # 0.2 -> -1.3 -> 0.2
        pts.append((x, yy, z))
    stroke(pts)
    print(f"stroke {i + 1} done")

gcode("G1 Z10 F600")