"""
EXIM Insight & Q&A Bot  --  Web application (Flask)
H.G. Hawa & Co.

The interface from the blueprint (Section 07): a web app -- upload a file, get the
auto-dashboard (Part A), and chat questions (Part B).  Open in a browser or phone,
drop a file, ask.  No install for the end user beyond running this server.

Routes
  GET  /                 the single-page app
  POST /api/upload       ingest -> clean -> store -> dashboard; returns summary
  GET  /api/state        current dataset summary + dashboard (for reloads)
  POST /api/ask          natural-language question -> exact answer
  POST /api/rows         run a server-issued evidence SELECT -> raw rows (read-only)
  GET  /api/export       download the current answer/tile rows as CSV
"""

from __future__ import annotations

import os
import io
import csv
import json
import time
import uuid
import traceback

from flask import (Flask, request, jsonify, render_template,
                   send_file, Response)

import pandas as pd

from exim import (ingest_file, clean_and_derive, quality_report, build_database,
                  build_dashboard, Engine, IngestError)
from exim.store import run_select

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {".xlsx", ".xls", ".csv"}
MAX_MB = 200

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


# --------------------------------------------------------------------------- #
#  In-process dataset registry (one active dataset; keeps a small history so
#  Phase-2 multi-period work can grow here without re-architecting).
# --------------------------------------------------------------------------- #

class Dataset:
    def __init__(self, ds_id, source_name, db_path):
        self.id = ds_id
        self.source_name = source_name
        self.db_path = db_path
        self.summary = {}
        self.quality = {}
        self.dashboard = {}
        self.engine = Engine(db_path)
        self.created = time.time()


REGISTRY: dict[str, Dataset] = {}
ACTIVE = {"id": None}


def _active() -> Dataset | None:
    return REGISTRY.get(ACTIVE["id"]) if ACTIVE["id"] else None


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    # Accept one OR several files (e.g. the last 3 months), merged into one
    # combined dataset -- blueprint Phase 2, multi-period tracking.
    files = request.files.getlist("file") or request.files.getlist("files")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"ok": False, "error": "No file in the request."}), 400

    ds_id = uuid.uuid4().hex[:12]
    frames, per_file, raw_paths = [], [], []
    has_rodtep = has_drawback = False
    recognised, unknown = set(), []

    try:
        t0 = time.time()
        for f in files:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_EXT:
                return jsonify({"ok": False,
                                "error": f"Unsupported file type '{ext}' in '{f.filename}'. "
                                         "Use .xlsx, .xls or .csv."}), 400
            safe_name = os.path.basename(f.filename)
            raw_path = os.path.join(UPLOAD_DIR, f"{ds_id}__{safe_name}")
            f.save(raw_path)
            raw_paths.append(raw_path)

            ing = ingest_file(raw_path, source_name=safe_name)
            clean, q = clean_and_derive(ing.frame)
            clean = clean.copy()
            clean["source_file"] = safe_name           # provenance per row
            frames.append(clean)
            recognised |= set(ing.recognised_fields)
            unknown += [u for u in ing.unknown_headers if u not in unknown]
            has_rodtep = has_rodtep or q.get("has_rodtep")
            has_drawback = has_drawback or q.get("has_drawback")
            per_file.append({"name": safe_name, "rows": ing.total_rows,
                             "periods": q.get("periods", [])})

        combined = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        quality = quality_report(combined, has_rodtep=has_rodtep, has_drawback=has_drawback)

        db_path = os.path.join(UPLOAD_DIR, f"{ds_id}.db")
        build_database(combined, db_path)
        dash = build_dashboard(db_path)
        elapsed = round(time.time() - t0, 1)

        label = (per_file[0]["name"] if len(per_file) == 1
                 else f"{len(per_file)} files ({quality['period_count']} months)")
        ds = Dataset(ds_id, label, db_path)
        ds.summary = {
            "id": ds_id,
            "source_name": label,
            "files": per_file,
            "file_count": len(per_file),
            "total_rows": len(combined),
            "periods": quality.get("periods", []),
            "period_count": quality.get("period_count", 0),
            "recognised_fields": sorted(recognised),
            "unknown_headers": unknown,
            "has_rodtep": has_rodtep,
            "has_drawback": has_drawback,
            "elapsed_sec": elapsed,
        }
        ds.quality = quality
        ds.dashboard = dash
        REGISTRY[ds_id] = ds
        ACTIVE["id"] = ds_id

        return jsonify({"ok": True, "summary": ds.summary,
                        "quality": quality, "dashboard": dash})
    except IngestError as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Could not process the file(s): {e}"}), 500


@app.route("/api/state")
def api_state():
    ds = _active()
    if not ds:
        return jsonify({"ok": True, "loaded": False})
    return jsonify({"ok": True, "loaded": True,
                    "summary": ds.summary, "quality": ds.quality,
                    "dashboard": ds.dashboard})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    ds = _active()
    if not ds:
        return jsonify({"ok": False, "error": "Load an EXIM file first."}), 400
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Empty question."}), 400
    try:
        ans = ds.engine.answer(question).to_dict()
        return jsonify({"ok": True, "answer": ans})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Query failed: {e}"}), 500


@app.route("/api/rows", methods=["POST"])
def api_rows():
    """Run a server-issued evidence SELECT to show the real rows behind a number.
    All SQL passes the SELECT-only guard; the DB connection is read-only."""
    ds = _active()
    if not ds:
        return jsonify({"ok": False, "error": "No dataset loaded."}), 400
    data = request.get_json(force=True, silent=True) or {}
    sql = (data.get("sql") or "").strip()
    params = data.get("params") or []
    limit = int(data.get("limit") or 500)
    if not sql:
        return jsonify({"ok": False, "error": "No query provided."}), 400
    try:
        rows = run_select(ds.db_path, sql, tuple(params), limit=max(1, min(limit, 2000)))
        return jsonify({"ok": True, "rows": rows, "count": len(rows)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Query failed: {e}"}), 500


@app.route("/api/export", methods=["POST"])
def api_export():
    """Download a result set (rows) as CSV."""
    data = request.get_json(force=True, silent=True) or {}
    rows = data.get("rows") or []
    name = data.get("name") or "exim_export"
    if not rows:
        return jsonify({"ok": False, "error": "Nothing to export."}), 400
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True,
                     download_name=f"{name}.csv")


@app.route("/api/health")
def health():
    ds = _active()
    return jsonify({"ok": True, "loaded": bool(ds),
                    "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("\n" + "=" * 64)
    print("  H.G. HAWA & CO.  --  EXIM Insight & Q&A Bot")
    print("  Open:  http://127.0.0.1:%d" % port)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  Free-form Q&A (Claude-backed SQL): ENABLED")
    else:
        print("  Free-form Q&A (Claude-backed SQL): off  (set ANTHROPIC_API_KEY to enable)")
    print("=" * 64 + "\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
