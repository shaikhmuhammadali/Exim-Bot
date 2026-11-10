"""
EXIM Insight & Q&A Bot  --  Insight Engine (Part A)
H.G. Hawa & Co.

Architecture Step 4a -- INSIGHT ENGINE.  Deterministic, repeatable SQL
aggregations that pre-compute the auto-dashboard the moment a file is loaded.

Every tile carries:
  * the headline figure(s),
  * an `evidence` SQL (+ params) so the UI can expand it to the real rows,
nothing is a black box.

Blueprint tile catalogue (Section 04):
  Headline numbers, Top 5 buyers, Top 5 suppliers, Rate band, Most-traded grade,
  Buyer frequency, Top trade lanes, Country concentration.
"""

from __future__ import annotations

import sqlite3
from .store import TABLE


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# A reusable "real rows behind this" projection.
EVIDENCE_COLS = (
    'date, shipment_id, hs_code, product_desc, seller, seller_country, '
    'origin_port, buyer, buyer_country, destination_port, unit, quantity, '
    'value_usd, unit_price, grade_derived, gsm_derived'
)


def build_dashboard(db_path: str, top_n: int = 5) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tiles = []

        # ---------------- Headline numbers ----------------
        head = _q(conn, f"""
            SELECT
              COUNT(*)                                   AS shipments,
              COALESCE(SUM(quantity),0)                  AS total_qty,
              COALESCE(SUM(value_usd),0)                 AS total_value,
              AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
              MIN(date)                                  AS date_from,
              MAX(date)                                  AS date_to,
              COUNT(DISTINCT buyer_key)                  AS uniq_buyers,
              COUNT(DISTINCT seller_key)                 AS uniq_sellers,
              COUNT(DISTINCT buyer_country)              AS buyer_countries,
              COUNT(DISTINCT seller_country)             AS seller_countries
            FROM {TABLE}
        """)[0]
        tiles.append({
            "id": "headline", "title": "Headline numbers",
            "subtitle": "Totals across the whole file",
            "type": "stats",
            "stats": [
                {"label": "Shipments", "value": head["shipments"], "fmt": "int"},
                {"label": "Total quantity", "value": head["total_qty"], "fmt": "qty"},
                {"label": "Total value", "value": head["total_value"], "fmt": "usd"},
                {"label": "Avg unit price", "value": head["avg_price"], "fmt": "usd4"},
                {"label": "Unique buyers", "value": head["uniq_buyers"], "fmt": "int"},
                {"label": "Unique suppliers", "value": head["uniq_sellers"], "fmt": "int"},
            ],
            "meta": {
                "date_from": head["date_from"], "date_to": head["date_to"],
                "buyer_countries": head["buyer_countries"],
                "seller_countries": head["seller_countries"],
            },
            "evidence": {"sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} ORDER BY value_usd DESC", "params": []},
        })

        # ---------------- Top buyers (volume + value) ----------------
        top_buyers = _q(conn, f"""
            SELECT buyer AS name, buyer_country AS country,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                   ROUND(100.0*COALESCE(SUM(quantity),0)/NULLIF(SUM(COALESCE(SUM(quantity),0)) OVER(),0),1) AS share
            FROM {TABLE}
            WHERE buyer_key IS NOT NULL
            GROUP BY buyer_key
            ORDER BY qty DESC
            LIMIT ?""", (top_n,))
        tiles.append({
            "id": "top_buyers", "title": f"Top {top_n} buyers",
            "subtitle": "Ranked by quantity — share = % of all quantity",
            "type": "ranktable",
            "columns": [("name", "Buyer"), ("country", "Country"),
                        ("shipments", "Shipments"), ("qty", "Quantity"),
                        ("value", "Value (USD)"), ("avg_price", "Avg rate"),
                        ("share", "Share %")],
            "rows": top_buyers,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE buyer=? ORDER BY quantity DESC",
                "params_from": "name"},
        })

        # ---------------- Top suppliers ----------------
        top_sellers = _q(conn, f"""
            SELECT seller AS name, seller_country AS country,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                   ROUND(100.0*COALESCE(SUM(quantity),0)/NULLIF(SUM(COALESCE(SUM(quantity),0)) OVER(),0),1) AS share
            FROM {TABLE}
            WHERE seller_key IS NOT NULL
            GROUP BY seller_key
            ORDER BY qty DESC
            LIMIT ?""", (top_n,))
        tiles.append({
            "id": "top_suppliers", "title": f"Top {top_n} suppliers",
            "subtitle": "Biggest sellers — share = % of all quantity",
            "type": "ranktable",
            "columns": [("name", "Supplier"), ("country", "Country"),
                        ("shipments", "Shipments"), ("qty", "Quantity"),
                        ("value", "Value (USD)"), ("avg_price", "Avg rate"),
                        ("share", "Share %")],
            "rows": top_sellers,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE seller=? ORDER BY quantity DESC",
                "params_from": "name"},
        })

        # ---------------- Rate band (low / avg / high + who) ----------------
        band = _q(conn, f"""
            SELECT MIN(unit_price) AS lo, AVG(unit_price) AS avg, MAX(unit_price) AS hi
            FROM {TABLE} WHERE unit_price>0""")[0]
        lo_row = _q(conn, f"""SELECT buyer, seller, product_desc, unit_price, unit, grade_derived, gsm_derived
            FROM {TABLE} WHERE unit_price>0 ORDER BY unit_price ASC LIMIT 1""")
        hi_row = _q(conn, f"""SELECT buyer, seller, product_desc, unit_price, unit, grade_derived, gsm_derived
            FROM {TABLE} WHERE unit_price>0 ORDER BY unit_price DESC LIMIT 1""")
        tiles.append({
            "id": "rate_band", "title": "Rate band",
            "subtitle": "Lowest / average / highest unit price -- and who is at each end",
            "type": "rateband",
            "band": band,
            "low": lo_row[0] if lo_row else None,
            "high": hi_row[0] if hi_row else None,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE unit_price>0 ORDER BY unit_price ASC",
                "params": []},
        })

        # ---------------- Most-traded grade (derived) ----------------
        grades = _q(conn, f"""
            SELECT COALESCE(grade_derived,'(not derived)') AS grade,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN gsm_derived>0 THEN gsm_derived END) AS avg_gsm
            FROM {TABLE}
            GROUP BY grade
            ORDER BY qty DESC
            LIMIT 8""")
        tiles.append({
            "id": "grades", "title": "Most-traded grade",
            "subtitle": "Grade & GSM breakdown  —  derived from product text (indicative)",
            "derived": True,
            "type": "ranktable",
            "columns": [("grade", "Grade (derived)"), ("shipments", "Shipments"),
                        ("qty", "Quantity"), ("value", "Value (USD)"), ("avg_gsm", "Avg GSM")],
            "rows": grades,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE COALESCE(grade_derived,'(not derived)')=? ORDER BY quantity DESC",
                "params_from": "grade"},
        })

        # ---------------- Buyer frequency ----------------
        freq = _q(conn, f"""
            SELECT buyer AS name, buyer_country AS country,
                   COUNT(*) AS purchases,
                   COUNT(DISTINCT date) AS distinct_days,
                   MIN(date) AS first_seen, MAX(date) AS last_seen
            FROM {TABLE}
            WHERE buyer_key IS NOT NULL
            GROUP BY buyer_key
            ORDER BY purchases DESC
            LIMIT ?""", (top_n * 2,))
        tiles.append({
            "id": "buyer_frequency", "title": "Buyer frequency",
            "subtitle": "How often each top buyer purchased — frequent vs one-off",
            "type": "ranktable",
            "columns": [("name", "Buyer"), ("country", "Country"),
                        ("purchases", "Shipments"), ("distinct_days", "Active days"),
                        ("first_seen", "First"), ("last_seen", "Last")],
            "rows": freq,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE buyer=? ORDER BY date",
                "params_from": "name"},
        })

        # ---------------- Top trade lanes ----------------
        lanes = _q(conn, f"""
            SELECT trade_lane AS lane,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value
            FROM {TABLE}
            WHERE trade_lane IS NOT NULL
            GROUP BY trade_lane
            ORDER BY qty DESC
            LIMIT ?""", (top_n * 2,))
        tiles.append({
            "id": "trade_lanes", "title": "Top trade lanes",
            "subtitle": "Origin country → destination country, by quantity",
            "type": "ranktable",
            "columns": [("lane", "Lane"), ("shipments", "Shipments"),
                        ("qty", "Quantity"), ("value", "Value (USD)")],
            "rows": lanes,
            "evidence": {
                "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE trade_lane=? ORDER BY quantity DESC",
                "params_from": "lane"},
        })

        # ---------------- Country concentration ----------------
        buyer_countries = _q(conn, f"""
            SELECT COALESCE(buyer_country,'(blank)') AS country,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(value_usd),0) AS value,
                   COALESCE(SUM(quantity),0) AS qty
            FROM {TABLE}
            GROUP BY country ORDER BY value DESC LIMIT ?""", (top_n * 2,))
        seller_countries = _q(conn, f"""
            SELECT COALESCE(seller_country,'(blank)') AS country,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(value_usd),0) AS value,
                   COALESCE(SUM(quantity),0) AS qty
            FROM {TABLE}
            GROUP BY country ORDER BY value DESC LIMIT ?""", (top_n * 2,))
        tiles.append({
            "id": "country_concentration", "title": "Country concentration",
            "subtitle": "Which buyer / seller countries dominate the value",
            "type": "twolist",
            "left": {"heading": "Buyer countries", "rows": buyer_countries,
                     "evidence": {"sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE COALESCE(buyer_country,'(blank)')=? ORDER BY value_usd DESC",
                                  "params_from": "country"}},
            "right": {"heading": "Seller countries", "rows": seller_countries,
                      "evidence": {"sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE COALESCE(seller_country,'(blank)')=? ORDER BY value_usd DESC",
                                   "params_from": "country"}},
        })

        # ---------------- Monthly breakdown (period / trend) ----------------
        months = _q(conn, f"""
            SELECT year_month AS month,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                   COUNT(DISTINCT buyer_key) AS buyers,
                   COUNT(DISTINCT seller_key) AS sellers
            FROM {TABLE}
            WHERE year_month IS NOT NULL
            GROUP BY year_month
            ORDER BY year_month ASC""")
        if len(months) >= 1:
            tiles.append({
                "id": "monthly", "title": "Monthly breakdown",
                "subtitle": "Each month in the file(s) — volume, value & activity over time",
                "type": "ranktable",
                "columns": [("month", "Month"), ("shipments", "Shipments"),
                            ("qty", "Quantity"), ("value", "Value (USD)"),
                            ("avg_price", "Avg rate"), ("buyers", "Buyers"),
                            ("sellers", "Suppliers")],
                "rows": months,
                "evidence": {
                    "sql": f"SELECT {EVIDENCE_COLS} FROM {TABLE} WHERE year_month=? ORDER BY value_usd DESC",
                    "params_from": "month"},
            })

        return {"tiles": tiles, "headline": head}
    finally:
        conn.close()
