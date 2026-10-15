"""Generate the README hero diagram: pipeline + verification report.

Renders with PIL (no browser needed) at 2x for crispness.
"""
from PIL import Image, ImageDraw, ImageFont
import glob

W, H = 1280, 560
SCALE = 2
img = Image.new("RGB", (W*SCALE, H*SCALE), "#0d1117")
d = ImageDraw.Draw(img)

def font(size, bold=False):
    paths = glob.glob("/usr/share/fonts/**/DejaVuSans*Bold*.ttf" if bold else "/usr/share/fonts/**/DejaVuSans.ttf", recursive=True)
    return ImageFont.truetype(paths[0], size*SCALE) if paths else ImageFont.load_default()

f_title = font(30, True)
f_box   = font(17, True)
f_sub   = font(13)
f_mono  = font(13)
f_green = font(15, True)

# Title
d.text((40*SCALE, 30*SCALE), "Every number computed. Every model verified.", fill="#f0f6fc", font=f_title)
d.text((40*SCALE, 78*SCALE), "OpenCabinet — a parametric wardrobe generator that refuses to emit structurally wrong models", fill="#8b949e", font=f_sub)

# Pipeline boxes
steps = [
    ("spec.json", "one file,\nplain JSON", "#30363d"),
    ("SOLVE", "deterministic\nengine", "#1f6feb"),
    ("board_graph", "exact bboxes\n+ groove cuts", "#30363d"),
    ("EXPORT", "FreeCAD\nFCStd + BOM", "#1f6feb"),
    ("VERIFY", "exact solid\ncommon()", "#238636"),
]
bx, by, bw, bh, gap = 40, 150, 200, 110, 42
for i, (t, s, c) in enumerate(steps):
    x = (bx + i*(bw+gap))*SCALE
    d.rounded_rectangle([x, by*SCALE, x+bw*SCALE, (by+bh)*SCALE], radius=10*SCALE, fill=c, outline="#444c56", width=SCALE)
    d.text((x+16*SCALE, (by+14)*SCALE), t, fill="#ffffff", font=f_box)
    d.multiline_text((x+16*SCALE, (by+48)*SCALE), s, fill="#9da7b3", font=f_sub, spacing=4*SCALE)
    if i < len(steps)-1:
        ax = x + bw*SCALE + 6*SCALE
        d.line([ax, (by+bh/2)*SCALE, ax+gap*SCALE-12*SCALE, (by+bh/2)*SCALE], fill="#58a6ff", width=3*SCALE)
        d.polygon([(ax+gap*SCALE-12*SCALE, (by+bh/2)*SCALE), (ax+gap*SCALE-22*SCALE, (by+bh/2-8)*SCALE), (ax+gap*SCALE-22*SCALE, (by+bh/2+8)*SCALE)], fill="#58a6ff")

# Reject gate below VERIFY
gx = (bx + 4*(bw+gap))*SCALE
d.text(((gx+bw/2-58)*SCALE, (by+bh+14)*SCALE), "fail → not emitted", fill="#f85149", font=f_sub)

# Verification report panel (the selling point)
py = 320
d.rounded_rectangle([40*SCALE, py*SCALE, 700*SCALE, (py+190)*SCALE], radius=12*SCALE, fill="#161b22", outline="#30363d", width=SCALE)
d.text((60*SCALE, (py+16)*SCALE), "verify report — shipped with every model", fill="#8b949e", font=f_sub)
report = [
    ("boards",            "25 == graph nodes", "#7ee787"),
    ("interference",      "[]  exact solid common(), cut-exempt", "#7ee787"),
    ("extents x,y,z",     "[0,1200] [2,580] [0,2400]", "#7ee787"),
    ("groove volume",     "== depth x width x length (exact)", "#7ee787"),
    ("bay width closure", "sum + t*(bays+1) == width", "#7ee787"),
]
for i, (k, v, c) in enumerate(report):
    yy = (py+46+i*27)
    d.text((60*SCALE, yy*SCALE), f"{k:<20}", fill="#e6edf3", font=f_mono)
    d.text((280*SCALE, yy*SCALE), v, fill=c, font=f_mono)

# Stats panel right
sx = 740
d.rounded_rectangle([sx*SCALE, py*SCALE, 1240*SCALE, (py+190)*SCALE], radius=12*SCALE, fill="#161b22", outline="#30363d", width=SCALE)
d.text(((sx+20)*SCALE, (py+16)*SCALE), "0", fill="#f85149", font=font(44, True))
d.text(((sx+95)*SCALE, (py+34)*SCALE), "silent structural errors tolerated", fill="#8b949e", font=f_sub)
checks = [
    "interference: exact solids, not AABBs",
    "dimension chains closed to the last decimal",
    "back-panel grooves cut into real solids",
    "ambiguous specs rejected with reasons",
    "deterministic: same spec, same boards",
]
for i, c in enumerate(checks):
    d.text(((sx+20)*SCALE, (py+96+i*20)*SCALE), "✓  "+c, fill="#7ee787", font=f_sub)

img = img.resize((W, H), Image.LANCZOS)
img.save("/home/ming/opencabinet/docs/hero.png", optimize=True)
print("saved", img.size)
