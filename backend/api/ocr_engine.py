"""
ocr_engine.py

Modular OCR pipeline for receipt images.

Pipeline stages:
    1. preprocess_image()      -> OpenCV cleanup for OCR accuracy
    2. EasyOCR + row grouping  -> reconstruct visual rows from scattered text boxes
    3. Column detection        -> identify DESCRIPTION/RATE/QTY/AMOUNT headers by x-position
    4. Row classification      -> split rows into menu ITEMS vs. TAX/SERVICE/TIP charges
    5. parse_table_rows() OR
       parse_receipt_lines()   -> structure items into [{"name": ..., "price": ...}]
    6. categorize_items()      -> Gemini veg/non-veg/drinks/desserts tagging (items only)

Kept separate from views.py so this logic is testable in isolation.
"""

import re
import cv2
import numpy as np
import easyocr

from .ai_service import categorize_items

# EasyOCR model loading is expensive (~1-2s + memory for weights).
# Instantiate the reader ONCE at module load time, not per-request.
_reader = easyocr.Reader(['en'], gpu=False)


def preprocess_image(image_bytes: bytes) -> np.ndarray:
    """
    Convert raw uploaded image bytes into a clean binary image optimized
    for OCR accuracy on receipt paper.

    Steps & rationale:
      1. Decode bytes -> OpenCV BGR array.
      2. Grayscale: color is irrelevant for text detection.
      3. Denoise (fastNlMeansDenoising): kills sensor/paper grain without
         blurring character edges the way a Gaussian blur would.
      4. Adaptive thresholding: receipts are often unevenly lit, so a
         single global threshold would wash out text in darker/brighter
         regions. ADAPTIVE_THRESH_GAUSSIAN_C recalculates the threshold
         per local neighborhood, self-correcting for lighting gradients.
    """
    np_arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image. Ensure a valid JPEG/PNG was uploaded.")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)

    binarized = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=25,
        C=15,
    )

    return binarized


# --- Row reconstruction ---

def _y_center(bbox):
    return (bbox[0][1] + bbox[2][1]) / 2


def _x_center(bbox):
    xs = [pt[0] for pt in bbox]
    return sum(xs) / len(xs)


def _group_rows(boxes, row_tolerance=15):
    """
    Clusters OCR text boxes into visual rows by vertical proximity, then
    sorts left-to-right within each row. Tabular receipts OCR each column
    as a separate text box even when cells belong to the same visual row,
    so naive top-to-bottom sorting alone isn't enough to reconstruct rows.

    Returns: list of rows, each row a list of (x_center, text) tuples.
    """
    boxes = sorted(boxes, key=lambda b: _y_center(b[0]))
    rows, current_row, current_y = [], [], None

    for bbox, text, conf in boxes:
        y = _y_center(bbox)
        if current_y is None or abs(y - current_y) <= row_tolerance:
            current_row.append((_x_center(bbox), text))
            current_y = y if current_y is None else (current_y + y) / 2
        else:
            rows.append(sorted(current_row, key=lambda t: t[0]))
            current_row, current_y = [(_x_center(bbox), text)], y

    if current_row:
        rows.append(sorted(current_row, key=lambda t: t[0]))

    return rows


# --- Column detection for tabular receipts ---

_HEADER_LABELS = {
    "description": "name",
    "item": "name",
    "particulars": "name",
    "rate": "rate",
    "price": "rate",
    "qty": "qty",
    "quantity": "qty",
    "amount": "amount",
    "total": "amount",
}


def _find_header_columns(rows):
    """
    Scans rows for one containing recognizable table headers and returns
    (header_row_index, {column_role: x_center}).

    Returns (None, {}) if no header row is found — this signals the
    caller to fall back to plain line-based parsing, since not every
    receipt is a table (a single-column thermal receipt has no header
    row at all, and that's fine — it just uses the other code path).
    """
    for idx, row in enumerate(rows):
        columns = {}
        for x, text in row:
            key = text.strip().lower().rstrip(":")
            role = _HEADER_LABELS.get(key)
            if role:
                columns[role] = x
        if "name" in columns and ("amount" in columns or "rate" in columns):
            return idx, columns
    return None, {}


def _nearest_column(x, columns):
    return min(columns.items(), key=lambda kv: abs(kv[1] - x))[0]


def _parse_number(text):
    """Extracts a float from a string, tolerating stray leading OCR glyphs
    (e.g. the ₹ symbol frequently misreads as '{' or a stray digit)."""
    match = re.search(r'[^\d]{0,2}(\d+(?:[.,]\d{1,2})?)', text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(',', '.'))
    except ValueError:
        return None


def _strip_possible_currency_misread(amount, rate):
    """
    EasyOCR sometimes misreads the ₹ glyph as an extra leading digit
    fused directly into the AMOUNT number itself (e.g. "180.00" OCR'd
    as "7180.00") -- not a separate stray character we can strip, but
    baked into the digit string. If removing the first digit of `amount`
    produces a value close to `rate`, that's a strong, specific signal
    of this exact corruption. Narrower than a generic RATE x QTY check,
    which misfires whenever quantity is genuinely >1 but wasn't OCR'd.

    Returns the corrected value, or None if no correction applies.
    """
    if rate is None or amount is None:
        return None

    amount_str = f"{amount:.2f}"
    integer_part, _, decimals = amount_str.partition(".")

    if len(integer_part) <= 1:
        return None

    try:
        stripped = float(f"{integer_part[1:]}.{decimals}")
    except ValueError:
        return None

    if abs(stripped - rate) <= max(rate * 0.1, 2):
        return stripped

    return None


# --- Charge line classification (tax / service / tip) ---
#
# These lines OCR as normal rows (e.g. "CGST  2.5%  {12.25") but they
# aren't menu items -- they're bill-level charges. Rather than discard
# them as noise (the old behavior), we detect and route them into a
# separate "charges" bucket so the frontend can auto-fill the Extra
# Charges section instead of the user typing these in by hand.
#
# Checked in this order because "service tax" contains the substring
# "tax" -- checking service first ensures it's bucketed as a service
# charge rather than misfiled as a generic tax line.
_CHARGE_PATTERNS = [
    ("service", re.compile(r'service\s*(charge|chg|tax)?', re.IGNORECASE)),
    ("tip", re.compile(r'\btip\b|gratuity', re.IGNORECASE)),
    ("tax", re.compile(r'\b(cgst|sgst|gst|vat|tax)\b', re.IGNORECASE)),
]

# Lines that are pure noise -- neither a menu item nor a charge
# (headers, the grand total line itself, invoice metadata, etc.)
_NOISE_KEYWORDS = re.compile(
    r'^(total|subtotal|sub-total|grand total|net amount|qty|item|'
    r'thank you|welcome|invoice|bill no|table no|date|time|gstin|'
    r'description|rate|round\s?off|discount)',
    re.IGNORECASE
)


def _classify_charge(text):
    """Returns 'service', 'tip', or 'tax' if the text matches a known
    bill-level charge label, otherwise None."""
    for charge_type, pattern in _CHARGE_PATTERNS:
        if pattern.search(text):
            return charge_type
    return None


def parse_table_rows(rows, header_idx, columns):
    """
    Column-aware parsing for tabular receipts: assigns each token in a
    row to its nearest header column by x-position, rather than assuming
    "first number = price" — which can't distinguish RATE from QTY from
    AMOUNT when all three are just numbers on the same row.

    Returns:
        (items, charges) where:
        - items: [{"name": str, "price": float}, ...] real menu items
        - charges: {"tax": float, "service": float, "tip": float} totals
    """
    items = []
    charges = {"tax": 0.0, "service": 0.0, "tip": 0.0}

    for row in rows[header_idx + 1:]:
        buckets = {"name": [], "rate": [], "qty": [], "amount": []}
        for x, text in row:
            role = _nearest_column(x, columns)
            buckets[role].append(text)

        name = " ".join(buckets["name"]).strip()
        name = re.sub(r'[\s\-:.]+$', '', name)

        if not name:
            continue

        amount = _parse_number(" ".join(buckets["amount"])) if buckets["amount"] else None
        rate = _parse_number(" ".join(buckets["rate"])) if buckets["rate"] else None
        qty = _parse_number(" ".join(buckets["qty"])) if buckets["qty"] else None

        # Prefer AMOUNT (corrected for currency misreads), fall back to RATE x QTY
        if amount is not None:
            corrected = _strip_possible_currency_misread(amount, rate)
            row_value = corrected if corrected is not None else amount
        elif rate is not None:
            q = qty if qty and qty > 0 else 1
            row_value = rate * q
        else:
            row_value = None

        charge_type = _classify_charge(name)
        if charge_type:
            if row_value is not None and 0 < row_value <= 100000:
                charges[charge_type] += row_value
            continue

        if _NOISE_KEYWORDS.match(name) or len(name) < 2:
            continue

        if row_value is None or row_value <= 0 or row_value > 100000:
            continue

        items.append({"name": name, "price": round(row_value, 2)})

    return items, charges


# --- Legacy single-column parsing (non-tabular receipts) ---

# Matches a TRAILING price at the end of a line, e.g. "Chicken Biryani  250.00"
_TRAILING_PRICE_PATTERN = re.compile(
    r'[^\d]{0,2}(\d+(?:[.,]\d{1,2})?)\s*/?-?\s*$',
    re.IGNORECASE
)


def parse_receipt_lines(lines: list[str]):
    """
    Fallback parser for plain single-column receipts (item name and price
    together on one line, no table structure). Used when no header row
    is detected.

    Returns:
        (items, charges) — same shape as parse_table_rows().
    """
    items = []
    charges = {"tax": 0.0, "service": 0.0, "tip": 0.0}

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        match = _TRAILING_PRICE_PATTERN.search(line)
        price = None
        if match:
            price_str = match.group(1).replace(',', '.')
            try:
                price = float(price_str)
            except ValueError:
                price = None

        name = line[:match.start()].strip() if match else line
        name = re.sub(r'[\s\-:.]+$', '', name)

        if not name:
            continue

        charge_type = _classify_charge(name)
        if charge_type:
            if price is not None and 0 < price <= 100000:
                charges[charge_type] += price
            continue

        if _NOISE_KEYWORDS.match(name) or len(name) < 2:
            continue

        if price is None or price <= 0 or price > 100000:
            continue

        items.append({"name": name, "price": round(price, 2)})

    return items, charges


def process_receipt_image(image_bytes: bytes) -> dict:
    """
    Orchestrates the full pipeline. Auto-detects whether the receipt is
    tabular (DESCRIPTION/RATE/QTY/AMOUNT columns) or plain single-column,
    routes to the matching parser, tags items via Gemini, and returns
    both the categorized items and any detected tax/service/tip charges.

    Returns:
        {
            "items": [{"name": str, "price": float, "tag": str}, ...],
            "charges": {"tax": float, "service": float, "tip": float}
        }
    """
    preprocessed = preprocess_image(image_bytes)

    results = _reader.readtext(preprocessed, detail=1, paragraph=False)
    boxes = [(bbox, text.strip(), conf) for bbox, text, conf in results if conf > 0.35 and text.strip()]
    rows = _group_rows(boxes, row_tolerance=15)

    header_idx, columns = _find_header_columns(rows)

    if header_idx is not None:
        raw_items, charges = parse_table_rows(rows, header_idx, columns)
    else:
        lines = [" ".join(text for _, text in row) for row in rows]
        raw_items, charges = parse_receipt_lines(lines)

    categorized_items = categorize_items(raw_items)

    return {
        "items": categorized_items,
        "charges": {k: round(v, 2) for k, v in charges.items()},
    }