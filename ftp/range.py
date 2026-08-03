"""HTTP `Range:` header parser for the inline streaming endpoint.

Pure stdlib, no I/O, async-safe (it is sync on purpose; the caller runs it
inside an awaited request handler). Handles three byte-range forms:

- ``bytes=N-M``  — closed range.
- ``bytes=N-``   — open-ended (from N to EOF).
- ``bytes=-N``   — suffix length (the last N bytes).

Returns ``(start, end_inclusive, status)`` where ``status`` is ``206`` for a
parsed partial request and ``200`` for missing/invalid input (caller may
treat the latter as "send the whole file").
"""
from __future__ import annotations


def parse_range(value: str | None, size: int) -> tuple[int, int, int]:
    if not value or not value.startswith("bytes="):
        return 0, size - 1, 200
    spec = value[6:].strip()
    if "," in spec:
        return 0, size - 1, 416
    dash = spec.find("-")
    if dash == -1:
        return 0, size - 1, 200
    start_text = spec[:dash].strip()
    end_text = spec[dash + 1:].strip()
    if start_text == "":
        try:
            length = int(end_text)
        except (TypeError, ValueError):
            return 0, size - 1, 200
        if length <= 0 or size <= 0:
            return 0, size - 1, 200
        start = max(0, size - length)
        end = size - 1
    else:
        try:
            start = int(start_text)
        except (TypeError, ValueError):
            return 0, size - 1, 200
        if end_text == "":
            end = size - 1
        else:
            try:
                end = int(end_text)
            except (TypeError, ValueError):
                return 0, size - 1, 200
    start = max(0, start)
    end = min(size - 1, end)
    if size <= 0 or start >= size or end < start:
        return 0, size - 1, 416
    return start, end, 206


__all__ = ("parse_range",)
