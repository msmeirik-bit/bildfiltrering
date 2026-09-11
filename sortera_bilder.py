#!/usr/bin/env python3
"""
sortera_bilder.py
==================

Sorterar bilder från den senaste veckan (eller valfritt antal dagar) i en
LOKAL mapp, baserat på fyra centrala kategorier:

  1. Byggarbetare   - det syns en byggarbetare (person i arbete) på bilden
  2. Tak             - bilden visar bara ett tak, inga personer av intresse
  3. Takrelaterat    - annat takrelaterat: industrilokal, företagslogga, etc.
  4. Ovrigt          - inget av ovanstående / går inte att avgöra

Skriptet döper om varje matchande fil genom att lägga till en kategori-tagg
i hakparentes främst i filnamnet, t.ex.:

    IMG_0231.jpg  ->  [Byggarbetare] IMG_0231.jpg

Bilderna flyttas INTE - de ligger kvar i samma mapp, bara namnet ändras.

Ligger dina bilder i Google Drive istället för lokalt? Använd
sortera_bilder_drive.py.

Användning
----------
    export ANTHROPIC_API_KEY="din-api-nyckel"
    python3 sortera_bilder.py --folder /path/till/bilder

Se --help för fler flaggor (antal dagar, dry-run, rekursiv sökning, m.m.)
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from bildklassificering import (
    CATEGORIES,
    DEFAULT_MODEL,
    VALID_EXTENSIONS,
    build_prompt,
    classify_bytes,
    new_tagged_name,
)


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
    new_name = new_tagged_name(path.name, category)
    if new_name is None:
        return path  # redan rätt taggad

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

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(classify_bytes, client, args.model, path.name, path.read_bytes(), prompt): path
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
                writer.writerow([r.name, r.category or "", r.reasoning, r.error or ""])
        print(f"\nCSV-rapport skriven till: {args.csv}")

    counts: dict[str, int] = {}
    for r in results:
        key = r.category or "FEL"
        counts[key] = counts.get(key, 0) + 1
    print("\nSammanfattning:")
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
