#!/usr/bin/env python3
"""
sortera_bilder.py
==================

Sorterar bilder från den senaste veckan (eller valfritt antal dagar) baserat
på fyra centrala kategorier:

  1. Byggarbetare   - det syns en byggarbetare (person i arbete) på bilden
  2. Tak             - bilden visar bara ett tak, inga personer av intresse
  3. Takrelaterat    - annat takrelaterat: industrilokal, företagslogga, etc.
  4. Ovrigt          - inget av ovanstående / går inte att avgöra

Skriptet döper om varje matchande fil genom att lägga till en kategori-tagg
i hakparentes främst i filnamnet, t.ex.:

    IMG_0231.jpg  ->  [Byggarbetare] IMG_0231.jpg

Bilderna flyttas INTE - de ligger kvar i samma mapp, bara namnet ändras.

Användning
----------
    export ANTHROPIC_API_KEY="din-api-nyckel"
    python3 sortera_bilder.py --folder /path/till/bilder

Se --help för fler flaggor (antal dagar, dry-run, rekursiv sökning, m.m.)
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import mimetypes
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
except ImportError:
    print("Paketet 'Pillow' saknas. Installera med: pip install Pillow", file=sys.stderr)
    sys.exit(1)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

try:
    import anthropic
except ImportError:
    print(
        "Paketet 'anthropic' saknas. Installera med: pip install anthropic",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Kategorier
# ---------------------------------------------------------------------------

CATEGORIES = {
    "Byggarbetare": "Det syns tydligt en byggarbetare (en person som arbetar, t.ex. i "
    "arbetskläder/hjälm, på ett tak eller en byggarbetsplats) på bilden.",
    "Tak": "Bilden visar huvudsakligen bara ett tak (takyta, tegel, plåt, skorsten "
    "etc.) utan personer och utan annat anmärkningsvärt i fokus.",
    "Takrelaterat": "Bilden är takrelaterad men varken bara ett tak eller en "
    "byggarbetare, t.ex. en industrilokal/industribyggnad, ett företags logga, "
    "ett fordon eller material märkt med företagsnamn.",
    "Ovrigt": "Inget av ovanstående stämmer, eller det går inte att avgöra vad "
    "bilden föreställer.",
}

VALID_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif",
}

DEFAULT_MODEL = "claude-sonnet-5"
MAX_DIMENSION = 1568  # rekommenderad maxstorlek för Claude vision


@dataclass
class Result:
    path: Path
    category: Optional[str]
    reasoning: str
    error: Optional[str] = None


def build_prompt() -> str:
    lines = [
        "Du klassificerar ett foto i EXAKT en av följande kategorier:",
        "",
    ]
    for name, desc in CATEGORIES.items():
        lines.append(f'- "{name}": {desc}')
    lines.append("")
    lines.append(
        "Svara ENDAST med kompakt JSON på formen "
        '{"kategori": "<en av namnen ovan>", "motivering": "<max 15 ord på svenska>"}. '
        "Inget annat i svaret."
    )
    return "\n".join(lines)


def load_image_as_jpeg_b64(path: Path) -> tuple[str, str]:
    """Läser in en bild, skalar ner den vid behov och returnerar (base64, media_type)."""
    ext = path.suffix.lower()

    if ext in {".heic", ".heif"} and not HEIC_SUPPORT:
        raise RuntimeError(
            "HEIC-stöd saknas. Installera med: pip install pillow-heif"
        )

    with Image.open(path) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_DIMENSION:
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()

    return base64.standard_b64encode(data).decode("utf-8"), "image/jpeg"


def classify_image(client: "anthropic.Anthropic", model: str, path: Path, prompt: str) -> Result:
    try:
        b64, media_type = load_image_as_jpeg_b64(path)
    except Exception as exc:  # noqa: BLE001
        return Result(path=path, category=None, reasoning="", error=f"Kunde inte läsa bild: {exc}")

    try:
        message = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return Result(path=path, category=None, reasoning="", error=f"API-fel: {exc}")

    text = "".join(block.text for block in message.content if hasattr(block, "text")).strip()

    # Plocka ut JSON även om modellen skulle råka lägga till text runt omkring.
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        category = parsed.get("kategori", "").strip()
        reasoning = parsed.get("motivering", "").strip()
    except Exception:  # noqa: BLE001
        return Result(path=path, category=None, reasoning="", error=f"Kunde inte tolka svar: {text!r}")

    if category not in CATEGORIES:
        # Försök hitta närmaste giltiga kategori (case-insensitive)
        match = next((c for c in CATEGORIES if c.lower() == category.lower()), None)
        if match is None:
            return Result(path=path, category=None, reasoning=reasoning,
                           error=f"Okänd kategori i svar: {category!r}")
        category = match

    return Result(path=path, category=category, reasoning=reasoning)


def existing_tag(filename: str) -> Optional[str]:
    """Returnerar kategorin om filnamnet redan börjar med en känd tagg."""
    if filename.startswith("["):
        end = filename.find("]")
        if end != -1:
            tag = filename[1:end]
            if tag in CATEGORIES:
                return tag
    return None


def file_mtime_within(path: Path, cutoff: datetime) -> bool:
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime >= cutoff


def collect_images(folder: Path, recursive: bool, days: int, include_all_dates: bool) -> list[Path]:
    cutoff = datetime.now() - timedelta(days=days)
    pattern = "**/*" if recursive else "*"
    files = []
    for p in sorted(folder.glob(pattern)):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VALID_EXTENSIONS:
            continue
        if include_all_dates or file_mtime_within(p, cutoff):
            files.append(p)
    return files


def rename_with_tag(path: Path, category: str, dry_run: bool) -> Path:
    current_tag = existing_tag(path.name)
    if current_tag == category:
        return path  # redan rätt taggad

    if current_tag is not None:
        # Ta bort gammal tagg innan ny läggs på (t.ex. vid omklassificering).
        base_name = path.name[path.name.find("]") + 1:].lstrip()
    else:
        base_name = path.name

    new_name = f"[{category}] {base_name}"
    new_path = path.with_name(new_name)

    if dry_run:
        return new_path

    if new_path.exists():
        # Undvik att skriva över en annan fil - lägg till suffix.
        stem, suffix = os.path.splitext(new_name)
        counter = 1
        while new_path.exists():
            new_path = path.with_name(f"{stem} ({counter}){suffix}")
            counter += 1

    path.rename(new_path)
    return new_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sortera/tagga bilder utifrån innehåll (byggarbetare, tak, "
        "takrelaterat, övrigt) med hjälp av Claude vision."
    )
    parser.add_argument("--folder", "-f", required=True, type=Path, help="Mapp med bilder")
    parser.add_argument("--days", "-d", type=int, default=7,
                         help="Hur många dagar bakåt (baserat på filens ändringsdatum). Default: 7")
    parser.add_argument("--all-dates", action="store_true",
                         help="Ignorera datumfiltret och kör på alla bilder i mappen")
    parser.add_argument("--recursive", "-r", action="store_true",
                         help="Sök även i undermappar")
    parser.add_argument("--dry-run", action="store_true",
                         help="Visa vad som skulle hända utan att döpa om några filer")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude-modell (default: {DEFAULT_MODEL})")
    parser.add_argument("--concurrency", type=int, default=4, help="Antal parallella API-anrop (default: 4)")
    parser.add_argument("--csv", type=Path, default=None,
                         help="Valfri sökväg till en CSV-rapport med resultaten")
    parser.add_argument("--api-key", default=None,
                         help="Anthropic API-nyckel (annars läses ANTHROPIC_API_KEY från miljön)")
    args = parser.parse_args()

    if not args.folder.is_dir():
        print(f"Mappen finns inte: {args.folder}", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Ingen API-nyckel hittades. Sätt miljövariabeln ANTHROPIC_API_KEY "
            "eller använd --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt()

    images = collect_images(args.folder, args.recursive, args.days, args.all_dates)
    if not images:
        print("Hittade inga bilder som matchar filtret.")
        return

    print(f"Hittade {len(images)} bild(er) att analysera "
          f"({'alla datum' if args.all_dates else f'senaste {args.days} dagarna'}).")

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(classify_image, client, args.model, path, prompt): path
            for path in images
        }
        for i, future in enumerate(as_completed(futures), start=1):
            path = futures[future]
            result = future.result()
            results.append(result)

            if result.error:
                print(f"[{i}/{len(images)}] ⚠️  {path.name}: {result.error}")
                continue

            new_path = rename_with_tag(path, result.category, args.dry_run)
            action = "SKULLE DÖPAS OM" if args.dry_run else "omdöpt"
            arrow = f" -> {new_path.name}" if new_path.name != path.name else " (redan taggad)"
            print(f"[{i}/{len(images)}] ✅ {path.name}: {result.category} ({action}){arrow}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["fil", "kategori", "motivering", "fel"])
            for r in results:
                writer.writerow([str(r.path), r.category or "", r.reasoning, r.error or ""])
        print(f"\nCSV-rapport skriven till: {args.csv}")

    # Sammanfattning
    counts: dict[str, int] = {}
    for r in results:
        key = r.category or "FEL"
        counts[key] = counts.get(key, 0) + 1
    print("\nSammanfattning:")
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
