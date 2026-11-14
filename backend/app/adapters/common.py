"""Cell-level helpers shared by the donor adapters."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_NBSP_RE = re.compile(r"[   ]")
_SPACES_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str | None:
    """Text cell -> stripped string with nbsp/newlines collapsed; empty -> None.

    Integral floats become integers ("1.0" -> "1"), because Excel stores plain numbers
    (order numbers, invoice numbers) as floats.
    """
    if value is None or isinstance(value, bool):
        return None if value is None else str(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = _SPACES_RE.sub(" ", _NBSP_RE.sub(" ", str(value))).strip()
    return text or None


def to_decimal(value: Any) -> Decimal | None:
    """Numeric cell -> Decimal (via str, so no binary float noise); blank/error/text -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = clean_text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def normalize_order_key(order_no: str | None) -> str | None:
    """Canonical order key: no whitespace, upper case, decimal comma -> dot; "N/A" -> None."""
    if order_no is None:
        return None
    key = _SPACES_RE.sub("", order_no).upper().replace(",", ".")
    if key in {"", "N/A", "NA", "N.A.", "-"}:
        return None
    return key


def is_subtotal_child(code: str, all_codes: set[str]) -> bool:
    """True if some other code is a child of `code` (A.III has A.III.1 / A.III.2)."""
    prefix = code + "."
    return any(other.startswith(prefix) for other in all_codes)


def find_sheet_name(sheet_names: list[str], target: str) -> str:
    """Sheet lookup tolerant to case and surrounding spaces ("X.B Listado de comprobantes ")."""
    wanted = target.strip().casefold()
    for name in sheet_names:
        if name.strip().casefold() == wanted:
            return name
    raise KeyError(f"Sheet '{target}' not found; available: {sheet_names}")
