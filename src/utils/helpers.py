import json
import os
import re
import mimetypes
import sqlite3
import asyncio
import contextvars
import hashlib
import ipaddress
import random
import socket
import time
import math
from collections import Counter
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode
from html import unescape
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from dotenv import load_dotenv
import discord
from discord.ext import commands
import plaid_sync
import sandbox_client
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image

from src.core.state import *


# ------------------------------------------------------------
# Vision / image-attachment handling
# ------------------------------------------------------------
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}
IMAGE_MAX_DIMENSION = int(os.getenv("IMAGE_MAX_DIMENSION", "1568"))
IMAGE_JPEG_QUALITY = int(os.getenv("IMAGE_JPEG_QUALITY", "85"))
MAX_IMAGES_PER_MESSAGE = int(os.getenv("MAX_IMAGES_PER_MESSAGE", "4"))

import pymupdf  # PyMuPDF

async def _prepare_pdf_attachment(attachment: discord.Attachment) -> tuple[list[str], str]:
    raw = await attachment.read()
    res = await asyncio.to_thread(_extract_pdf_content, raw)
    return res["image_pages"], res["text"]

def _extract_pdf_content(content: bytes) -> dict:
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
        text = ""
        images = []
        for i, page in enumerate(doc):
            text += page.get_text() + "\n"
            if i < PDF_MAX_PAGES:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)
                images.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        return {"text": text, "image_pages": images}
    except Exception as e:
        print(f" PDF parse error: {e}")
        return {"text": f"PDF parse error: {e}", "image_pages": []}

# ============================================================
# Merchant Parsing
# ============================================================
def sanitize_merchant_name(raw: str) -> str:
    if not raw: return ''
    if not raw: return ''
    clean = re.sub(
        r"^(AplPay\s*|TST\*|SQ \*|UEP\*|PAYPAL\s*\*|SP \*|PAYPAL\s+TO\s+)",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"#\d+|\d{5,}", "", clean)
    clean = re.sub(r"\bSTORE\s*#?\d*\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"(BROOKLYN|NEW YORK|NY|MANHATTAN)$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+[A-Z]{2}$", "", clean)
    clean = re.sub(r"\s{2,}", " ", clean)
    return clean.strip() or raw

async def _prepare_image_attachment(attachment: discord.Attachment) -> str | None:
    """Download and normalize a Discord image for Ollama vision.

    Discord can occasionally omit or vary ``content_type``.  Do not silently
    discard an image just because the MIME metadata is missing; infer it from
    the filename and ultimately let Pillow validate the actual bytes.
    Ollama expects the image as raw base64 (NOT a data: URI), so this function
    always returns raw JPEG base64.
    """
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if not content_type:
        guessed_type, _ = mimetypes.guess_type(attachment.filename or "")
        content_type = (guessed_type or "").lower()

    # Some Discord/CDN attachments arrive with an unhelpful MIME type.  The
    # filename is enough to identify the common image extensions safely.
    filename_lower = (attachment.filename or "").lower()
    extension_is_image = filename_lower.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )
    if content_type not in SUPPORTED_IMAGE_CONTENT_TYPES and not extension_is_image:
        print(
            f" [image attachment] skipping unsupported attachment "
            f"name={attachment.filename!r} content_type={attachment.content_type!r}"
        )
        return None

    try:
        raw = await attachment.read(use_cached=False)
        if not raw:
            raise ValueError("Discord returned an empty attachment")
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception as exc:
        print(
            f" [image attachment] failed to decode "
            f"{attachment.filename!r}: {type(exc).__name__}: {exc}"
        )
        return None

    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
#    if longest > IMAGE_MAX_DIMENSION:
#        scale = IMAGE_MAX_DIMENSION / longest
#        img = img.resize(
#            (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
#        )
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    print(
        f" [image attachment] prepared {attachment.filename!r} "
        f"original={w}x{h} jpeg_bytes={len(buf.getvalue())} "
        f"b64_chars={len(encoded)}"
    )
    return encoded

async def _prepare_image_url(url: str) -> str | None:
    """Download and normalize a user-supplied web image URL for Ollama vision.

    This uses the exact same JPEG/base64 normalization path as Discord images,
    but accepts a normal HTTP(S) image URL from message text (for example a
    Zipline URL). The returned value is raw JPEG base64 suitable for Ollama's
    ``images`` field.
    """
    url = str(url or "").strip()
    if not url:
        return None

    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            print(f" [web image] rejected non-http URL: {url!r}")
            return None
    except Exception as exc:
        print(f" [web image] URL parse failed: {type(exc).__name__}: {exc}")
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/151 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }

        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

        raw = response.content
        if not raw:
            raise ValueError("server returned an empty response")

        content_type = (
            response.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        # Do not trust MIME alone. Pillow is the final authority on whether
        # the response is actually an image.
        if not (
            content_type.startswith("image/")
            or urlparse(str(response.url)).path.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif")
            )
        ):
            print(
                f" [web image] response does not identify itself as an image "
                f"url={url!r} content_type={content_type!r}"
            )

        img = Image.open(BytesIO(raw))
        img.load()

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)

        encoded = base64.b64encode(buf.getvalue()).decode("ascii")

        print(
            f" [web image] prepared url={url!r} "
            f"final_url={str(response.url)!r} "
            f"content_type={content_type!r} "
            f"original={w}x{h} "
            f"jpeg_bytes={len(buf.getvalue())} "
            f"b64_chars={len(encoded)}"
        )

        return encoded

    except Exception as exc:
        print(
            f" [web image] failed to download/decode "
            f"url={url!r}: {type(exc).__name__}: {exc}"
        )
        return None


def _extract_image_urls(text: str) -> list[str]:
    """Extract HTTP(S) URLs that appear to reference common image formats."""
    if not text:
        return []

    candidates = re.findall(
        r'https?://[^\s<>\[\]()"\']+',
        text,
        flags=re.IGNORECASE,
    )

    image_urls = []
    seen = set()

    for raw_url in candidates:
        url = raw_url.rstrip(".,;:!?")
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
        except Exception:
            continue

        if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            key = url.lower()
            if key not in seen:
                seen.add(key)
                image_urls.append(url)

    return image_urls


async def _collect_message_images(
    message: discord.Message,
) -> tuple[list[str], list[str]]:
    """Return (base64 images, extracted PDF texts) from a message's attachments."""
    if not message.attachments:
        return [], []

    images: list[str] = []
    pdf_texts: list[str] = []
    pdf_pages: list[str] = []
    seen_image_hashes: set[str] = set()

    for attachment in message.attachments:
        if len(images) >= MAX_IMAGES_PER_MESSAGE:
            break
        content_type = (attachment.content_type or "").split(";")[0].strip().lower()
        if content_type in SUPPORTED_IMAGE_CONTENT_TYPES:
            encoded = await _prepare_image_attachment(attachment)
            if encoded:
                digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
                if digest in seen_image_hashes:
                    print(
                        f" [image attachment] duplicate image skipped "
                        f"name={attachment.filename!r}"
                    )
                else:
                    seen_image_hashes.add(digest)
                    images.append(encoded)
        elif content_type in SUPPORTED_PDF_CONTENT_TYPES:
            new_pdf_pages, pdf_text = await _prepare_pdf_attachment(attachment)
            pdf_pages.extend(new_pdf_pages)
            if pdf_text and pdf_text.strip():
                pdf_texts.append(f"[PDF attachment: {attachment.filename}]\n{pdf_text}")

    remaining = MAX_IMAGES_PER_MESSAGE - len(images)
    images.extend(pdf_pages[:remaining])

    return images, pdf_texts


__all__ = ['IMAGE_MAX_DIMENSION', 'IMAGE_JPEG_QUALITY', 'MAX_IMAGES_PER_MESSAGE', '_prepare_image_attachment', '_prepare_image_url', '_extract_image_urls', 'SUPPORTED_IMAGE_CONTENT_TYPES', '_collect_message_images', '_extract_pdf_content', 'sanitize_merchant_name', '_prepare_pdf_attachment']
