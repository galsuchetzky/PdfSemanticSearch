"""Render the product icon: a multi-size Windows .ico, a PNG, and optionally a 1024px PNG for macOS.

Usage: python install/make_icon.py [path-for-1024px-png]
"""
from pathlib import Path
import sys
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
scale = 4
size = 256
im = Image.new("RGBA", (size * scale, size * scale))
px = im.load()
for y in range(im.height):
    for x in range(im.width):
        t = (x + y) / (im.width + im.height)
        px[x, y] = (int(18 + 0 * t), int(56 + 70 * t), int(74 + 57 * t), 255)
d = ImageDraw.Draw(im)
S = lambda n: int(n * scale)
mask = Image.new("L", im.size)
ImageDraw.Draw(mask).rounded_rectangle((S(8), S(8), S(248), S(248)), radius=S(54), fill=255)
im.putalpha(mask)
d = ImageDraw.Draw(im)
d.polygon([(S(65), S(43)), (S(141), S(43)), (S(180), S(82)), (S(180), S(177)),
           (S(166), S(191)), (S(65), S(191)), (S(51), S(177)), (S(51), S(57))], fill="#F8FBF8")
d.polygon([(S(141), S(43)), (S(180), S(82)), (S(152), S(82)), (S(141), S(71))], fill="#D8E9E5")
d.line([(S(77), S(103)), (S(130), S(103))], fill="#6D9292", width=S(8))
d.line([(S(77), S(120)), (S(119), S(120))], fill="#6D9292", width=S(8))
d.ellipse((S(102), S(125), S(154), S(177)), outline="#087E83", width=S(10))
d.line([(S(147), S(170)), (S(169), S(192))], fill="#F5B45C", width=S(14))
d.ellipse((S(120), S(143), S(136), S(159)), fill="#D8F0E8")
if len(sys.argv) > 1:
    im.save(sys.argv[1])  # full 1024px render for the macOS .icns
im = im.resize((size, size), Image.Resampling.LANCZOS)
im.save(ROOT / "assets" / "acrobat-semantic-search.ico", format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
im.save(ROOT / "assets" / "acrobat-semantic-search.png")
