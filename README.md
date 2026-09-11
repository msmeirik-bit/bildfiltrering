# Bildfiltrering

Ett litet skript som sorterar/taggar dina bilder från den senaste veckan
utifrån innehåll, med hjälp av Claudes bildanalys (vision).

## Kategorier

Varje bild klassificeras i **en** av följande kategorier:

| Kategori        | Beskrivning |
|-----------------|-------------|
| `Byggarbetare`  | Det syns tydligt en byggarbetare (person i arbete) på bilden. |
| `Tak`           | Bilden visar huvudsakligen bara ett tak, inga personer. |
| `Takrelaterat`  | Annat takrelaterat: industrilokal/industribyggnad, företagslogga, märkt fordon/material. |
| `Ovrigt`        | Inget av ovanstående, eller går inte att avgöra. |

Skriptet **döper om** varje matchande bildfil genom att lägga till
kategorin i hakparentes främst i filnamnet:

```
IMG_0231.jpg  ->  [Byggarbetare] IMG_0231.jpg
```

Filerna flyttas inte och innehållet ändras inte – bara filnamnet får en
kategori-tagg, så du enkelt kan sortera/filtrera på namn i din filhanterare
eller fotoapp.

## Installation

Kör detta på datorn där bilderna faktiskt ligger (skriptet körs inte i den
här molnsessionen, eftersom den inte har tillgång till din lokala bildmapp):

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Skaffa en Anthropic API-nyckel på https://console.anthropic.com/ och sätt den:

```bash
export ANTHROPIC_API_KEY="din-nyckel-här"      # Windows (PowerShell): $env:ANTHROPIC_API_KEY="din-nyckel-här"
```

## Användning

Testa först med `--dry-run` så inget döps om förrän du är nöjd:

```bash
python3 sortera_bilder.py --folder "/sökväg/till/dina/bilder" --dry-run
```

Kör sedan på riktigt (sorterar bilder från de senaste 7 dagarna, baserat på
filens ändringsdatum):

```bash
python3 sortera_bilder.py --folder "/sökväg/till/dina/bilder"
```

### Vanliga flaggor

| Flagga | Beskrivning |
|--------|-------------|
| `--days 14` | Ändra hur många dagar bakåt som ska tas med (default 7). |
| `--all-dates` | Ignorera datumfiltret, kör på alla bilder i mappen. |
| `--recursive` | Sök även i undermappar. |
| `--dry-run` | Visa vad som skulle hända utan att döpa om filer. |
| `--csv rapport.csv` | Spara en CSV-rapport med kategori och motivering per bild. |
| `--concurrency 8` | Antal parallella API-anrop (default 4). |
| `--model claude-opus-5` | Använd en annan Claude-modell. |

### Exempel

```bash
python3 sortera_bilder.py \
  --folder "~/Bilder/Kamera" \
  --recursive \
  --csv rapport.csv
```

## Stödda filformat

JPG, JPEG, PNG, WEBP, GIF, HEIC/HEIF (iPhone-bilder – kräver `pillow-heif`,
som redan ingår i `requirements.txt`).

## Bra att veta

- Skriptet ändrar aldrig bildinnehållet, bara filnamnet.
- Kör du skriptet igen på redan taggade filer byts bara taggen ut om
  klassificeringen skulle bli en annan – redan korrekt taggade filer rörs inte.
- Datumfiltret baseras på filens ändringsdatum (mtime) på disk, inte EXIF-datum
  – om dina bilder synkats/kopierats nyligen kan det påverka vilka som räknas
  som "senaste veckan".
