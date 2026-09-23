"""
Rigenera i raster dell'icona a partire dalle sorgenti SVG in static/.

Tool di sviluppo: non serve in esecuzione, gli asset prodotti sono versionati.
Dipendenze (non incluse in requirements.txt):

    pip install cairosvg pillow

Uso:

    python tools/generate_icons.py

Sorgenti:
  - static/icon.svg        icona principale (pile di monete + germoglio)
  - static/icon-small.svg  variante semplificata, leggibile a 16 e 32 px
"""

import io
import os

import cairosvg
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")

MAIN_SVG = os.path.join(STATIC, "icon.svg")
SMALL_SVG = os.path.join(STATIC, "icon-small.svg")


def render(svg_path: str, size: int, svg_text: str = None) -> Image.Image:
    kwargs = {"output_width": size, "output_height": size}
    if svg_text is None:
        png = cairosvg.svg2png(url=svg_path, **kwargs)
    else:
        png = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), **kwargs)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main():
    # PNG dell'icona principale (PWA, README, condivisioni)
    for size in (192, 512):
        out = os.path.join(STATIC, f"icon-{size}.png")
        render(MAIN_SVG, size).save(out)
        print(f"scritto {out}")

    # apple-touch-icon: iOS applica la propria maschera, quindi niente
    # angoli arrotondati nostri (altrimenti si vedrebbe un doppio bordo)
    with open(MAIN_SVG, encoding="utf-8") as fh:
        squared = fh.read().replace('rx="14"', 'rx="0"')
    out = os.path.join(STATIC, "apple-touch-icon.png")
    render(MAIN_SVG, 180, svg_text=squared).save(out)
    print(f"scritto {out}")

    # favicon.ico multi-risoluzione dalla variante semplificata
    frames = [render(SMALL_SVG, size) for size in (48, 32, 16)]
    out = os.path.join(STATIC, "favicon.ico")
    frames[0].save(out, format="ICO", sizes=[(48, 48), (32, 32), (16, 16)])
    print(f"scritto {out}")


if __name__ == "__main__":
    main()
