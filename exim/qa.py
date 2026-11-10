"""
EXIM Insight & Q&A Bot  --  Ask-Anything Engine (Part B)
H.G. Hawa & Co.

Architecture Step 4b -- Q&A ENGINE.

The promise (blueprint Section 05): the bot does NOT stuff 16,000+ rows into an
LLM's memory and hope.  It keeps the data in a small database, converts the
question into a PRECISE SQL query, runs it on the real data, and returns the
exact result -- verifiable, hallucination-free, always able to show the rows.

Two layers, in order:

  1. DETERMINISTIC NL->SQL  (default, no API key, instant, exact)
     A real intent+entity parser that recognises the documented question shapes
     (English and Hinglish) and compiles each to a parameterised SELECT.

  2. OPTIONAL LLM->SQL      (only if ANTHROPIC_API_KEY is set)
     For free-form questions the parser can't map, Claude is asked to write ONE
     read-only SELECT against the known schema.  The SQL is then run on the real
     DB through the same SELECT-only guard -- so the answer still comes from the
     data, never from the model's imagination.

Either way, the answer object carries the SQL used + the evidence rows, so every
number is traceable.
"""

from __future__ import annotations

import os
import re
import json
import difflib
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .store import TABLE, run_select, is_safe_select, SCHEMA


# --------------------------------------------------------------------------- #
#  Hinglish / synonym normalisation
# --------------------------------------------------------------------------- #

_SYNONYMS = {
    # hindi / hinglish -> canonical english cue words
    "khareed": "buy", "khariid": "buy", "kharid": "buy", "liya": "bought",
    "becha": "sold", "bika": "sold", "bechaa": "sold", "vikri": "sold",
    "sasta": "cheapest", "sastaa": "cheapest", "sabse sasta": "cheapest",
    "mehnga": "highest", "mehengaa": "highest", "mahanga": "highest",
    "kitna": "how much", "kitni": "how much", "kaun": "who", "kaun-kaun": "who",
    "konsa": "which", "kaunsa": "which", "kis": "which", "kis rate": "what rate",
    "rate pe": "at rate", "rate pr": "at rate", "frequency se": "frequency",
    "kitni baar": "how many times", "kitne baar": "how many times",
    "sabse zyada": "top", "sabse jyada": "top", "zyada": "most",
    "average": "average", "aausat": "average", "ausat": "average",
    "total": "total", "kul": "total",
    "supplier ne": "supplier", "ne becha": "sold", "se": "from",
}

_UNIT_WORDS = {"mt", "ton", "tons", "tonne", "tonnes", "kg", "kgs", "kilogram",
               "carton", "cartons", "package", "packages", "roll", "rolls",
               "pcs", "piece", "pieces", "unit", "units"}


def _normalise_question(q: str) -> str:
    t = " " + q.lower().strip() + " "
    # multi-word synonyms first
    for k in sorted(_SYNONYMS, key=len, reverse=True):
        if " " in k:
            t = t.replace(" " + k + " ", " " + _SYNONYMS[k] + " ")
    # single-word
    words = t.split()
    words = [_SYNONYMS.get(w, w) for w in words]
    t = " ".join(words)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --------------------------------------------------------------------------- #
#  Answer container
# --------------------------------------------------------------------------- #

@dataclass
class Answer:
    ok: bool
    kind: str                      # "table" | "scalar" | "list" | "error" | "clarify"
    title: str = ""
    columns: list[tuple] = field(default_factory=list)   # [(key,label),...]
    rows: list[dict] = field(default_factory=list)
    scalar: object = None
    note: str = ""
    sql: str = ""
    params: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)   # {sql, params} to show raw rows
    engine: str = "deterministic"
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "ok": self.ok, "kind": self.kind, "title": self.title,
            "columns": [list(c) for c in self.columns], "rows": self.rows,
            "scalar": self.scalar, "note": self.note, "sql": self.sql,
            "params": self.params, "evidence": self.evidence,
            "engine": self.engine, "suggestions": self.suggestions,
        }


# --------------------------------------------------------------------------- #
#  Entity resolver -- maps "Nine Dragons" -> the stored canonical display name
# --------------------------------------------------------------------------- #

class EntityIndex:
    """Loads distinct buyer / seller / country / port / grade values once, so a
    free-text mention in a question can be resolved to the exact stored value."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._loaded = False
        self.buyers: dict[str, str] = {}
        self.sellers: dict[str, str] = {}
        self.countries: set[str] = set()
        self.ports: set[str] = set()
        self.grades: set[str] = set()
        self.units: set[str] = set()

    def load(self):
        if self._loaded:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            for name, key in conn.execute(
                    f"SELECT DISTINCT buyer, buyer_key FROM {TABLE} WHERE buyer IS NOT NULL"):
                if name:
                    self.buyers[name.lower()] = name
            for name, key in conn.execute(
                    f"SELECT DISTINCT seller, seller_key FROM {TABLE} WHERE seller IS NOT NULL"):
                if name:
                    self.sellers[name.lower()] = name
            for (c,) in conn.execute(
                    f"SELECT DISTINCT buyer_country FROM {TABLE} WHERE buyer_country IS NOT NULL"):
                self.countries.add(c)
            for (c,) in conn.execute(
                    f"SELECT DISTINCT seller_country FROM {TABLE} WHERE seller_country IS NOT NULL"):
                self.countries.add(c)
            for col in ("origin_port", "destination_port"):
                for (p,) in conn.execute(f"SELECT DISTINCT {col} FROM {TABLE} WHERE {col} IS NOT NULL"):
                    self.ports.add(p)
            for (g,) in conn.execute(
                    f"SELECT DISTINCT grade_derived FROM {TABLE} WHERE grade_derived IS NOT NULL"):
                self.grades.add(g)
            for (u,) in conn.execute(f"SELECT DISTINCT unit FROM {TABLE} WHERE unit IS NOT NULL"):
                self.units.add(u)
        finally:
            conn.close()
        self._loaded = True

    # ---- fuzzy lookups ----
    def _match(self, text: str, pool: dict[str, str], cutoff=0.6, strict=False) -> Optional[str]:
        if not text:
            return None
        t = text.lower().strip()
        if t in pool:
            return pool[t]
        if strict:
            # Token-overlap score: the candidate must share >=2 words with a
            # company name, OR 1 distinctive (>=5-char) word. This matches real
            # names even with noise verbs ('buying Pt Fajar Surya Wisesa') while
            # rejecting stray words ('most', 'everything').
            toks = set(w for w in t.split() if len(w) > 2)
            if not toks:
                return None
            best, best_key = None, (0, 0.0)
            for low, orig in pool.items():
                ctoks = set(low.split())
                ov = toks & ctoks
                score = len(ov)
                if score == 0:
                    continue
                strong = (score >= 2) or (score == 1 and max(len(w) for w in ov) >= 5)
                if not strong:
                    continue
                key = (score, score / max(1, len(ctoks)))   # prefer tight matches
                if key > best_key:
                    best_key, best = key, orig
            if best:
                return best
            close = difflib.get_close_matches(t, list(pool.keys()), n=1, cutoff=0.9)
            return pool[close[0]] if close else None
        # lenient: substring / token containment
        hits = [orig for low, orig in pool.items() if t in low or low in t]
        if hits:
            return max(hits, key=lambda h: len(set(t.split()) & set(h.lower().split())))
        close = difflib.get_close_matches(t, list(pool.keys()), n=1, cutoff=cutoff)
        return pool[close[0]] if close else None

    def match_buyer(self, text, strict=False):   self.load(); return self._match(text, self.buyers, strict=strict)
    def match_seller(self, text, strict=False):  self.load(); return self._match(text, self.sellers, strict=strict)

    def match_country(self, text):
        self.load()
        if not text:
            return None
        t = text.strip().lower()
        for c in self.countries:
            if c and c.lower() == t:
                return c
        # word-boundary containment only (so 'many' does NOT match 'Germany')
        tset = set(t.split())
        for c in self.countries:
            if not c:
                continue
            cl = c.lower()
            if cl in t or t in cl.split() or set(cl.split()) <= tset:
                return c
        close = difflib.get_close_matches(t, [c.lower() for c in self.countries], n=1, cutoff=0.7)
        if close:
            for c in self.countries:
                if c.lower() == close[0]:
                    return c
        return None

    def match_grade(self, text):
        self.load()
        if not text:
            return None
        t = text.lower()
        for g in self.grades:
            if g and g.lower() in t:
                return g
        return None

    def find_any_company(self, text, strict=False):
        """Return ('buyer'|'seller', name) for whichever side matches best."""
        b = self.match_buyer(text, strict=strict)
        s = self.match_seller(text, strict=strict)
        if b and not s:
            return ("buyer", b)
        if s and not b:
            return ("seller", s)
        if b and s:
            # prefer the longer token overlap
            tb = len(set(text.lower().split()) & set(b.lower().split()))
            ts = len(set(text.lower().split()) & set(s.lower().split()))
            return ("seller", s) if ts >= tb else ("buyer", b)
        return (None, None)


EVID = ('date, shipment_id, hs_code, product_desc, seller, seller_country, '
        'origin_port, buyer, buyer_country, destination_port, unit, quantity, '
        'value_usd, unit_price, grade_derived, gsm_derived')


# --------------------------------------------------------------------------- #
#  Feature extraction from the (normalised) question
# --------------------------------------------------------------------------- #

def _extract_top_n(q: str, default=5) -> int:
    m = re.search(r"\btop\s+(\d{1,3})\b", q) or re.search(r"\b(\d{1,3})\s+(?:top|best|biggest|largest)\b", q)
    if m:
        return max(1, min(100, int(m.group(1))))
    return default

def _extract_gsm(q: str) -> Optional[int]:
    m = re.search(r"(\d{2,4})\s*gsm", q)
    if m:
        return int(m.group(1))
    return None

def _wants_value(q: str) -> bool:
    return bool(re.search(r"\b(value|usd|dollar|worth|amount|revenue)\b", q))

def _wants_count(q: str) -> bool:
    return bool(re.search(r"\b(how many times|frequency|frequent|number of shipments|count|baar|times)\b", q))

def _wants_rate(q: str) -> bool:
    return bool(re.search(r"\b(rate|unit price|price|per unit|rate-wise|price-wise)\b", q)
                and not re.search(r"\b(quantity|volume|qty|value|amount)\b", q))

def _metric_col(q: str) -> tuple[str, str]:
    """Return (sql_expr, label) for the metric the user is ranking by."""
    if _wants_value(q):
        return ("COALESCE(SUM(value_usd),0)", "Value (USD)")
    if _wants_count(q):
        return ("COUNT(*)", "Shipments")
    if _wants_rate(q):
        return ("AVG(CASE WHEN unit_price>0 THEN unit_price END)", "Avg rate")
    return ("COALESCE(SUM(quantity),0)", "Quantity")


# --------------------------------------------------------------------------- #
#  Deterministic intent handlers
#  Each returns an Answer or None (not my shape, try the next).
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.idx = EntityIndex(db_path)

    # ---- small run helper ----
    def _run(self, sql, params=(), limit=500):
        return run_select(self.db_path, sql, tuple(params), limit=limit)

    # ================= handlers =================

    def h_top_entities(self, q, raw) -> Optional[Answer]:
        """top N buyers / suppliers / countries / ports / grades / lanes by metric"""
        if not re.search(r"\btop\b|\bbiggest\b|\blargest\b|\bbest\b|\bmost\b", q):
            return None
        n = _extract_top_n(q)
        metric_sql, metric_label = _metric_col(q)

        dim = None
        if re.search(r"\bbuyer|importer|consignee\b", q):
            dim = ("buyer", "buyer_key", "Buyer", "buyer_country")
        elif re.search(r"\bsupplier|seller|exporter|shipper\b", q):
            dim = ("seller", "seller_key", "Supplier", "seller_country")
        elif re.search(r"\bdestination port|discharge port\b", q):
            dim = ("destination_port", "destination_port", "Destination port", None)
        elif re.search(r"\borigin port|loading port\b", q):
            dim = ("origin_port", "origin_port", "Origin port", None)
        elif re.search(r"\bport", q):
            dim = ("destination_port", "destination_port", "Destination port", None)
        elif re.search(r"\blane|route|corridor\b", q):
            dim = ("trade_lane", "trade_lane", "Trade lane", None)
        elif re.search(r"\bgrade", q):
            dim = ("grade_derived", "grade_derived", "Grade (derived)", None)
        elif re.search(r"\bbuyer countr|importing countr|destination countr\b", q):
            dim = ("buyer_country", "buyer_country", "Buyer country", None)
        elif re.search(r"\bseller countr|supplier countr|origin countr|exporting countr\b", q):
            dim = ("seller_country", "seller_country", "Seller country", None)
        elif re.search(r"\bcountr\b|\bcountries\b", q):
            dim = ("buyer_country", "buyer_country", "Buyer country", None)
        if not dim:
            return None

        disp, group, label, country_col = dim
        extra = f", {country_col} AS country" if country_col else ""
        country_grp = ""
        sql = f"""
            SELECT {disp} AS name{extra},
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price
            FROM {TABLE}
            WHERE {group} IS NOT NULL
            GROUP BY {group}
            ORDER BY {metric_sql} DESC
            LIMIT ?"""
        rows = self._run(sql, (n,), limit=n)
        cols = [("name", label)]
        if country_col:
            cols.append(("country", "Country"))
        cols += [("shipments", "Shipments"), ("qty", "Quantity"),
                 ("value", "Value (USD)"), ("avg_price", "Avg rate")]
        ev_col = disp
        return Answer(
            ok=True, kind="table",
            title=f"Top {n} {label.lower()}s by {metric_label.lower()}",
            columns=cols, rows=rows,
            sql=sql.strip(), params=[n],
            evidence={"sql": f"SELECT {EVID} FROM {TABLE} WHERE {ev_col}=? ORDER BY quantity DESC",
                      "params_from": "name"},
        )

    def h_company_summary(self, q, raw) -> Optional[Answer]:
        """'Buyer X ne total kitna liya, average rate?' / 'how much did X buy/sell'"""
        if not re.search(r"\btotal\b|\bhow much\b|\baverage\b|\bsummary\b|\boverview\b", q):
            return None
        # find a company mention: strip metric words, feed the rest to the resolver
        side, name = self._guess_company(q, raw)
        if not name:
            return None
        return self._company_summary(side, name)

    def _company_summary(self, side, name) -> Answer:
        """Full profile of one company -- used by the summary handler and the
        universal fallback (when a question is just a company name)."""
        col = "buyer" if side == "buyer" else "seller"
        sql = f"""
            SELECT COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS total_qty,
                   COALESCE(SUM(value_usd),0) AS total_value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                   MIN(unit_price) AS min_price, MAX(unit_price) AS max_price,
                   MIN(date) AS first_seen, MAX(date) AS last_seen,
                   COUNT(DISTINCT {'seller_key' if side=='buyer' else 'buyer_key'}) AS counterparties
            FROM {TABLE} WHERE {col}=?"""
        rows = self._run(sql, (name,))
        r = rows[0] if rows else {}
        label = "bought" if side == "buyer" else "sold"
        other = "suppliers" if side == "buyer" else "buyers"
        cols = [("metric", "Metric"), ("v", "Value")]
        # each row carries a 'fmt' hint so the UI formats it (and honours USD/INR)
        disp_rows = [
            {"metric": "Shipments", "v": r.get("shipments"), "fmt": "int"},
            {"metric": f"Total quantity {label}", "v": r.get("total_qty"), "fmt": "qty"},
            {"metric": "Total value (USD)", "v": r.get("total_value"), "fmt": "usd"},
            {"metric": "Average rate", "v": r.get("avg_price"), "fmt": "usd4"},
            {"metric": "Lowest rate", "v": r.get("min_price"), "fmt": "usd4"},
            {"metric": "Highest rate", "v": r.get("max_price"), "fmt": "usd4"},
            {"metric": "Active window",
             "v": f"{r.get('first_seen') or '?'} → {r.get('last_seen') or '?'}", "fmt": "text"},
            {"metric": f"Distinct {other}", "v": r.get("counterparties"), "fmt": "int"},
        ]
        return Answer(
            ok=True, kind="table",
            title=f"{name} — {'buyer' if side=='buyer' else 'supplier'} summary",
            columns=cols, rows=disp_rows,
            sql=sql.strip(), params=[name],
            evidence={"sql": f"SELECT {EVID} FROM {TABLE} WHERE {col}=? ORDER BY date",
                      "params": [name]},
        )

    def h_who_trades_with(self, q, raw) -> Optional[Answer]:
        """'X se kaun khareed raha hai' (buyers of seller X) /
           'who supplies X' (sellers to buyer X), with frequency."""
        if not re.search(r"\bwho\b|\bwhich (?:compan|importer|buyer|supplier|seller)", q):
            return None
        # If the 'from <X>' target is actually a COUNTRY, this is a trade-lane
        # question, not a company question -> let h_lane_filter handle it.
        m = re.search(r"from ([a-z .&-]{3,40})", q)
        if m and self.idx.match_country(m.group(1).strip()):
            return None
        # Needs a real company counterparty; strict match so superlatives like
        # "who bought the most" fall through to the ranking engine instead.
        side, name = self._guess_company(q, raw, strict=True)
        if not name:
            return None
        if side == "seller":
            # buyers purchasing from this seller
            sql = f"""
                SELECT buyer AS name, buyer_country AS country,
                       COUNT(*) AS shipments,
                       COALESCE(SUM(quantity),0) AS qty,
                       COALESCE(SUM(value_usd),0) AS value,
                       AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                       MIN(date) AS first_seen, MAX(date) AS last_seen
                FROM {TABLE} WHERE seller=? AND buyer_key IS NOT NULL
                GROUP BY buyer_key ORDER BY qty DESC LIMIT 50"""
            title = f"Who is buying from {name} (with frequency)"
            ev = {"sql": f"SELECT {EVID} FROM {TABLE} WHERE seller=? AND buyer=? ORDER BY date",
                  "params_fixed": [name], "params_from": "name"}
        else:
            sql = f"""
                SELECT seller AS name, seller_country AS country,
                       COUNT(*) AS shipments,
                       COALESCE(SUM(quantity),0) AS qty,
                       COALESCE(SUM(value_usd),0) AS value,
                       AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                       MIN(date) AS first_seen, MAX(date) AS last_seen
                FROM {TABLE} WHERE buyer=? AND seller_key IS NOT NULL
                GROUP BY seller_key ORDER BY qty DESC LIMIT 50"""
            title = f"Who is supplying {name} (with frequency)"
            ev = {"sql": f"SELECT {EVID} FROM {TABLE} WHERE buyer=? AND seller=? ORDER BY date",
                  "params_fixed": [name], "params_from": "name"}
        rows = self._run(sql, (name,))
        cols = [("name", "Counterparty"), ("country", "Country"),
                ("shipments", "Shipments"), ("qty", "Quantity"),
                ("value", "Value (USD)"), ("avg_price", "Avg rate"),
                ("first_seen", "First"), ("last_seen", "Last")]
        return Answer(ok=True, kind="table", title=title, columns=cols, rows=rows,
                      sql=sql.strip(), params=[name], evidence=ev)

    def h_cheapest_highest(self, q, raw) -> Optional[Answer]:
        """'cheapest testliner 140 gsm, which supplier, what rate' / highest rate."""
        if not re.search(r"\bcheapest|lowest|sasta|highest|most expensive|best rate\b", q):
            return None
        ascending = not bool(re.search(r"\bhighest|most expensive\b", q))
        where = ["unit_price>0"]
        params = []
        grade = self.idx.match_grade(raw)
        if grade:
            where.append("grade_derived=?"); params.append(grade)
        gsm = _extract_gsm(q)
        if gsm:
            where.append("gsm_derived=?"); params.append(float(gsm))
        # optional country filter ("from vietnam")
        m = re.search(r"\bfrom ([a-z ]{3,30})", q)
        if m:
            c = self.idx.match_country(m.group(1))
            if c:
                where.append("seller_country=?"); params.append(c)
        order = "ASC" if ascending else "DESC"
        sql = f"""
            SELECT seller, seller_country, buyer, buyer_country, product_desc,
                   grade_derived, gsm_derived, unit, unit_price, quantity, value_usd, date
            FROM {TABLE}
            WHERE {' AND '.join(where)}
            ORDER BY unit_price {order}
            LIMIT 15"""
        rows = self._run(sql, params)
        if not rows:
            return Answer(ok=True, kind="list", title="No matching priced rows",
                          note="No rows with a usable unit price matched those filters.",
                          sql=sql.strip(), params=params)
        what = (grade or "product")
        if gsm:
            what += f" {gsm} GSM"
        cols = [("seller", "Supplier"), ("seller_country", "Origin"),
                ("buyer", "Buyer"), ("unit_price", "Rate"), ("unit", "Unit"),
                ("quantity", "Qty"), ("grade_derived", "Grade*"),
                ("gsm_derived", "GSM*"), ("date", "Date")]
        return Answer(
            ok=True, kind="table",
            title=f"{'Lowest' if ascending else 'Highest'} rate for {what}",
            note="Grade/GSM are derived from product text (indicative).",
            columns=cols, rows=rows, sql=sql.strip(), params=params,
            evidence={"sql": sql.strip(), "params": params},
        )

    def h_lane_filter(self, q, raw) -> Optional[Answer]:
        """'which <country> importers bought from <country>, at what rates'"""
        # Price/rank questions are handled elsewhere; this is a who-on-a-lane ask.
        if re.search(r"\bcheapest|lowest|highest|sasta|most expensive\b", q):
            return None
        if not re.search(r"\bimporter|buyer|bought|buying|who|importing\b", q):
            return None
        m = re.search(r"from ([a-z ]{3,30})", q)
        dest = re.search(r"\b([a-z]+)\s+(?:importers|buyers)\b", q)
        c_from = self.idx.match_country(m.group(1)) if m else None
        c_to = self.idx.match_country(dest.group(1)) if dest else None
        if not (c_from or c_to):
            return None
        where, params = [], []
        if c_from:
            where.append("seller_country=?"); params.append(c_from)
        if c_to:
            where.append("buyer_country=?"); params.append(c_to)
        sql = f"""
            SELECT buyer AS name, buyer_country AS country,
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price
            FROM {TABLE}
            WHERE {' AND '.join(where)} AND buyer_key IS NOT NULL
            GROUP BY buyer_key ORDER BY qty DESC LIMIT 50"""
        rows = self._run(sql, params)
        lane = f"{c_from or 'any'} → {c_to or 'any'}"
        cols = [("name", "Buyer"), ("country", "Country"), ("shipments", "Shipments"),
                ("qty", "Quantity"), ("value", "Value (USD)"), ("avg_price", "Avg rate")]
        return Answer(ok=True, kind="table", title=f"Buyers on lane {lane}",
                      columns=cols, rows=rows, sql=sql.strip(), params=params,
                      evidence={"sql": f"SELECT {EVID} FROM {TABLE} WHERE {' AND '.join(where)} AND buyer=? ORDER BY date",
                                "params_fixed": params, "params_from": "name"})

    def h_frequency(self, q, raw) -> Optional[Answer]:
        """'how many times did X buy' """
        if not _wants_count(q):
            return None
        side, name = self._guess_company(q, raw)
        if not name:
            return None
        col = "buyer" if side == "buyer" else "seller"
        sql = f"""SELECT COUNT(*) AS shipments, COUNT(DISTINCT date) AS active_days,
                         MIN(date) AS first_seen, MAX(date) AS last_seen
                  FROM {TABLE} WHERE {col}=?"""
        r = self._run(sql, (name,))[0]
        return Answer(
            ok=True, kind="scalar",
            title=f"{name} — {'purchases' if side=='buyer' else 'sales'} frequency",
            scalar=r["shipments"],
            note=f"{r['shipments']} shipments across {r['active_days']} active day(s), "
                 f"{r['first_seen']} → {r['last_seen']}.",
            sql=sql.strip(), params=[name],
            evidence={"sql": f"SELECT {EVID} FROM {TABLE} WHERE {col}=? ORDER BY date",
                      "params": [name]},
        )

    def h_aggregate(self, q, raw) -> Optional[Answer]:
        """Overall aggregate / count / overview questions that want ONE figure
        or a small stat block -- e.g. 'total value?', 'how many buyers?',
        'average rate?', 'date range?', 'give me an overview'. Runs before the
        ranking engine so these don't get turned into a top-N list."""

        def stat(expr):
            return self._run(f"SELECT {expr} AS v FROM {TABLE}")[0]["v"]

        def one(metric, value, fmt, note=""):
            return Answer(ok=True, kind="table", title=metric,
                          columns=[("metric", "Metric"), ("v", "Value")],
                          rows=[{"metric": metric, "v": value, "fmt": fmt}],
                          note=note,
                          sql=f"SELECT ... FROM {TABLE}",
                          evidence={"sql": f"SELECT {EVID} FROM {TABLE} ORDER BY value_usd DESC",
                                    "params": []})

        # ---- distinct counts ----
        count_defs = [
            (r"\bhow many (?:unique |distinct )?(?:buyers|importers|customers|consignees)\b|number of (?:buyers|importers)|total (?:buyers|importers)|count of buyers",
             "COUNT(DISTINCT buyer_key)", "Unique buyers"),
            (r"\bhow many (?:unique |distinct )?(?:suppliers|sellers|exporters|vendors|shippers)\b|number of (?:suppliers|sellers)|total (?:suppliers|sellers)",
             "COUNT(DISTINCT seller_key)", "Unique suppliers"),
            (r"\bhow many (?:shipments|records|rows|transactions|deals|entries)\b|number of shipments|total shipments|total records",
             "COUNT(*)", "Shipments"),
            (r"\bhow many (?:grades)\b|number of grades",
             "COUNT(DISTINCT grade_derived)", "Grades (derived)"),
            (r"\bhow many (?:ports)\b|number of ports",
             "COUNT(DISTINCT destination_port)", "Destination ports"),
            (r"\bhow many (?:lanes|routes|corridors)\b|number of lanes",
             "COUNT(DISTINCT trade_lane)", "Trade lanes"),
            (r"\bhow many (?:months|periods)\b|number of months",
             "COUNT(DISTINCT year_month)", "Months covered"),
            (r"\bhow many (?:hs ?codes?|hsn)\b|number of hs ?codes?",
             "COUNT(DISTINCT hs4)", "HS codes (4-digit)"),
        ]
        for pat, expr, label in count_defs:
            if re.search(pat, q):
                return one(label, stat(expr), "int")

        # "how many countries"
        if re.search(r"\bhow many countr|number of countr", q):
            bc = stat("COUNT(DISTINCT buyer_country)")
            sc = stat("COUNT(DISTINCT seller_country)")
            return Answer(ok=True, kind="table", title="Countries covered",
                          columns=[("metric", "Metric"), ("v", "Value")],
                          rows=[{"metric": "Buyer (destination) countries", "v": bc, "fmt": "int"},
                                {"metric": "Seller (origin) countries", "v": sc, "fmt": "int"}],
                          evidence={"sql": f"SELECT {EVID} FROM {TABLE} ORDER BY value_usd DESC", "params": []})

        # ---- grand totals ----
        if re.search(r"\b(total|overall|grand total|sum of|aggregate) (?:value|worth|usd|amount)\b|total value|overall value|grand total", q):
            return one("Total value (USD)", stat("COALESCE(SUM(value_usd),0)"), "usd")
        if re.search(r"\b(total|overall) (?:quantity|volume|qty)\b|total quantity|total volume", q):
            return one("Total quantity", stat("COALESCE(SUM(quantity),0)"), "qty")

        # ---- overall average rate (no entity in the question) ----
        if re.search(r"\b(average|avg|mean|overall average) (?:unit )?(?:rate|price)\b", q) \
                and not self._guess_company(q, raw, strict=True)[1]:
            return one("Average unit price", stat("AVG(CASE WHEN unit_price>0 THEN unit_price END)"),
                       "usd4", note="Average across all priced shipments.")

        # ---- date range / period ----
        if re.search(r"\b(date range|time ?frame|what period|period cover|coverage|date span|how many days|from when|span of)\b", q):
            lo = stat("MIN(date)"); hi = stat("MAX(date)"); m = stat("COUNT(DISTINCT year_month)")
            return Answer(ok=True, kind="scalar", title="Date range covered",
                          scalar=f"{lo} → {hi}",
                          note=f"{lo} to {hi} · {m} month(s) in the file.",
                          evidence={"sql": f"SELECT {EVID} FROM {TABLE} ORDER BY date", "params": []})

        # ---- overview / summary / key numbers ----
        if re.search(r"\b(overview|summary|headline|key numbers|key figures|snapshot|highlights|at a glance|give me insights?|the numbers)\b", q) \
                and not self._guess_company(q, raw, strict=True)[1]:
            r = self._run(f"""SELECT COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty, COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price,
                   COUNT(DISTINCT buyer_key) AS buyers, COUNT(DISTINCT seller_key) AS sellers,
                   MIN(date) AS lo, MAX(date) AS hi FROM {TABLE}""")[0]
            rows = [
                {"metric": "Shipments", "v": r["shipments"], "fmt": "int"},
                {"metric": "Total quantity", "v": r["qty"], "fmt": "qty"},
                {"metric": "Total value (USD)", "v": r["value"], "fmt": "usd"},
                {"metric": "Average unit price", "v": r["avg_price"], "fmt": "usd4"},
                {"metric": "Unique buyers", "v": r["buyers"], "fmt": "int"},
                {"metric": "Unique suppliers", "v": r["sellers"], "fmt": "int"},
                {"metric": "Date range", "v": f"{r['lo']} → {r['hi']}", "fmt": "text"},
            ]
            return Answer(ok=True, kind="table", title="Overview — key numbers",
                          columns=[("metric", "Metric"), ("v", "Value")], rows=rows,
                          evidence={"sql": f"SELECT {EVID} FROM {TABLE} ORDER BY value_usd DESC", "params": []})

        return None

    # ---- dimension vocabulary for the universal builder ----
    # (regex, (display_col, group_col, label, country_col_or_None))
    DIMENSIONS = [
        (r"\bbuyer|importer|consignee|purchaser|customer\b",
         ("buyer", "buyer_key", "Buyer", "buyer_country")),
        (r"\bsupplier|seller|exporter|shipper|vendor|manufacturer\b",
         ("seller", "seller_key", "Supplier", "seller_country")),
        (r"\bdestination port|discharge port|unloading port|port of discharge\b",
         ("destination_port", "destination_port", "Destination port", None)),
        (r"\borigin port|loading port|port of loading\b",
         ("origin_port", "origin_port", "Origin port", None)),
        (r"\bport",
         ("destination_port", "destination_port", "Destination port", None)),
        (r"\blane|route|corridor\b",
         ("trade_lane", "trade_lane", "Trade lane", None)),
        (r"\bgrade",
         ("grade_derived", "grade_derived", "Grade (derived)", None)),
        (r"\bgsm",
         ("gsm_derived", "gsm_derived", "GSM (derived)", None)),
        (r"\bhs ?code|hsn|tariff|hs\b",
         ("hs4", "hs4", "HS code (4-digit)", None)),
        (r"\bbuyer countr|importing countr|destination countr|importer countr\b",
         ("buyer_country", "buyer_country", "Buyer country", None)),
        (r"\bseller countr|supplier countr|origin countr|exporting countr|exporter countr\b",
         ("seller_country", "seller_country", "Seller country", None)),
        (r"\bcountr\b|\bcountries\b|\bnation\b",
         ("buyer_country", "buyer_country", "Buyer country", None)),
        (r"\bbuyer city|importer city|destination city\b",
         ("buyer_city", "buyer_city", "Buyer city", None)),
        (r"\bseller city|supplier city|origin city\b",
         ("seller_city", "seller_city", "Seller city", None)),
        (r"\bcity|cities\b",
         ("buyer_city", "buyer_city", "Buyer city", None)),
        (r"\bmonth|monthly|trend|over time|timeline|per month\b",
         ("year_month", "year_month", "Month", None)),
        (r"\bproduct|description|item|goods|commodity|material\b",
         ("grade_derived", "grade_derived", "Grade (derived)", None)),
        (r"\bunit\b",
         ("unit", "unit", "Unit", None)),
    ]

    def _detect_filters(self, q, raw):
        """Pull WHERE filters (grade, GSM, country, unit) out of a free question.
        Returns (where_list, params, human_desc)."""
        where, params, desc = [], [], []
        grade = self.idx.match_grade(raw)
        if grade:
            where.append("grade_derived=?"); params.append(grade); desc.append(grade)
        gsm = _extract_gsm(q)
        if gsm:
            where.append("gsm_derived=?"); params.append(float(gsm)); desc.append(f"{gsm} GSM")
        # "from <country>" -> seller origin
        m = re.search(r"from ([a-z .&-]{3,30})", q)
        cfrom = self.idx.match_country(m.group(1).strip()) if m else None
        if cfrom:
            where.append("seller_country=?"); params.append(cfrom); desc.append(f"from {cfrom}")
        # "to/into <country>" -> buyer destination
        m2 = re.search(r"\b(?:to|into|in) ([a-z .&-]{3,30})", q)
        cto = self.idx.match_country(m2.group(1).strip()) if m2 else None
        if cto and cto != cfrom:
            where.append("buyer_country=?"); params.append(cto); desc.append(f"to {cto}")
        # month-name filter (e.g. 'August buyers') -> matching year_month(s)
        ym = self._detect_month(q)
        if ym:
            placeholders = ",".join("?" for _ in ym)
            where.append(f"year_month IN ({placeholders})"); params += ym
            desc.append(", ".join(ym))
        return where, params, desc

    _MONTHS = {"january": "01", "jan": "01", "february": "02", "feb": "02",
               "march": "03", "mar": "03", "april": "04", "apr": "04",
               "may": "05", "june": "06", "jun": "06", "july": "07", "jul": "07",
               "august": "08", "aug": "08", "september": "09", "sep": "09", "sept": "09",
               "october": "10", "oct": "10", "november": "11", "nov": "11",
               "december": "12", "dec": "12"}

    def _detect_month(self, q):
        """Return the list of year_month values in the data that match a month
        name mentioned in the question (e.g. 'august' -> ['2026-08'])."""
        self.idx.load()
        avail = getattr(self, "_avail_months", None)
        if avail is None:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            try:
                avail = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT year_month FROM {TABLE} WHERE year_month IS NOT NULL")]
            finally:
                conn.close()
            self._avail_months = avail
        hits = []
        for word, mm in self._MONTHS.items():
            if re.search(r"\b" + word + r"\b", q):
                for ym in avail:
                    if ym.endswith("-" + mm) and ym not in hits:
                        hits.append(ym)
        return hits

    def h_universal(self, q, raw) -> Optional[Answer]:
        """The catch-all. Detects whatever dimension + metric + filter the
        question contains (any phrasing, incl. 'purchase-wise / quantity-wise /
        rate-wise / description-wise / frequency') and returns a relevant table,
        so the bot almost never dead-ends. Runs after the specific handlers."""
        # 1) which dimension to group by?
        dim = None
        for pat, d in self.DIMENSIONS:
            if re.search(pat, q):
                dim = d
                break
        # 2) parameter-word inference when no explicit dimension named
        if not dim:
            if re.search(r"purchase|buying|bought|khareed|liya|demand", q):
                dim = ("buyer", "buyer_key", "Buyer", "buyer_country")
            elif re.search(r"supply|selling|sold|becha|export|produce", q):
                dim = ("seller", "seller_key", "Supplier", "seller_country")

        # detect filters (grade / gsm / country) up front
        where, params, fdesc = self._detect_filters(q, raw)

        # any signal that the user wants a metric/breakdown rather than a company?
        has_signal = bool(re.search(
            r"quantity|volume|value|amount|rate|price|frequency|most|highest|lowest|"
            r"top|wise|breakdown|ranking|how many|count|everything|overview|summary|"
            r"insight|all|dashboard|trend|average|total", q))

        # 3) a BARE company name (no dimension, no filter, no metric signal)
        #    -> full company profile. Guarded so noise words don't match a company.
        if not dim and not where and not has_signal:
            side, name = self._guess_company(q, raw, strict=True)
            if name:
                return self._company_summary(side, name)
            return None   # truly unmapped -> clarify step

        # 4) choose a sensible default dimension when none was named but there is
        #    a filter (e.g. bare 'testliner') or a metric signal.
        if not dim:
            if where or has_signal:
                dim = ("buyer", "buyer_key", "Buyer", "buyer_country")
            else:
                return None

        disp, group, label, country_col = dim
        metric_sql, metric_label = _metric_col(q)
        n = _extract_top_n(q, default=10)
        where = [f"{group} IS NOT NULL"] + where
        extra = f", {country_col} AS country" if country_col else ""
        sql = f"""
            SELECT {disp} AS name{extra},
                   COUNT(*) AS shipments,
                   COALESCE(SUM(quantity),0) AS qty,
                   COALESCE(SUM(value_usd),0) AS value,
                   AVG(CASE WHEN unit_price>0 THEN unit_price END) AS avg_price
            FROM {TABLE}
            WHERE {' AND '.join(where)}
            GROUP BY {group}
            ORDER BY {metric_sql} DESC
            LIMIT ?"""
        rows = self._run(sql, params + [n], limit=n)
        if not rows:
            return None
        cols = [("name", label)]
        if country_col:
            cols.append(("country", "Country"))
        cols += [("shipments", "Shipments"), ("qty", "Quantity"),
                 ("value", "Value (USD)"), ("avg_price", "Avg rate")]
        suffix = (" — " + ", ".join(fdesc)) if fdesc else ""
        # evidence: the rows behind the whole filtered set
        ev_where = " AND ".join(where[1:]) if len(where) > 1 else ""
        ev_sql = f"SELECT {EVID} FROM {TABLE}" + (f" WHERE {ev_where}" if ev_where else "") + " ORDER BY quantity DESC"
        return Answer(
            ok=True, kind="table",
            title=f"Top {n} {label.lower()}s by {metric_label.lower()}{suffix}",
            note=("Grade/GSM are derived from product text (indicative)."
                  if (self.idx.match_grade(raw) or _extract_gsm(q) or "grade" in label.lower()
                      or "gsm" in label.lower()) else ""),
            columns=cols, rows=rows, sql=sql.strip(), params=params + [n],
            evidence={"sql": ev_sql, "params": params},
        )

    # ---- utility: pull a company name out of a question ----
    def _guess_company(self, q, raw, strict=False):
        # remove obvious metric/question words, keep the candidate span
        cand = raw
        cand = re.sub(r"(?i)\b(buyer|seller|supplier|importer|exporter|how much|total|"
                      r"average|rate|avg|value|usd|did|does|ne|se|from|the|what|who|"
                      r"is|are|buy|bought|sold|sell|purchase|frequency|times|how many|"
                      r"show|me|list|top|everything|overview|summary|all|wise|\d+)\b", " ", cand)
        cand = re.sub(r"[^\w &.-]", " ", cand)
        cand = re.sub(r"\s+", " ", cand).strip()
        # In strict mode, demand a stronger match so stray words don't resolve to
        # a company that merely shares a fragment.
        if strict and len(cand) < 4:
            return (None, None)
        side, name = self.idx.find_any_company(cand, strict=strict)
        if name:
            return side, name
        if not strict:
            side, name = self.idx.find_any_company(raw)
        return side, name

    # ================= dispatch =================
    # Order matters: country-lane questions ("importers from <country>") must be
    # tried before the company resolver, so a country name is never mistaken for
    # a company that merely contains that word.
    HANDLERS = ["h_aggregate", "h_top_entities", "h_lane_filter",
                "h_who_trades_with", "h_cheapest_highest", "h_frequency",
                "h_company_summary", "h_universal"]

    def answer(self, question: str) -> Answer:
        raw = question.strip()
        if not raw:
            return Answer(ok=False, kind="error", title="Empty question",
                          note="Type a question about the data.")
        q = _normalise_question(raw)

        for hname in self.HANDLERS:
            try:
                res = getattr(self, hname)(q, raw)
            except Exception as e:
                res = None
            if res is not None:
                return res

        # Nothing matched deterministically -> optional LLM, else graceful clarify.
        llm = try_llm_sql(self.db_path, raw)
        if llm is not None:
            return llm

        return Answer(
            ok=False, kind="clarify",
            title="I couldn't map that to the data yet",
            note="I answer strictly from the columns in your file, so I need the "
                 "question in one of these shapes. Try, for example:",
            suggestions=[
                "Top 5 buyers by value",
                "Top 10 suppliers by quantity",
                "Who is buying from <supplier name>?",
                "How much did <buyer name> buy, and at what average rate?",
                "Cheapest testliner 140 GSM — which supplier and rate?",
                "Top destination ports by volume",
                "Which importers bought from Vietnam?",
                "How many times did <buyer name> buy?",
            ],
        )


def _fmt(x):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):,.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)


# --------------------------------------------------------------------------- #
#  Optional LLM -> SQL (only if ANTHROPIC_API_KEY is set)
# --------------------------------------------------------------------------- #

_SCHEMA_DOC = """Table `shipments`, one row per shipment. Columns:
  date TEXT (yyyy-mm-dd), year_month TEXT, shipment_id, hs_code, hs4, industry,
  product_desc,
  seller TEXT (normalised supplier display name), seller_country, seller_state,
  seller_city, origin_port,
  buyer TEXT (normalised importer display name), buyer_country, buyer_state,
  buyer_city, destination_port,
  unit, quantity REAL, value_usd REAL, unit_price REAL,
  grade_derived TEXT (indicative, from product text), gsm_derived REAL,
  bf_derived REAL, trade_lane TEXT ('Origin -> Destination').
Notes: use normalised `seller`/`buyer` for grouping. value_usd / unit_price may
be 0 or NULL in some rows (filter unit_price>0 for rate questions)."""


def try_llm_sql(db_path: str, question: str) -> Optional[Answer]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get("EXIM_LLM_MODEL", "claude-sonnet-4-5")
        prompt = (
            "You convert a user's question into ONE read-only SQLite SELECT over "
            "this schema. Return ONLY JSON: {\"sql\": \"...\", \"title\": \"...\"}. "
            "No writes, one statement, table name exactly `shipments`, add a LIMIT "
            "of at most 200 unless the question is a single aggregate.\n\n"
            + _SCHEMA_DOC + f"\n\nQuestion: {question}"
        )
        msg = client.messages.create(
            model=model, max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        spec = json.loads(m.group(0))
        sql = spec.get("sql", "").strip()
        ok, why = is_safe_select(sql)
        if not ok:
            return None
        rows = run_select(db_path, sql, (), limit=200)
        cols = [(k, k.replace("_", " ").title()) for k in (rows[0].keys() if rows else [])]
        return Answer(
            ok=True, kind="table" if rows else "list",
            title=spec.get("title") or "Answer",
            columns=list(cols), rows=rows, sql=sql, params=[],
            evidence={"sql": sql, "params": []},
            engine="llm",
            note="Generated SQL ran on your real data (read-only).",
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Public convenience
# --------------------------------------------------------------------------- #

def ask(db_path: str, question: str) -> dict:
    return Engine(db_path).answer(question).to_dict()
