"""
bildklassificering.py
======================

Delad logik för att klassificera bilder med Claude vision, oavsett om
bilderna kommer från en lokal mapp eller Google Drive.

Används av sortera_bilder.py (lokal mapp) och sortera_bilder_drive.py
(Google Drive).
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Optional

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

import anthropic


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
    name: str
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


def raw_bytes_to_jpeg_b64(raw: bytes) -> str:
    """Läser in godtyckliga bildbytes (jpg/png/heic/...), skalar ner vid behov
    och returnerar base64-kodad JPEG."""
    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_DIMENSION:
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()
    return base64.standard_b64encode(data).decode("utf-8")


def classify_bytes(
    client: "anthropic.Anthropic",
    model: str,
    name: str,
    raw_bytes: bytes,
    prompt: str,
) -> Result:
    """Klassificerar en bild givet dess råa bytes. `name` används bara för loggning."""
    try:
        b64 = raw_bytes_to_jpeg_b64(raw_bytes)
    except Exception as exc:  # noqa: BLE001
        return Result(name=name, category=None, reasoning="", error=f"Kunde inte läsa bild: {exc}")

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
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return Result(name=name, category=None, reasoning="", error=f"API-fel: {exc}")

    text = "".join(block.text for block in message.content if hasattr(block, "text")).strip()

    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        category = parsed.get("kategori", "").strip()
        reasoning = parsed.get("motivering", "").strip()
    except Exception:  # noqa: BLE001
        return Result(name=name, category=None, reasoning="", error=f"Kunde inte tolka svar: {text!r}")

    if category not in CATEGORIES:
        match = next((c for c in CATEGORIES if c.lower() == category.lower()), None)
        if match is None:
            return Result(name=name, category=None, reasoning=reasoning,
                           error=f"Okänd kategori i svar: {category!r}")
        category = match

    return Result(name=name, category=category, reasoning=reasoning)


def existing_tag(filename: str) -> Optional[str]:
    """Returnerar kategorin om filnamnet redan börjar med en känd tagg, t.ex. '[Tak] '."""
    if filename.startswith("["):
        end = filename.find("]")
        if end != -1:
            tag = filename[1:end]
            if tag in CATEGORIES:
                return tag
    return None


def new_tagged_name(filename: str, category: str) -> Optional[str]:
    """Räknar ut nytt filnamn med kategori-tagg. Returnerar None om filen redan
    har rätt tagg (ingen förändring behövs)."""
    current_tag = existing_tag(filename)
    if current_tag == category:
        return None

    if current_tag is not None:
        base_name = filename[filename.find("]") + 1:].lstrip()
    else:
        base_name = filename

    return f"[{category}] {base_name}"
