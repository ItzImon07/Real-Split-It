"""
ai_service.py

Handles the full receipt understanding pipeline via Gemini's multimodal
vision capability: reading the raw receipt photo directly, extracting
menu items with category tags, and detecting tax/service/tip charges —
all in a single model call.

This replaces the old OpenCV (preprocess) -> EasyOCR (extract) -> regex
(parse) -> Gemini (categorize) pipeline. That approach required hand-tuned
thresholds and column-detection heuristics that broke on every new bill
layout (different fonts, lighting, thermal vs. glossy paper, table vs.
single-column formats). Gemini's vision model has seen a much wider range
of real-world receipt formats than any hand-tuned CV pipeline can cover,
so sending it the image directly is both more robust and far less code
to maintain.
"""

import os
import json
import re
import logging

from dotenv import load_dotenv
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

load_dotenv()

_API_KEY = os.environ.get("GEMINI_API_KEY")
if not _API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment. Receipt extraction will fail.")

_client = genai.Client(api_key=_API_KEY) if _API_KEY else None

_MODEL_NAME = "gemini-2.5-flash"

_VALID_TAGS = {"veg", "non-veg", "drinks", "desserts", "staples"}

_EXTRACTION_PROMPT = """You are a strict JSON API that reads restaurant receipt photos.

Look at the receipt image and extract two things:

1. MENU ITEMS — every food/drink line item actually ordered, with its price
   and a category tag.
2. CHARGES — any tax, service charge, or tip/gratuity printed on the bill,
   summed into three totals. Do NOT include these as menu items.

Category tags for items (exactly one per item):
- "staples"   (breads, rice, and noodles: naan, roti, kulcha, paratha, papad,
              plain/jeera/fried rice, biryani, pulao, noodles/chowmein/hakka
              noodles — ALL of these are "staples" regardless of whether
              they're veg or non-veg. E.g. "Chicken Fried Rice" and "Veg
              Hakka Noodles" are BOTH "staples", not "non-veg"/"veg".)
- "veg"       (vegetarian curries and other non-staple veg dishes)
- "non-veg"   (meat, fish, egg curries and other non-staple non-veg dishes)
- "drinks"    (beverages, alcohol, juices, water, soda)
- "desserts"  (sweets, ice cream, cakes, Indian mithai)

Rules:
1. Return ONLY valid JSON. No markdown, no code fences, no explanation, no preamble.
2. "staples" takes priority over "veg"/"non-veg" for any bread, rice, or noodle
   dish, even if the name also mentions chicken, egg, or a vegetable.
3. If the receipt has multiple tax lines (e.g. CGST + SGST), sum them into one
   "tax" total. If there's no tax/service/tip line at all, use 0 for that field.
4. If a price is genuinely illegible, make your best reasonable estimate rather
   than omitting the item — but never invent an item that isn't on the receipt.
5. Always extract the FINAL total price for an item. If a line shows both a unit 
   price and a total price (e.g., '2 x 150 = 300'), extract the total (300). If the 
   total is missing or illegible, calculate it yourself (unit price * quantity).
6. Ignore restaurant name, address, invoice number, date, table number, and the
   final "Total"/"Grand Total" line itself — those aren't items or charges.
7. If unsure about a category, make your best guess based on common Indian
   restaurant menu conventions — never invent a sixth category.

Return EXACTLY this JSON shape, nothing else:
{
  "items": [
    {"name": "Item Name", "price": 100.0, "tag": "veg"}
  ],
  "charges": {"tax": 0.0, "service": 0.0, "tip": 0.0}
}
"""


def _extract_json_object(raw_text: str) -> str:
    """Strips markdown code fences defensively, in case Gemini wraps JSON
    in them despite being told not to."""
    text = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    return text


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """
    Sends the raw receipt image to Gemini and returns structured items +
    charges in one call.

    Args:
        image_bytes: raw bytes of the uploaded receipt photo.
        mime_type: the image's content type (e.g. "image/jpeg", "image/png").

    Returns:
        {
            "items": [{"name": str, "price": float, "tag": str}, ...], 
            "charges": {"tax": float, "service": float, "tip": float}
        }

    Raises:
        RuntimeError if the Gemini client isn't configured, the API call
        fails, or the response can't be parsed into the expected shape —
        the caller (views.py) turns this into a clean 500 response rather
        than silently returning an empty/wrong receipt.
    """
    if _client is None:
        raise RuntimeError("Gemini client not configured — check GEMINI_API_KEY in .env")

    try:
        response = _client.models.generate_content(
            model=_MODEL_NAME,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                _EXTRACTION_PROMPT,
            ],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.1,  # low temperature: extraction, not creative writing
            },
        )
        raw_text = response.text
    except Exception as e:
        logger.error(f"Gemini vision extraction failed: {e}")
        raise RuntimeError(f"Could not read this receipt: {e}")

    cleaned = _extract_json_object(raw_text)

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Failed to parse Gemini response as JSON: {e}. Raw: {raw_text[:300]}")
        raise RuntimeError("Gemini returned a response that wasn't valid JSON.")

    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini response was not a JSON object as expected.")

    raw_items = parsed.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        price = _safe_float(entry.get("price"))
        tag = entry.get("tag") if entry.get("tag") in _VALID_TAGS else "unknown"

        if not name or price <= 0:
            continue

        items.append({"name": name, "price": round(price, 2), "tag": tag})

    raw_charges = parsed.get("charges", {})
    if not isinstance(raw_charges, dict):
        raw_charges = {}

    charges = {
        "tax": round(_safe_float(raw_charges.get("tax")), 2),
        "service": round(_safe_float(raw_charges.get("service")), 2),
        "tip": round(_safe_float(raw_charges.get("tip")), 2),
    }

    return {"items": items, "charges": charges}