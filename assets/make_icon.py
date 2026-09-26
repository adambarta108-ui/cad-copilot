"""Draws assets/icon.ico (app and exe icon) and assets/icon.png. Run: python assets/make_icon.py"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024


def c(x, y):
    """Map the 32-unit grid used by the in-app logo to pixels."""
    return x * S / 32, y * S / 32


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=230, fill=(31, 111, 235, 255))

width = int(S * 2.2 / 32)
edges = [((9, 12.5), (16, 9)), ((16, 9), (23, 12.5)), ((23, 12.5), (23, 21.5)), ((23, 21.5), (16, 25)),
         ((16, 25), (9, 21.5)), ((9, 21.5), (9, 12.5)), ((9, 12.5), (16, 16)), ((16, 16), (23, 12.5)),
         ((16, 16), (16, 25))]
for a, b in edges:
    d.line([c(*a), c(*b)], fill="white", width=width)
for p in {pt for edge in edges for pt in edge}:  # round the joints
    x, y = c(*p)
    r = width / 2
    d.ellipse([x - r, y - r, x + r, y + r], fill="white")

img.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
print("wrote icon.ico and icon.png")
