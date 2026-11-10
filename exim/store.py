"""
EXIM Insight & Q&A Bot  --  Store layer
H.G. Hawa & Co.

Architecture Step 3 -- STORE.  Loads the cleaned frame into a small local
SQLite database (one table, `shipments`).  SQLite is built into Python, needs
no server, and is fast on lakhs of rows -- exactly the "keep the data in a
small database, run precise queries" design the blueprint calls for.

A read-only connection + a hard SELECT-only guard are provided so the Q&A
engine can run generated SQL safely (no writes, ever).
"""

from __future__ import annotations

import re
import sqlite3
import pandas as pd


TABLE = "shipments"

# Column name -> SQLite type.  Keeps queries predictable.
SCHEMA: dict[str, str] = {
    "row_id":          "INTEGER",
    "date":            "TEXT",      # ISO yyyy-mm-dd
    "year_month":      "TEXT",
    "shipment_id":     "TEXT",
    "hs_code":         "TEXT",
    "hs4":             "TEXT",
    "industry":        "TEXT",
    "product_desc":    "TEXT",
    "seller_raw":      "TEXT",
    "seller":          "TEXT",
    "seller_key":      "TEXT",
    "seller_country":  "TEXT",
    "seller_state":    "TEXT",
    "seller_city":     "TEXT",
    "seller_address":  "TEXT",
    "origin_port":     "TEXT",
    "buyer_raw":       "TEXT",
    "buyer":           "TEXT",
    "buyer_key":       "TEXT",
    "buyer_country":   "TEXT",
    "buyer_state":     "TEXT",
    "buyer_city":      "TEXT",
    "buyer_address":   "TEXT",
    "destination_port":"TEXT",
    "unit":            "TEXT",
    "quantity":        "REAL",
    "value_usd":       "REAL",
    "unit_price":      "REAL",
    "unit_price_src":  "TEXT",
    "price_flag":      "TEXT",
    "grade_derived":   "TEXT",
    "gsm_derived":     "REAL",
    "bf_derived":      "REAL",
    "rodtep":          "REAL",
    "drawback":        "REAL",
    "trade_lane":      "TEXT",
    "source_file":     "TEXT",
}

ALLOWED_COLUMNS = set(SCHEMA.keys())

_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|truncate|grant|revoke|commit|rollback)\b", re.I)


def build_database(df: pd.DataFrame, db_path: str) -> dict:
    """Write the cleaned frame into `db_path`.  Returns a small report with
    indexed columns and row count."""
    data = df.copy()
    data.insert(0, "row_id", range(1, len(data) + 1))

    # dates -> ISO strings for stable text comparison / display
    if "date" in data.columns:
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # keep only known columns, in schema order
    cols = [c for c in SCHEMA if c in data.columns]
    data = data[cols]

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        coldefs = ", ".join(f'"{c}" {SCHEMA[c]}' for c in cols)
        conn.execute(f'CREATE TABLE {TABLE} ({coldefs})')

        # Insert in chunks (pandas NA -> None), so we never materialise the whole
        # table as one giant Python list -- keeps peak memory low on tiny
        # (512MB) cloud instances.
        placeholders = ", ".join("?" for _ in cols)
        insert_sql = (f'INSERT INTO {TABLE} ({", ".join(chr(34)+c+chr(34) for c in cols)}) '
                      f'VALUES ({placeholders})')
        n = len(data)
        CHUNK = 5000
        for start in range(0, n, CHUNK):
            sub = data.iloc[start:start + CHUNK]
            recs = sub.astype(object).where(pd.notna(sub), None).values.tolist()
            conn.executemany(insert_sql, recs)
            del recs, sub

        # helpful indexes for the common query shapes
        for c in ["buyer_key", "seller_key", "buyer_country", "seller_country",
                  "grade_derived", "hs4", "year_month", "destination_port",
                  "origin_port", "unit"]:
            if c in cols:
                conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{c} ON {TABLE}("{c}")')
        conn.commit()
    finally:
        conn.close()

    return {"rows": len(data), "columns": cols, "db_path": db_path}


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open a connection used only for SELECTs.  We also set the authorizer to
    deny anything that is not a pure read."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def _authorizer(action, *_args):
        # Permit only read actions + the function/transaction bookkeeping SELECT needs.
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)
    return conn


def is_safe_select(sql: str) -> tuple[bool, str]:
    """Guard for Q&A-generated SQL: exactly one SELECT statement, no write
    keywords, references only the shipments table."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty query"
    if ";" in s:
        return False, "only a single statement is allowed"
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        return False, "query must start with SELECT"
    if _WRITE_WORDS.search(s):
        return False, "write/DDL keywords are not allowed"
    # must mention the shipments table and no other table name after FROM/JOIN
    tables = re.findall(r"(?is)\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", s)
    for t in tables:
        if t.lower() != TABLE:
            return False, f"unknown table '{t}'"
    return True, "ok"


def run_select(db_path: str, sql: str, params: tuple = (), limit: int = 500):
    """Run a validated SELECT and return list[dict]."""
    ok, why = is_safe_select(sql)
    if not ok:
        raise ValueError(f"Unsafe query rejected: {why}")
    conn = connect_readonly(db_path)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchmany(limit)
        return [dict(r) for r in rows]
    finally:
        conn.close()
