from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

def _escape_tag(v: str) -> str:
    """Escape tag values per the Influx line protocol rules.

    Args:
        v (str): Raw tag value.

    Returns:
        str: Escaped tag string safe to embed in line protocol.
    """
    return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

def _escape_str_field(v: str) -> str:
    """Escape string field values for line protocol serialization.

    Args:
        v (str): Raw string field value.

    Returns:
        str: Escaped string safe to surround with double quotes.
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"')

def to_ns(dt: datetime) -> int:
    """Convert a ``datetime`` instance to an integer nanosecond timestamp.

    Args:
        dt (datetime): Timestamp to convert. Naive instances are assumed UTC.

    Returns:
        int: Unix epoch nanoseconds representation.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)

def parse_timestamp(value) -> datetime:
    """Normalize epoch or ISO8601 input into a timezone-aware ``datetime``.

    Args:
        value (int | float | str | datetime): Epoch seconds, ISO string, or
            ``datetime`` instance to normalize.

    Returns:
        datetime: Value coerced to UTC timezone.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
