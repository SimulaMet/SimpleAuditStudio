"""Helpers for user-downloadable exports."""

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formula injection in a CSV cell.

    Run names, scenario titles and judge summaries are user- or model-written;
    a cell starting with ``=``/``+``/``-``/``@`` would execute as a formula when
    the export is opened in Excel or Sheets. Non-strings pass through unchanged.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def buffered(chunks, size: int = 64 * 1024):
    """Join many small strings into ~``size`` pieces for a streaming response.

    JSON/CSV writers emit tiny fragments; sending each as its own WSGI write
    would be slow. Yields UTF-8 bytes.
    """
    buf: list[str] = []
    length = 0
    for chunk in chunks:
        buf.append(chunk)
        length += len(chunk)
        if length >= size:
            yield "".join(buf).encode("utf-8")
            buf, length = [], 0
    if buf:
        yield "".join(buf).encode("utf-8")


def json_chunks(document, *, indent: int | None = 2):
    """Encode ``document`` incrementally (no single giant string in memory)."""
    import json

    return json.JSONEncoder(indent=indent, ensure_ascii=False).iterencode(document)


def json_array_chunks(items, *, indent: int | None = 2):
    """Stream an iterable of JSON-able items as one ``[...]`` array."""
    import json

    encoder = json.JSONEncoder(indent=indent, ensure_ascii=False)
    pad = " " * indent if indent else ""
    first = True
    yield "["
    for item in items:
        yield ("\n" if first else ",\n") if indent else ("" if first else ",")
        first = False
        encoded = "".join(encoder.iterencode(item))
        yield pad + encoded.replace("\n", "\n" + pad) if indent else encoded
    yield "\n]" if indent and not first else "]"


def csv_rows(header: list, rows):
    """Yield CSV text line by line for ``header`` then each row in ``rows``."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate()
        writer.writerow(row)
        yield buf.getvalue()
