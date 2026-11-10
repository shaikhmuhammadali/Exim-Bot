"""
EXIM Insight & Q&A Bot  --  Ingestion layer
H.G. Hawa & Co.

Job of this module (Architecture Step 1 -- INGEST):
    * Read any EXIM trade report (.xlsx / .xls / .csv), however messy.
    * Survive broken Excel exports (invalid stylesheet colours, title banners,
      header row not on line 1, thousands separators, blank columns).
    * Detect the real header row automatically.
    * Lock the EXACT headers found in the file and map them to canonical fields,
      flagging any column it does not recognise -- never silently wrong.

Nothing here invents data.  Blank stays blank.
"""

from __future__ import annotations

import io
import os
import re
import csv
import zipfile
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


# --------------------------------------------------------------------------- #
#  Canonical schema  --  the vocabulary the rest of the bot speaks.
#  Each canonical field lists the header spellings we accept for it.
#  (All comparisons are done on an upper-cased, punctuation-stripped key.)
# --------------------------------------------------------------------------- #

CANONICAL_FIELDS: dict[str, list[str]] = {
    "date":              ["DATE", "SHIPMENT DATE", "TRADE DATE", "BILL DATE"],
    "shipment_id":       ["SHIPMENT ID", "SHIPMENTID", "BILL NO", "BL NO", "BILL OF LADING", "INVOICE NO"],
    "hs_code":           ["HS CODE", "HSCODE", "HS", "HSN", "HSN CODE", "TARIFF"],
    "industry":          ["INDUSTRY", "SECTOR"],
    "product_desc":      ["PRODUCT DESCRIPTION", "PRODUCT DESC", "DESCRIPTION", "GOODS DESCRIPTION",
                          "ITEM DESCRIPTION", "COMMODITY"],

    "seller":            ["SELLER", "SUPPLIER", "SHIPPER", "EXPORTER", "SELLER NAME", "CONSIGNOR"],
    "seller_country":    ["SELLER COUNTRY", "SUPPLIER COUNTRY", "EXPORTER COUNTRY", "ORIGIN COUNTRY",
                          "COUNTRY OF ORIGIN", "SHIPPER COUNTRY"],
    "seller_state":      ["SELLER STATE", "SUPPLIER STATE", "EXPORTER STATE", "SHIPPER STATE"],
    "seller_city":       ["SELLER CITY", "SUPPLIER CITY", "EXPORTER CITY", "SHIPPER CITY"],
    "seller_address":    ["SELLER ADDRESS", "SUPPLIER ADDRESS", "EXPORTER ADDRESS", "SHIPPER ADDRESS"],
    "origin_port":       ["ORIGIN PORT", "PORT OF ORIGIN", "PORT OF LOADING", "LOADING PORT", "POL"],

    "buyer":             ["BUYER", "IMPORTER", "CONSIGNEE", "BUYER NAME", "PURCHASER"],
    "buyer_country":     ["BUYER COUNTRY", "IMPORTER COUNTRY", "CONSIGNEE COUNTRY", "DESTINATION COUNTRY",
                          "COUNTRY OF DESTINATION"],
    "buyer_state":       ["BUYER STATE", "IMPORTER STATE", "CONSIGNEE STATE"],
    "buyer_city":        ["BUYER CITY", "IMPORTER CITY", "CONSIGNEE CITY"],
    "buyer_address":     ["BUYER ADDRESS", "IMPORTER ADDRESS", "CONSIGNEE ADDRESS"],
    "destination_port":  ["DESTINATION PORT", "PORT OF DESTINATION", "PORT OF DISCHARGE",
                          "DISCHARGE PORT", "POD"],

    "unit":              ["UNIT", "UOM", "UNIT OF MEASURE", "QUANTITY UNIT"],
    "quantity":          ["QUANTITY", "QTY", "QUANTITIES", "NET QUANTITY"],
    "value_usd":         ["VALUE(USD)", "VALUE USD", "VALUE (USD)", "VALUE", "USD VALUE",
                          "TOTAL VALUE", "CIF VALUE", "FOB VALUE", "VALUE IN USD"],
    "unit_price":        ["UNIT PRICE", "UNITPRICE", "PRICE", "RATE", "PRICE PER UNIT", "UNIT RATE"],

    # Commercials that appear in export reports (absent in some import files).
    "rodtep":            ["RODTEP", "RODTEP BENEFIT", "RO DTEP"],
    "drawback":          ["DRAWBACK", "DUTY DRAWBACK", "DBK"],
}

# Fields we truly need before we can call something an EXIM shipment ledger.
MIN_REQUIRED = {"product_desc", "quantity"}
# A richer set we hope to see -- used only to score candidate header rows.
HEADER_HINTS = set(CANONICAL_FIELDS.keys())


def _norm_key(s: object) -> str:
    """Normalise a header cell to a comparable key: upper, collapse spaces,
    drop punctuation except parentheses meaning."""
    if s is None:
        return ""
    t = str(s).strip().upper()
    t = t.replace("_", " ")
    t = re.sub(r"[^A-Z0-9() ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Build a fast reverse lookup: normalised spelling -> canonical field.
_SPELLING_TO_FIELD: dict[str, str] = {}
for _field, _spellings in CANONICAL_FIELDS.items():
    for _sp in _spellings:
        _SPELLING_TO_FIELD[_norm_key(_sp)] = _field


# --------------------------------------------------------------------------- #
#  Tolerant workbook repair (handles the real-world broken EXIM exports)
# --------------------------------------------------------------------------- #

def _repair_xlsx_to_tempfile(path: str) -> str:
    """Some EXIM portals export .xlsx whose xl/styles.xml carries invalid aRGB
    colour values (e.g. 'FFFF000000') that openpyxl refuses to parse.  We copy
    the workbook to a temp file, sanitising every rgb="..." to a valid 8-char
    hex, so the data can be read.  Only styles are touched; cell data is
    byte-for-byte preserved."""
    def _fix_rgb(m: re.Match) -> str:
        hexonly = re.sub(r"[^0-9A-Fa-f]", "", m.group(1))
        hexonly = hexonly[-8:] if len(hexonly) >= 8 else hexonly.rjust(8, "0")
        return 'rgb="' + hexonly.upper() + '"'

    fd, tmp = tempfile.mkstemp(suffix=".xlsx", prefix="exim_repaired_")
    os.close(fd)
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("styles.xml"):
                txt = data.decode("utf-8", "replace")
                txt = re.sub(r'rgb="([^"]*)"', _fix_rgb, txt)
                data = txt.encode("utf-8")
            zout.writestr(item, data)
    return tmp


def _read_raw(path: str) -> pd.DataFrame:
    """Read the file into a header-less DataFrame of strings, trying the
    forgiving paths first.  Returns raw cells; header detection happens next."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        # sniff delimiter, read everything as text
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            sample = fh.read(8192)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            sep = dialect.delimiter
        except Exception:
            sep = ","
        return pd.read_csv(path, header=None, dtype=str, sep=sep,
                           keep_default_na=False, na_values=[""], engine="python")

    if ext == ".xls":
        return pd.read_excel(path, header=None, dtype=str, engine="xlrd")

    # .xlsx -- stream with openpyxl read_only (much lower peak memory than
    # pd.read_excel, which matters on small cloud instances). Repair the broken
    # stylesheet first if needed. Fall back to pandas only if streaming fails.
    for p in (path, None):
        rp = p if p is not None else _repair_xlsx_to_tempfile(path)
        try:
            df = _read_xlsx_stream(rp)
            if df is not None:
                return df
        except Exception:
            pass
        finally:
            if p is None:
                try: os.remove(rp)
                except OSError: pass
    # last resort: full pandas load (repaired)
    repaired = _repair_xlsx_to_tempfile(path)
    try:
        return pd.read_excel(repaired, header=None, dtype=str, engine="openpyxl")
    finally:
        try: os.remove(repaired)
        except OSError: pass


def _read_xlsx_stream(path: str):
    """Low-memory .xlsx reader: openpyxl read_only streaming into a DataFrame of
    strings. Holds one row at a time while reading (no full workbook graph)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = []
        maxc = 0
        for r in ws.iter_rows(values_only=True):
            row = ["" if v is None else str(v) for v in r]
            maxc = max(maxc, len(row))
            rows.append(row)
    finally:
        wb.close()
    if not rows:
        return None
    for r in rows:                      # pad ragged rows
        if len(r) < maxc:
            r.extend([""] * (maxc - len(r)))
    return pd.DataFrame(rows, dtype=object)


def _score_header_row(cells: list[str]) -> tuple[int, dict[int, str]]:
    """How many canonical fields does this row's cells resolve to?
    Returns (match_count, {col_index: canonical_field})."""
    mapping: dict[int, str] = {}
    seen: set[str] = set()
    for idx, cell in enumerate(cells):
        key = _norm_key(cell)
        if not key:
            continue
        field_name = _SPELLING_TO_FIELD.get(key)
        if field_name is None:
            # try a looser contains-match for compound headers
            for spelling, fn in _SPELLING_TO_FIELD.items():
                if spelling and (spelling == key):
                    field_name = fn
                    break
        if field_name and field_name not in seen:
            mapping[idx] = field_name
            seen.add(field_name)
    return len(seen), mapping


# --------------------------------------------------------------------------- #
#  Public result object
# --------------------------------------------------------------------------- #

@dataclass
class IngestResult:
    frame: pd.DataFrame                      # data rows, columns renamed to canonical fields
    header_row_index: int                    # 0-based row where headers were found
    column_map: dict[str, str] = field(default_factory=dict)   # original header -> canonical field
    recognised_fields: list[str] = field(default_factory=list)
    unknown_headers: list[str] = field(default_factory=list)    # headers we could not map (flagged)
    missing_expected: list[str] = field(default_factory=list)   # canonical fields not present in file
    source_name: str = ""
    total_rows: int = 0

    def summary(self) -> dict:
        return {
            "source_name": self.source_name,
            "total_rows": self.total_rows,
            "header_row_index": self.header_row_index,
            "recognised_fields": self.recognised_fields,
            "unknown_headers": self.unknown_headers,
            "missing_expected": self.missing_expected,
            "column_map": self.column_map,
        }


class IngestError(Exception):
    pass


def ingest_file(path: str, source_name: Optional[str] = None,
                max_scan_rows: int = 25) -> IngestResult:
    """Read + header-lock an EXIM report.  Raises IngestError only when the file
    cannot be recognised as a shipment ledger at all."""
    source_name = source_name or os.path.basename(path)
    raw = _read_raw(path)
    if raw.empty:
        raise IngestError("The file appears to be empty.")

    # --- detect the header row: scan the first N rows, pick the best match ---
    best_i, best_score, best_map = -1, -1, {}
    for i in range(min(max_scan_rows, len(raw))):
        cells = ["" if pd.isna(x) else str(x) for x in raw.iloc[i].tolist()]
        score, mapping = _score_header_row(cells)
        if score > best_score:
            best_i, best_score, best_map = i, score, mapping

    if best_score < 2:
        raise IngestError(
            "Could not find a recognisable EXIM header row. Expected columns like "
            "DATE, PRODUCT DESCRIPTION, BUYER, SELLER, QUANTITY, VALUE(USD)."
        )

    header_cells = ["" if pd.isna(x) else str(x).strip() for x in raw.iloc[best_i].tolist()]

    # Build column_map (original header text -> canonical field) and collect unknowns.
    column_map: dict[str, str] = {}
    unknown_headers: list[str] = []
    recognised_fields: list[str] = []
    col_to_field: dict[int, str] = {}
    for idx, original in enumerate(header_cells):
        if idx in best_map:
            fld = best_map[idx]
            col_to_field[idx] = fld
            column_map[original] = fld
            recognised_fields.append(fld)
        else:
            if original:                      # a real, non-blank header we don't know
                unknown_headers.append(original)

    # --- slice the data rows (everything below the header row) ---
    data = raw.iloc[best_i + 1:].reset_index(drop=True)

    # Keep only columns that mapped to a canonical field; rename them.
    keep_cols = sorted(col_to_field.keys())
    data = data.iloc[:, keep_cols].copy()
    data.columns = [col_to_field[c] for c in keep_cols]

    # Drop fully-blank trailing rows (common in Excel exports).
    data = data.replace({"": pd.NA})
    data = data.dropna(how="all").reset_index(drop=True)

    if not MIN_REQUIRED.issubset(set(data.columns)):
        raise IngestError(
            "The file is missing essential columns (needs at least a product "
            "description and a quantity) -- it may not be an EXIM shipment ledger."
        )

    missing_expected = [f for f in HEADER_HINTS if f not in set(data.columns)
                        and f not in ("rodtep", "drawback")]  # rodtep/drawback are optional by design

    return IngestResult(
        frame=data,
        header_row_index=best_i,
        column_map=column_map,
        recognised_fields=recognised_fields,
        unknown_headers=unknown_headers,
        missing_expected=missing_expected,
        source_name=source_name,
        total_rows=len(data),
    )
