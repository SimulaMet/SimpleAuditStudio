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
