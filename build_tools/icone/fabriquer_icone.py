"""Fabrique les icônes de l'application à partir d'un dessin SVG (€ et vagues sur fond turquoise) :
TripCompta.icns (Mac), TripCompta.ico (Windows) et TripCompta.png (1024 px).
À relancer seulement si on change le dessin (sur Mac : iconutil) ; les fichiers produits sont dans le dépôt.
    .venv/bin/python build_tools/icone/fabriquer_icone.py"""
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

# dessin sur une grille de 24 ; fond : carré arrondi
ART = """
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2bb09b"/><stop offset="1" stop-color="#136b5e"/>
    </linearGradient>
    <linearGradient id="hl" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fff" stop-opacity=".18"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect width="24" height="24" rx="5.4" fill="url(#g)"/>
  <rect width="24" height="24" rx="5.4" fill="url(#hl)"/>
  <g fill="none" stroke="#fff" stroke-linecap="round" stroke-linejoin="round">
    <g stroke-width="1.55" transform="translate(0.5 -0.6)">
      <path d="M15.4 5.4A4.3 4.3 0 1 0 15.4 12.2"/>
      <path d="M7 8h5.6M7 9.9h5.6"/>
    </g>
    <path stroke-width="1.35" d="M4.6 16.6c1.2 0 1.2-.9 2.45-.9s1.2.9 2.45.9 1.2-.9 2.45-.9 1.2.9 2.45.9 1.2-.9 2.45-.9 1.2.9 2.45.9"/>
    <path stroke-width="1.35" stroke-opacity=".6" d="M4.6 19.2c1.2 0 1.2-.9 2.45-.9s1.2.9 2.45.9 1.2-.9 2.45-.9 1.2.9 2.45.9 1.2-.9 2.45-.9 1.2.9 2.45.9"/>
  </g>"""


def render(pg, size: int, margin: float, shadow: bool) -> Image.Image:
    """Le dessin dans un carré de `size` px, avec une marge (fraction) autour, comme les icônes macOS."""
    inner = size * (1 - 2 * margin)
    off = size * margin
    filt = (f'<filter id="s" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="{size * .012}" '
            f'stdDeviation="{size * .01}" flood-opacity=".28"/></filter>') if shadow else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">{filt}'
           f'<g transform="translate({off} {off}) scale({inner / 24})" {"filter=url(#s)" if shadow else ""}>'
           f'{ART}</g></svg>')
    pg.set_viewport_size({"width": size, "height": size})
    pg.set_content(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
    return Image.open(io.BytesIO(pg.screenshot(omit_background=True, clip=dict(x=0, y=0, width=size, height=size))))


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        mac = {n: render(pg, n, 100 / 1024, n >= 64) for n in (16, 32, 64, 128, 256, 512, 1024)}   # grille Apple
        win = {n: render(pg, n, 0.0, False) for n in (16, 24, 32, 48, 64, 128, 256)}
        b.close()
    mac[1024].save(HERE / "TripCompta.png")
    win[256].save(HERE / "TripCompta.ico", sizes=[(n, n) for n in win], append_images=list(win.values()))
    with tempfile.TemporaryDirectory() as d:
        iset = Path(d) / "TripCompta.iconset"
        iset.mkdir()
        for n in (16, 32, 128, 256, 512):
            mac[n].save(iset / f"icon_{n}x{n}.png")
            mac[n * 2].save(iset / f"icon_{n}x{n}@2x.png")
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(HERE / "TripCompta.icns")], check=True)
        else:
            print("iconutil absent (pas sur Mac) : TripCompta.icns non refait")
    print("Icônes écrites dans", HERE)


if __name__ == "__main__":
    main()
