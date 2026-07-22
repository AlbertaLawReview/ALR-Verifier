"""Generate assets/splash.png — the PyInstaller boot splash."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(r"C:\Users\elias\Desktop\Martys Qote Verifier\ALR-Quote-Verifier")
GREEN_DARK = (16, 49, 36)     # #103124
GOLD = (232, 184, 75)         # #E8B84B
WHITE = (255, 255, 255)
MUTED = (181, 210, 194)       # #B5D2C2

W, H = 440, 240
img = Image.new("RGB", (W, H), GREEN_DARK)
d = ImageDraw.Draw(img)

# App icon, centered
icon = Image.open(ROOT / "assets" / "app_icon_64.png").convert("RGBA")
img.paste(icon, ((W - icon.width) // 2, 46), icon)

semibold = ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf", 26)
regular = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 13)

title = "ALR Quote Verifier"
tw = d.textlength(title, font=semibold)
d.text(((W - tw) / 2, 124), title, font=semibold, fill=WHITE)

sub = "Loading…"
sw = d.textlength(sub, font=regular)
d.text(((W - sw) / 2, 164), sub, font=regular, fill=MUTED)

# Gold brand rule along the bottom, echoing the app header
d.rectangle([0, H - 3, W, H], fill=GOLD)

out = ROOT / "assets" / "splash.png"
img.save(out)
print("saved", out, img.size)
