"""
EXIM Insight & Q&A Bot  --  Clean & Derive layer
H.G. Hawa & Co.

Architecture Step 2 -- CLEAN & DERIVE.  Rules implemented (from the blueprint):

  Name normalisation   Collapse "NINE DRAGONS", "Nine Dragons Paper Ltd" -> one
                       entity, so counts and ranks are correct.
  Date parsing         Standardise dates -> enables frequency / recency / trend.
  Grade/GSM/BF derive  Keyword-extract from Product Description; everything is
                       flagged "(derived)" at source.
  Unit-price sanity    Cross-check Unit Price vs Value / Quantity; recover a
                       price when missing; flag outliers instead of trusting.
  Country normalise    Fix spellings ("Sri lanka"/"SRI LANKA") so roll-ups clean.
  Missing-data honesty Blank fields stay blank -- never fabricated.

Output: a tidy DataFrame with canonical + derived columns, ready for the store.
"""

from __future__ import annotations

import re
import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #

def _to_number(series: pd.Series) -> pd.Series:
    """Parse messy numeric text ('1,440', ' 12.5 ', '', 'N/A') -> float/NaN."""
    if series is None:
        return pd.Series(dtype="float64")
    s = series.astype("string")
    s = s.str.replace(",", "", regex=False)
    s = s.str.replace(r"[^0-9.\-]", "", regex=True)
    s = s.replace({"": np.nan, "-": np.nan, ".": np.nan})
    return pd.to_numeric(s, errors="coerce")


def _clean_text(series: pd.Series) -> pd.Series:
    """Trim, collapse inner whitespace; blank-ish -> NA (missing-data honesty)."""
    if series is None:
        return pd.Series(dtype="string")
    s = series.astype("string").str.strip()
    s = s.str.replace(r"\s+", " ", regex=True)
    s = s.replace({"": pd.NA, "N/A": pd.NA, "NA": pd.NA, "NULL": pd.NA,
                   "-": pd.NA, "--": pd.NA, "nan": pd.NA, "NAN": pd.NA})
    return s


# --------------------------------------------------------------------------- #
#  Entity-name normalisation
# --------------------------------------------------------------------------- #

_CORP_SUFFIXES = [
    "private limited", "pvt ltd", "pvt. ltd", "pvt", "limited", "ltd", "llp",
    "llc", "inc", "incorporated", "co ltd", "company limited", "company", "co",
    "corp", "corporation", "gmbh", "s a", "sa", "s p a", "spa", "bv", "nv",
    "sdn bhd", "bhd", "pte ltd", "pte", "plc", "ag", "kg", "srl", "sarl",
    "trading", "international", "intl", "industries", "industry", "enterprises",
    "enterprise", "exports", "export", "imports", "import", "paper", "papers",
    "group", "holdings", "logistics",
]
_CORP_SUFFIX_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(_CORP_SUFFIXES, key=len, reverse=True)) + r")\b"
)


def _normalise_entity(name: object) -> object:
    """Produce a canonical key for a company name so variant spellings collapse
    to one entity.  The *display* name is kept separately (first-seen original)."""
    if name is None or (isinstance(name, float) and np.isnan(name)) or pd.isna(name):
        return pd.NA
    t = str(name).upper().strip()
    t = re.sub(r"[.,/&()+'\"-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    low = t.lower()
    low = _CORP_SUFFIX_RE.sub(" ", low)
    low = re.sub(r"\s+", " ", low).strip()
    return low if low else t.lower()


def _best_display_names(original: pd.Series, key: pd.Series) -> pd.Series:
    """For each normalised key, choose a clean human display name: the most
    frequent original spelling (title-cased)."""
    df = pd.DataFrame({"orig": original, "key": key}).dropna(subset=["key"])
    if df.empty:
        return pd.Series(index=original.index, dtype="string")
    # most common original per key
    best = (df.dropna(subset=["orig"])
              .groupby(["key", "orig"]).size().reset_index(name="n")
              .sort_values(["key", "n"], ascending=[True, False])
              .drop_duplicates("key").set_index("key")["orig"])
    def _disp(o):
        s = str(o).strip()
        # keep ALLCAPS acronyms, otherwise title-case tidy
        return s if s.isupper() and len(s) <= 5 else s.title()
    best = best.map(_disp)
    return key.map(best).astype("string")


# --------------------------------------------------------------------------- #
#  Country normalisation
# --------------------------------------------------------------------------- #

_COUNTRY_FIX = {
    "USA": "United States", "U S A": "United States", "US": "United States",
    "UNITED STATES OF AMERICA": "United States", "UNITED STATES": "United States",
    "UK": "United Kingdom", "U K": "United Kingdom", "GREAT BRITAIN": "United Kingdom",
    "UAE": "United Arab Emirates", "U A E": "United Arab Emirates",
    "KOREA": "South Korea", "KOREA SOUTH": "South Korea", "REPUBLIC OF KOREA": "South Korea",
    "KOREA REPUBLIC OF": "South Korea", "SOUTH KOREA": "South Korea",
    "VIET NAM": "Vietnam", "VIETNAM": "Vietnam",
    "SRI LANKA": "Sri Lanka", "SRILANKA": "Sri Lanka",
    "CHINA PR": "China", "P R CHINA": "China", "PR CHINA": "China", "MAINLAND CHINA": "China",
    "HONG KONG SAR": "Hong Kong", "RUSSIAN FEDERATION": "Russia",
    "TAIWAN ROC": "Taiwan", "CHINESE TAIPEI": "Taiwan",
    "CZECHIA": "Czech Republic", "DEUTSCHLAND": "Germany", "NETHERLAND": "Netherlands",
    "THE NETHERLANDS": "Netherlands", "IVORY COAST": "Cote d'Ivoire",
}


def _normalise_country(name: object) -> object:
    if name is None or pd.isna(name):
        return pd.NA
    t = str(name).strip()
    key = re.sub(r"[.\-]", " ", t).upper()
    key = re.sub(r"\s+", " ", key).strip()
    if key in _COUNTRY_FIX:
        return _COUNTRY_FIX[key]
    # default: Title Case, but preserve short acronyms
    return t.title() if not (t.isupper() and len(t) <= 3) else t


# --------------------------------------------------------------------------- #
#  Grade / GSM / BF derivation (from free-text Product Description)
#  Everything here is INDICATIVE -- flagged "(derived)" everywhere it shows.
# --------------------------------------------------------------------------- #

# Ordered: more specific grades first.
_GRADE_PATTERNS: list[tuple[str, str]] = [
    ("White Top Testliner", r"white\s*top\s*test\s*liner|wtl\b"),
    ("White Top Kraftliner", r"white\s*top\s*kraft"),
    ("White Top Liner",      r"white\s*top"),
    ("Testliner",            r"test\s*liner|testliner|\btl\b"),
    ("Kraftliner",           r"kraft\s*liner|kraftliner|\bkl\b"),
    ("Sack Kraft",           r"sack\s*kraft|sackkraft"),
    ("Kraft Paper",          r"kraft\s*paper|\bkraft\b"),
    ("Fluting / Medium",     r"fluting|corrugating\s*medium|\bmedium\b|semi\s*chem"),
    ("MG Paper",             r"\bmg\b|machine\s*glazed"),
    ("Duplex Board",         r"duplex"),
    ("SBS Board",            r"\bsbs\b|solid\s*bleached"),
    ("Folding Box Board",    r"\bfbb\b|folding\s*box"),
    ("Coated Paper/Board",   r"coated"),
    ("Uncoated Paper/Board", r"uncoated"),
    ("Newsprint",            r"news\s*print|newsprint"),
    ("Tissue",               r"tissue"),
    ("Corrugated",           r"corrugat"),
    ("Paperboard",           r"paper\s*board|paperboard"),
]
_GRADE_COMPILED = [(label, re.compile(pat, re.I)) for label, pat in _GRADE_PATTERNS]

_GSM_RE = re.compile(r"(\d{2,4})\s*(?:gsm|g/?m2|g/?m²|gm2|grm)\b", re.I)
_GSM_RANGE_RE = re.compile(r"(\d{2,4})\s*[-/]\s*(\d{2,4})\s*(?:gsm|g/?m2|g/?m²|gm2)\b", re.I)
_BF_RE = re.compile(r"(?:\bbf\s*[:=]?\s*(\d{2,3})\b|\b(\d{2,3})\s*bf\b)", re.I)


def _derive_grade(desc: object) -> object:
    if desc is None or pd.isna(desc):
        return pd.NA
    s = str(desc)
    for label, rx in _GRADE_COMPILED:
        if rx.search(s):
            return label
    return pd.NA


def _derive_gsm(desc: object) -> object:
    if desc is None or pd.isna(desc):
        return pd.NA
    s = str(desc)
    m = _GSM_RANGE_RE.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return float(round((a + b) / 2))
    m = _GSM_RE.search(s)
    if m:
        v = int(m.group(1))
        if 20 <= v <= 1500:
            return float(v)
    return pd.NA


def _derive_bf(desc: object) -> object:
    if desc is None or pd.isna(desc):
        return pd.NA
    s = str(desc)
    m = _BF_RE.search(s)
    if m:
        v = int(m.group(1) or m.group(2))
        if 10 <= v <= 60:
            return float(v)
    return pd.NA


# --------------------------------------------------------------------------- #
#  Main entry
# --------------------------------------------------------------------------- #

_RETURNED_COLUMNS_NOTE = """Returned frame columns (all lower_snake):
  date (datetime64), year_month (str), shipment_id, hs_code, hs4, industry,
  product_desc,
  seller_raw, seller, seller_key, seller_country, seller_state, seller_city,
  seller_address, origin_port,
  buyer_raw, buyer, buyer_key, buyer_country, buyer_state, buyer_city,
  buyer_address, destination_port,
  unit, quantity, value_usd, unit_price, unit_price_src, price_flag,
  grade_derived, gsm_derived, bf_derived,
  rodtep, drawback, trade_lane
"""


def clean_and_derive(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Takes the canonical-renamed frame from ingest and returns (clean_frame,
    report).  `report` carries derivation coverage and data-quality flags for
    honest display in the UI."""
    df = frame.copy()

    def col(name):
        return df[name] if name in df.columns else pd.Series([pd.NA] * len(df), index=df.index)

    out = pd.DataFrame(index=df.index)

    # ---- dates ----
    raw_date = col("date")
    dt = pd.to_datetime(raw_date, errors="coerce", dayfirst=False)
    # retry day-first for the ones that failed (handles DD/MM/YYYY exports)
    if dt.isna().any():
        dt2 = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
        dt = dt.fillna(dt2)
    out["date"] = dt
    out["year_month"] = dt.dt.strftime("%Y-%m")

    # ---- plain text fields ----
    out["shipment_id"] = _clean_text(col("shipment_id"))
    out["hs_code"] = _clean_text(col("hs_code"))
    out["hs4"] = out["hs_code"].str.extract(r"(\d{4})", expand=False)
    out["industry"] = _clean_text(col("industry"))
    out["product_desc"] = _clean_text(col("product_desc"))

    # ---- seller entity ----
    seller_raw = _clean_text(col("seller"))
    seller_key = seller_raw.map(_normalise_entity).astype("string")
    out["seller_raw"] = seller_raw
    out["seller_key"] = seller_key
    out["seller"] = _best_display_names(seller_raw, seller_key)
    out["seller_country"] = _clean_text(col("seller_country")).map(_normalise_country).astype("string")
    out["seller_state"] = _clean_text(col("seller_state"))
    out["seller_city"] = _clean_text(col("seller_city"))
    out["seller_address"] = _clean_text(col("seller_address"))
    out["origin_port"] = _clean_text(col("origin_port")).map(
        lambda x: pd.NA if pd.isna(x) else str(x).title()).astype("string")

    # ---- buyer entity ----
    buyer_raw = _clean_text(col("buyer"))
    buyer_key = buyer_raw.map(_normalise_entity).astype("string")
    out["buyer_raw"] = buyer_raw
    out["buyer_key"] = buyer_key
    out["buyer"] = _best_display_names(buyer_raw, buyer_key)
    out["buyer_country"] = _clean_text(col("buyer_country")).map(_normalise_country).astype("string")
    out["buyer_state"] = _clean_text(col("buyer_state"))
    out["buyer_city"] = _clean_text(col("buyer_city"))
    out["buyer_address"] = _clean_text(col("buyer_address"))
    out["destination_port"] = _clean_text(col("destination_port")).map(
        lambda x: pd.NA if pd.isna(x) else str(x).title()).astype("string")

    # ---- commercials ----
    out["unit"] = _clean_text(col("unit")).map(
        lambda x: pd.NA if pd.isna(x) else str(x).upper()).astype("string")
    qty = _to_number(col("quantity"))
    val = _to_number(col("value_usd"))
    price = _to_number(col("unit_price"))
    out["quantity"] = qty
    out["value_usd"] = val

    # ---- unit-price sanity ----
    # Prefer the stated price; if missing/zero but value&qty exist, recover it;
    # then flag rows where stated price diverges wildly from value/qty.
    recovered = np.where((qty.notna()) & (qty != 0) & (val.notna()),
                         val / qty.replace(0, np.nan), np.nan)
    recovered = pd.Series(recovered, index=df.index)

    # work in plain numpy to avoid nullable-boolean ambiguity
    price_np = price.to_numpy(dtype="float64", na_value=np.nan)
    rec_np = recovered.to_numpy(dtype="float64", na_value=np.nan)
    stated_ok = (~np.isnan(price_np)) & (price_np > 0)
    rec_ok = ~np.isnan(rec_np)
    final_price = np.where(stated_ok, price_np, np.where(rec_ok, rec_np, np.nan))
    src = np.where(stated_ok, "stated", np.where(rec_ok, "derived(value/qty)", "unavailable"))
    out["unit_price"] = pd.Series(final_price, index=df.index)
    out["unit_price_src"] = pd.Series(src, index=df.index, dtype="string")

    # outlier flag: stated price vs recovered differ by >5x (either direction)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(rec_ok & (rec_np != 0), price_np / rec_np, np.nan)
    outlier = stated_ok & rec_ok & ((ratio > 5) | (ratio < 0.2))
    flag = np.where(outlier, "price_outlier", "")
    out["price_flag"] = pd.Series(flag, index=df.index, dtype="string")

    # ---- derived grade / gsm / bf (indicative) ----
    out["grade_derived"] = out["product_desc"].map(_derive_grade).astype("string")
    out["gsm_derived"] = out["product_desc"].map(_derive_gsm)
    out["bf_derived"] = out["product_desc"].map(_derive_bf)

    # ---- optional export commercials ----
    out["rodtep"] = _to_number(col("rodtep")) if "rodtep" in df.columns else np.nan
    out["drawback"] = _to_number(col("drawback")) if "drawback" in df.columns else np.nan

    # ---- trade lane ----
    oc = out["seller_country"].fillna("Unknown")
    dc = out["buyer_country"].fillna("Unknown")
    out["trade_lane"] = (oc.astype(str) + " → " + dc.astype(str)).astype("string")

    # ---------------- data-quality report (honest flags) ----------------
    report = quality_report(out, has_rodtep="rodtep" in frame.columns,
                            has_drawback="drawback" in frame.columns)
    return out, report


def quality_report(out: pd.DataFrame, has_rodtep=False, has_drawback=False) -> dict:
    """Honest data-quality summary for a cleaned frame. Works on a single file
    or on several months concatenated together (multi-period)."""
    n = len(out)
    def pct_filled(c):
        return round(float(out[c].notna().mean()) * 100, 1) if c in out and n else 0.0

    # periods covered (year-month list) -- used by the multi-period UI
    periods = []
    if "year_month" in out:
        periods = sorted([p for p in out["year_month"].dropna().unique().tolist()])

    return {
        "rows": n,
        "date_parsed_pct": pct_filled("date"),
        "grade_derived_pct": pct_filled("grade_derived"),
        "gsm_derived_pct": pct_filled("gsm_derived"),
        "bf_derived_pct": pct_filled("bf_derived"),
        "value_present_pct": round(float((out["value_usd"].fillna(0) > 0).mean()) * 100, 1) if n else 0.0,
        "price_present_pct": round(float((out["unit_price"].fillna(0) > 0).mean()) * 100, 1) if n else 0.0,
        "seller_present_pct": pct_filled("seller"),
        "buyer_present_pct": pct_filled("buyer"),
        "price_outliers": int((out["price_flag"] == "price_outlier").sum()) if "price_flag" in out else 0,
        "has_rodtep": bool(has_rodtep),
        "has_drawback": bool(has_drawback),
        "periods": periods,
        "period_count": len(periods),
        "fields_filled_pct": {c: pct_filled(c) for c in out.columns},
    }
