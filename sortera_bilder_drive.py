#!/usr/bin/env python3
"""
sortera_bilder_drive.py
========================

Samma sak som sortera_bilder.py, fast för bilder som ligger i GOOGLE DRIVE
istället för i en lokal mapp.

Klassificerar bilder från senaste veckan (eller valfritt antal dagar) i en
Drive-mapp i fyra kategorier (Byggarbetare / Tak / Takrelaterat / Ovrigt)
med hjälp av Claude vision, och döper om filerna direkt i Drive genom att
lägga till kategorin i hakparentes:

    IMG_0231.jpg  ->  [Byggarbetare] IMG_0231.jpg

Bilderna flyttas INTE och innehållet ändras inte - bara filnamnet i Drive.

Förberedelser (görs en gång)
-----------------------------
1. Gå till https://console.cloud.google.com/ och skapa ett projekt.
2. Aktivera "Google Drive API" under APIs & Services -> Library.
3. Konfigurera OAuth consent screen (välj "External", lägg till ditt eget
   Google-konto under "Test users" - då slipper du Googles appgranskning).
4. Skapa autentiseringsuppgifter: APIs & Services -> Credentials ->
   Create Credentials -> OAuth client ID -> Application type: Desktop app.
5. Ladda ner JSON-filen och spara den som credentials.json i samma mapp
   som det här skriptet.
6. Öppna Drive-mappen med dina bilder i webbläsaren och kopiera mapp-ID:t
   ur adressen: https://drive.google.com/drive/folders/<MAPP-ID>

Installation
------------
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="din-api-nyckel"

Användning
----------
    python3 sortera_bilder_drive.py --folder-id <MAPP-ID> --dry-run
    python3 sortera_bilder_drive.py --folder-id <MAPP-ID>

Första gången öppnas en webbläsarflik där du loggar in och godkänner
åtkomst. En token cachas sedan lokalt (token.json) så du slipper logga in
igen nästa gång.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import anthropic

from bildklassificering import DEFAULT_MODEL, build_prompt, classify_bytes, new_tagged_name

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    print(
        "Google Drive-paketen saknas. Installera med:\n"
        "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib",
        file=sys.stderr,
    )
    sys.exit(1)

# Läs+skriv metadata (för listning och omdöpning) samt läs innehåll (för nedladdning).
# Medvetet INTE fullständig 'drive'-scope - vi behöver aldrig ändra bildinnehåll.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata",
]

FOLDER_MIME = "application/vnd.google-apps.folder"


def extract_folder_id(value: str) -> str:
    """Tar emot antingen ett rått mapp-ID eller en fullständig Drive-URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)
    return value.strip()


def get_drive_service(credentials_path: str, token_path: str):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                print(
                    f"Hittar ingen credentials-fil: {credentials_path}\n"
                    "Se instruktionerna högst upp i sortera_bilder_drive.py för hur du skapar den.",
                    file=sys.stderr,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_subfolders(service, folder_id: str) -> list[str]:
    ids = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
            fields="nextPageToken, files(id)",
            pageToken=page_token,
        ).execute()
        ids.extend(f["id"] for f in resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def list_images_in_folder(service, folder_id: str, cutoff_iso: str | None) -> list[dict]:
    q = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false"
    if cutoff_iso:
        q += f" and modifiedTime > '{cutoff_iso}'"

    files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token,
            pageSize=200,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def collect_images(service, folder_id: str, recursive: bool, days: int, include_all_dates: bool) -> list[dict]:
    cutoff_iso = None
    if not include_all_dates:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    folder_ids = [folder_id]
    if recursive:
        seen = {folder_id}
        queue = [folder_id]
        while queue:
            current = queue.pop()
            for sub_id in list_subfolders(service, current):
                if sub_id not in seen:
                    seen.add(sub_id)
                    queue.append(sub_id)
                    folder_ids.append(sub_id)

    all_files = []
    for fid in folder_ids:
        all_files.extend(list_images_in_folder(service, fid, cutoff_iso))
    return all_files


def download_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def rename_file(service, file_id: str, new_name: str) -> None:
    service.files().update(fileId=file_id, body={"name": new_name}, fields="id,name").execute()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sortera/tagga bilder i Google Drive utifrån innehåll (byggarbetare, "
        "tak, takrelaterat, övrigt) med hjälp av Claude vision."
    )
    parser.add_argument("--folder-id", "-i", required=True,
                         help="Google Drive mapp-ID (eller klistra in hela mapp-URL:en)")
    parser.add_argument("--days", "-d", type=int, default=7,
                         help="Hur många dagar bakåt (baserat på filens ändringsdatum i Drive). Default: 7")
    parser.add_argument("--all-dates", action="store_true",
                         help="Ignorera datumfiltret och kör på alla bilder i mappen")
    parser.add_argument("--recursive", "-r", action="store_true",
                         help="Sök även i undermappar")
    parser.add_argument("--dry-run", action="store_true",
                         help="Visa vad som skulle hända utan att döpa om några filer")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude-modell (default: {DEFAULT_MODEL})")
    parser.add_argument("--concurrency", type=int, default=4, help="Antal parallella API-anrop (default: 4)")
    parser.add_argument("--csv", default=None, help="Valfri sökväg till en CSV-rapport med resultaten")
    parser.add_argument("--api-key", default=None,
                         help="Anthropic API-nyckel (annars läses ANTHROPIC_API_KEY från miljön)")
    parser.add_argument("--credentials", default="credentials.json",
                         help="Sökväg till Google OAuth client secret-fil (default: credentials.json)")
    parser.add_argument("--token", default="token.json",
                         help="Sökväg där Google-inloggningen cachas (default: token.json)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Ingen API-nyckel hittades. Sätt miljövariabeln ANTHROPIC_API_KEY "
            "eller använd --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    folder_id = extract_folder_id(args.folder_id)
    service = get_drive_service(args.credentials, args.token)
    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt()

    files = collect_images(service, folder_id, args.recursive, args.days, args.all_dates)
    if not files:
        print("Hittade inga bilder som matchar filtret i Drive-mappen.")
        return

    print(f"Hittade {len(files)} bild(er) att analysera "
          f"({'alla datum' if args.all_dates else f'senaste {args.days} dagarna'}).")

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {}
        for meta in files:
            raw = download_bytes(service, meta["id"])
            futures[pool.submit(classify_bytes, client, args.model, meta["name"], raw, prompt)] = meta

        for i, future in enumerate(as_completed(futures), start=1):
            meta = futures[future]
            result = future.result()
            results.append(result)

            if result.error:
                print(f"[{i}/{len(files)}] ⚠️  {meta['name']}: {result.error}")
                continue

            new_name = new_tagged_name(meta["name"], result.category)
            action = "SKULLE DÖPAS OM" if args.dry_run else "omdöpt"
            if new_name is None:
                print(f"[{i}/{len(files)}] ✅ {meta['name']}: {result.category} (redan taggad)")
            else:
                if not args.dry_run:
                    rename_file(service, meta["id"], new_name)
                print(f"[{i}/{len(files)}] ✅ {meta['name']}: {result.category} ({action}) -> {new_name}")

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
