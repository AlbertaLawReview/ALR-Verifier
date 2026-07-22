#!/usr/bin/env python3
"""Generate the app icon: navy rounded tile, white serif quotation marks,
gold verification check. Writes app_icon.ico (multi-size) and PNGs for the
GUI header. Re-run after design tweaks; outputs are committed.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
S = 1024  # supersampled master

NAVY_TOP = (16, 49, 36)      # #103124 — ALR dark green
NAVY_BOTTOM = (31, 92, 64)   # #1F5C40
GOLD = (232, 184, 75)        # #E8B84B
GOLD_DARK = (176, 132, 40)
WHITE = (255, 255, 255)


def rounded_tile() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # vertical gradient
    grad = Image.new("RGBA", (1, S))
    for y in range(S):
        t = y / (S - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOTTOM))
        grad.putpixel((0, y), c + (255,))
    grad = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    # subtle top highlight
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        [int(S * 0.04), int(S * 0.03), int(S * 0.96), int(S * 0.30)],
        radius=int(S * 0.16), fill=(255, 255, 255, 18))
    return Image.alpha_composite(img, hl)


def draw_quotes(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("georgiab.ttf", int(S * 0.95))
    text = "“"  # left double quotation mark
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = int(S * 0.30) - bbox[0] - w // 2
    y = int(S * 0.38) - bbox[1] - h // 2
    d.text((x + int(S * 0.012), y + int(S * 0.016)), text, font=font, fill=(0, 0, 0, 60))
    d.text((x, y), text, font=font, fill=WHITE)


def draw_check(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    # anchor points of the check, lower-right quadrant
    p1 = (int(S * 0.46), int(S * 0.66))
    p2 = (int(S * 0.62), int(S * 0.82))
    p3 = (int(S * 0.90), int(S * 0.40))
    wmain = int(S * 0.085)
    # soft shadow
    off = int(S * 0.014)
    for a, b in ((p1, p2), (p2, p3)):
        d.line([(a[0] + off, a[1] + off), (b[0] + off, b[1] + off)],
               fill=(0, 0, 0, 70), width=wmain)
    for pt in (p1, p2, p3):
        d.ellipse([pt[0] - wmain // 2 + off, pt[1] - wmain // 2 + off,
                   pt[0] + wmain // 2 + off, pt[1] + wmain // 2 + off], fill=(0, 0, 0, 0))
    # outline pass (darker gold) then main stroke, round caps via end circles
    for color, w in ((GOLD_DARK, wmain + int(S * 0.024)), (GOLD, wmain)):
        d.line([p1, p2, p3], fill=color, width=w, joint="curve")
        for pt in (p1, p3):
            d.ellipse([pt[0] - w // 2, pt[1] - w // 2, pt[0] + w // 2, pt[1] + w // 2],
                      fill=color)


def main() -> None:
    img = rounded_tile()
    draw_quotes(img)
    draw_check(img)

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    renders = {n: img.resize((n, n), Image.LANCZOS) for n in sizes}
    renders[256].save(HERE / "app_icon_256.png")
    renders[64].save(HERE / "app_icon_64.png")
    renders[32].save(HERE / "app_icon_32.png")
    renders[256].save(
        HERE / "app_icon.ico",
        format="ICO",
        sizes=[(n, n) for n in sizes],
    )
    print("wrote app_icon.ico +", ", ".join(f"app_icon_{n}.png" for n in (256, 64, 32)))


if __name__ == "__main__":
    main()
