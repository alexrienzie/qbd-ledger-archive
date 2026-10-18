"""Exact money and date handling.

Every amount in the archive is an integer number of cents, converted with
Decimal so nothing is ever rounded through a float. Dates are ISO strings.
"""
import re
from decimal import Decimal, InvalidOperation

_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cents(value):
    """'1,234.5' -> 123450; '' / None -> None. Raises on garbage, so a
    malformed amount stops the load instead of becoming zero."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "":
        return None
    try:
        d = Decimal(s) * 100
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {value!r}") from e
    q = d.to_integral_value()
    if q != d:
        # QuickBooks amounts carry at most two decimals; anything finer means
        # we are reading the wrong column (a rate or a quantity).
        raise ValueError(f"amount has sub-cent precision: {value!r}")
    return int(q)


def norm_date(value):
    """'3/7/2019' or '2019-03-07' -> '2019-03-07'; anything else -> None."""
    s = (value or "").strip()
    m = _US_DATE.match(s)
    if m:
        mo, dy, yr = int(m[1]), int(m[2]), int(m[3])
        if yr < 100:
            yr += 2000
        return f"{yr:04d}-{mo:02d}-{dy:02d}"
    if _ISO_DATE.match(s):
        return s
    return None


def fmt(c):
    """Cents -> '1234.56' for CSV output (no thousands separator)."""
    if c is None:
        return ""
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"
