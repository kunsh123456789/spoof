"""Generate assets/icon.png (1024x1024): a map pin with signal arcs on a blue gradient.  Needs Pillow.

    python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE, SS = 1024, 2           # final size, supersampling factor
W = SIZE * SS
OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.png"


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def main() -> None:
    top, bottom = (58, 160, 255), (92, 74, 255)
    bg = Image.new("RGB", (W, W))
    px = ImageDraw.Draw(bg)
    for y in range(W):
        px.line([(0, y), (W, y)], fill=lerp(top, bottom, y / W))
    mask = Image.new("L", (W, W), 0)                                     # rounded-square app shape
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.225), fill=255)

    cx, cy, r = W // 2, int(W * 0.43), int(W * 0.215)
    pin = Image.new("L", (W, W), 0)
    d = ImageDraw.Draw(pin)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    d.polygon([(cx - int(r * 0.86), cy + int(r * 0.5)), (cx + int(r * 0.86), cy + int(r * 0.5)), (cx, int(W * 0.80))], fill=255)
    d.ellipse([cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2], fill=0)   # the hole

    shadow = Image.new("L", (W, W), 0)                                   # soft ground shadow under the tip
    ImageDraw.Draw(shadow).ellipse([cx - int(W * 0.13), int(W * 0.79), cx + int(W * 0.13), int(W * 0.835)], fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(W * 0.012))

    arcs = Image.new("L", (W, W), 0)                                     # "signal" arcs above the pin
    da = ImageDraw.Draw(arcs)
    for k, (rad, alpha) in enumerate(((0.33, 200), (0.43, 120))):
        R = int(W * rad)
        da.arc([cx - R, cy - R, cx + R, cy + R], start=215, end=325, fill=alpha, width=int(W * 0.03))

    img = bg.copy()
    img.paste((20, 20, 60), mask=shadow)
    img.paste((255, 255, 255), mask=arcs)
    img.paste((255, 255, 255), mask=pin)
    img.putalpha(ImageChops.multiply(mask, Image.new("L", (W, W), 255)))
    OUT.parent.mkdir(exist_ok=True)
    img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
