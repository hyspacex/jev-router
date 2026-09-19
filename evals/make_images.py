"""Generate the small PNGs the image cases use.

The images are drawn here rather than committed, so the repository stays text
only and anyone can regenerate the exact same bytes. Everything is original:
a bar chart, a table that looks like a screenshot, a digit string in a
handwriting font, and a terminal traceback. Each one carries the ground truth
the grading spec checks against, so no human has to read the picture.

    uv run --with pillow python evals/make_images.py --out /tmp/imgs

Needs Pillow, which is not a dependency of the router. Run it with
`uv run --with pillow`.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

# Drawn at 2x and downscaled, so the text has clean edges at the size a model
# actually sees.
SCALE = 2

# macOS ships these. The first one that exists is used; if none do, Pillow's
# bitmap default font is used and the images are still legible.
FONT_CANDIDATES = {
    "sans": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "sans_bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "hand": [
        "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
        "/System/Library/Fonts/Supplemental/Chalkduster.ttf",
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ],
}


def _font(kind: str, size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size * SCALE)
            except OSError:
                continue
    return ImageFont.load_default()


def _canvas(width: int, height: int, colour: str = "white"):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width * SCALE, height * SCALE), colour)
    return img, ImageDraw.Draw(img)


def _finish(img, path: Path) -> Path:
    from PIL import Image

    img = img.resize(
        (img.width // SCALE, img.height // SCALE), Image.Resampling.LANCZOS
    )
    img.save(path, "PNG", optimize=True)
    return path


# --- the four images ----------------------------------------------------

BARS = [
    ("Rotterdam", 418),
    ("Gdansk", 265),
    ("Bilbao", 372),
    ("Trieste", 149),
    ("Aarhus", 203),
]


def bar_chart(path: Path) -> dict:
    """A labelled bar chart. Every bar prints its own value."""
    img, d = _canvas(520, 340)
    title = _font("sans_bold", 15)
    label = _font("sans", 12)
    d.text((18 * SCALE, 14 * SCALE), "Containers handled, Q3 (thousands)",
           fill="black", font=title)
    top, bottom, left = 56, 292, 70
    d.line([(left * SCALE, bottom * SCALE), (500 * SCALE, bottom * SCALE)],
           fill="black", width=2)
    d.line([(left * SCALE, top * SCALE), (left * SCALE, bottom * SCALE)],
           fill="black", width=2)
    step = 84
    for i, (name, value) in enumerate(BARS):
        x = left + 22 + i * step
        height = int((value / 450) * (bottom - top - 16))
        d.rectangle(
            [(x * SCALE, (bottom - height) * SCALE), ((x + 46) * SCALE, bottom * SCALE)],
            fill=(58, 92, 148),
        )
        d.text(((x + 4) * SCALE, (bottom - height - 18) * SCALE), str(value),
               fill="black", font=label)
        d.text(((x - 4) * SCALE, (bottom + 8) * SCALE), name, fill="black", font=label)
    ordered = sorted(BARS, key=lambda b: b[1])
    return {
        "path": str(_finish(img, path)),
        "truth": {
            "highest": ordered[-1][0],
            "highest_value": ordered[-1][1],
            "lowest": ordered[0][0],
            "sum_two_smallest": ordered[0][1] + ordered[1][1],
        },
    }


ROWS = [
    ("INV-2041", "Halvorsen AS", "2026-08-14", "1,240.00", "Paid"),
    ("INV-2042", "Kestrel Ltd", "2026-08-19", "3,815.50", "Overdue"),
    ("INV-2043", "Pelagic BV", "2026-08-27", "902.25", "Paid"),
    ("INV-2044", "Norquist Oy", "2026-09-02", "5,460.00", "Overdue"),
    ("INV-2045", "Cadence SRL", "2026-09-06", "1,118.75", "Draft"),
]
HEADERS = ("Invoice", "Customer", "Issued", "Amount EUR", "Status")
COL_X = (18, 108, 236, 330, 440)


def table_screenshot(path: Path) -> dict:
    """A table drawn to look like a screenshot of an admin page."""
    img, d = _canvas(540, 210, colour=(246, 247, 249))
    head = _font("sans_bold", 12)
    body = _font("sans", 12)
    d.rectangle([(0, 0), (540 * SCALE, 30 * SCALE)], fill=(228, 231, 236))
    for x, text in zip(COL_X, HEADERS):
        d.text((x * SCALE, 9 * SCALE), text, fill=(40, 44, 52), font=head)
    y = 38
    for i, row in enumerate(ROWS):
        if i % 2:
            d.rectangle([(0, y * SCALE - 4), (540 * SCALE, (y + 30) * SCALE - 4)],
                        fill=(255, 255, 255))
        for x, text in zip(COL_X, row):
            d.text((x * SCALE, y * SCALE), text, fill=(25, 27, 31), font=body)
        y += 30
    overdue = [r for r in ROWS if r[4] == "Overdue"]
    total = sum(float(r[3].replace(",", "")) for r in overdue)
    return {
        "path": str(_finish(img, path)),
        "truth": {
            "overdue_count": len(overdue),
            "overdue_total": round(total, 2),
            "overdue_invoices": [r[0] for r in overdue],
        },
    }


DIGITS = "409" + "271" + "86"


def handwritten_digits(path: Path) -> dict:
    """A digit string in a handwriting font, on lined paper."""
    img, d = _canvas(420, 130, colour=(253, 252, 246))
    for y in (40, 78, 116):
        d.line([(0, y * SCALE), (420 * SCALE, y * SCALE)], fill=(206, 214, 226), width=1)
    d.text((26 * SCALE, 44 * SCALE), " ".join(DIGITS), fill=(28, 40, 88),
           font=_font("hand", 30))
    return {"path": str(_finish(img, path)), "truth": {"digits": DIGITS}}


TRACEBACK = [
    "$ uv run python -m ledger.jobs.settle",
    "Traceback (most recent call last):",
    '  File "/srv/ledger/jobs/settle.py", line 118, in <module>',
    "    main()",
    '  File "/srv/ledger/jobs/settle.py", line 74, in main',
    "    batch = build_batch(rows, cutoff=cutoff)",
    '  File "/srv/ledger/batching.py", line 219, in build_batch',
    "    return Batch(total=sum(r.amount for r in rows) / len(rows))",
    "ZeroDivisionError: division by zero",
]


def terminal_error(path: Path) -> dict:
    """A terminal window with a Python traceback."""
    img, d = _canvas(640, 210, colour=(24, 26, 30))
    font = _font("mono", 11)
    y = 14
    for line in TRACEBACK:
        colour = (220, 92, 92) if line.startswith("ZeroDivisionError") else (214, 218, 224)
        d.text((14 * SCALE, y * SCALE), line, fill=colour, font=font)
        y += 21
    return {
        "path": str(_finish(img, path)),
        "truth": {
            "exception": "ZeroDivisionError",
            "line": 219,
            "file": "batching.py",
        },
    }


BUILDERS = {
    "bar-chart": bar_chart,
    "table-screenshot": table_screenshot,
    "handwritten-digits": handwritten_digits,
    "terminal-error": terminal_error,
}


def build_all(out_dir: Path) -> dict[str, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        name: fn(out_dir / f"{name}.png") for name, fn in sorted(BUILDERS.items())
    }


def data_url(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True, help="directory to write the PNGs into")
    args = p.parse_args()
    built = build_all(Path(args.out))
    print(json.dumps(built, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
