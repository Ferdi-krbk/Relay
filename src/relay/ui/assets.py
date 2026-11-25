"""Generate Relay Warm Ceramic & Obsidian brand assets (PNG + multi-size ICO)."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def build_brand_assets(output_dir: Path | None = None) -> Path:
    target = output_dir or ASSETS_DIR
    target.mkdir(parents=True, exist_ok=True)
    size = 512

    mark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mdraw = ImageDraw.Draw(mark)
    ink = (246, 244, 239, 255)      # Warm alabaster #F6F4EF
    vermilion = (229, 72, 38, 255)  # Signature vermilion #E54826
    pts = [(108, 278), (186, 278), (232, 146), (284, 366), (332, 234), (404, 234)]
    mdraw.line(pts, fill=ink, width=34, joint="curve")
    radius = 17
    for x, y in pts[:-1]:
        mdraw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=ink)
    ex, ey = pts[-1]
    mdraw.ellipse((ex - 24, ey - 24, ex + 24, ey + 24), fill=vermilion)
    mark.save(target / "relay-mark.png")

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    idraw = ImageDraw.Draw(icon)
    idraw.rounded_rectangle((16, 16, size - 16, size - 16), radius=128, fill=(22, 20, 18, 255))
    idraw.rounded_rectangle((20, 20, size - 20, size - 20), radius=124, outline=(56, 51, 45, 255), width=5)
    icon = Image.alpha_composite(icon, mark)
    icon.save(target / "relay-icon.png")
    ico_path = target / "relay.ico"
    icon.save(
        ico_path,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return ico_path


if __name__ == "__main__":
    print(f"Generated: {build_brand_assets()}")
