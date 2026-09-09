"""Address normalization and formatting utilities."""
from __future__ import annotations


def normalize_address(addr: str) -> str:
    """Normalize an address to lowercase, no prefix, zero-padded to 8 chars.

    Examples:
        >>> normalize_address("0x5E3E90")
        '005e3e90'
        >>> normalize_address("5e3e90")
        '005e3e90'
        >>> normalize_address("0x005E3E90")
        '005e3e90'
    """
    cleaned = addr.strip().lower()
    if ":" in cleaned:
        cleaned = cleaned.rsplit(":", 1)[1]
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    return cleaned.rjust(8, "0")


def format_address(addr: str) -> str:
    """Ensure an address has a ``0x`` prefix and is lowercase.

    Examples:
        >>> format_address("5E3E90")
        '0x5e3e90'
        >>> format_address("0x5E3E90")
        '0x5e3e90'
    """
    cleaned = addr.strip().lower()
    if not cleaned.startswith("0x"):
        cleaned = "0x" + cleaned
    return cleaned



def checked_address(value: object) -> str:
    """Validate external hexadecimal address fields without truncating their width."""
    import re

    if not isinstance(value, str) or re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", value.strip()) is None:
        raise ValueError(f"Invalid hexadecimal address: {value!r}")
    return normalize_address(value)
