# EXIM Insight & Q&A Bot
### H.G. Hawa & Co. — Paper & Packaging trade intelligence · v1.0

A tool that ingests **any EXIM trade report**, auto-extracts the insights that
matter, and then answers **any question** about that data on demand — like a
private Google for your import/export intelligence.

Built exactly to the project blueprint: **Part A — Auto-Insight** (the dashboard
appears the moment a file is loaded) and **Part B — Ask-Anything** (plain-English
or Hinglish questions, answered from the real data, never guessed).

---

## Why it is accurate (and not a guessing LLM)
The bot does **not** stuff thousands of rows into a model's memory and hope. It:

1. **Ingests** the Excel/CSV, locks the exact headers, validates.
2. **Cleans & derives** — normalises company names, parses dates, derives
   grade/GSM/BF from the product text, sanity-checks unit price, fixes country
   spellings, and keeps blanks blank (never fabricated).
3. **Stores** the data in a small local **SQLite** database.
4. **Answers** every question by converting it into a precise SQL query, running
   that query on the real data, and returning the exact result — with the rows
   behind any number one click away.

So even with lakhs of rows, every answer is **verifiable and hallucination-free.**

---

## How to run it

### Option 1 — one click (Windows)
Double-click **`run.bat`**. On first run it installs the required Python
packages, then starts the bot. When it prints an address, open it in your
browser:

> http://127.0.0.1:5000

### Option 2 — command line
```bash
pip install -r requirements.txt
python app.py
```
Then open **http://127.0.0.1:5000**.

> Requires **Python 3.10+**. Everything runs **locally** — no data leaves your machine.

---

## Using the bot
1. **Drop a file** — an EXIM shipment ledger (`.xlsx`, `.xls`, or `.csv`).
2. **Read the dashboard (Part A)** — headline numbers, top buyers, top
   suppliers, rate band, most-traded grade, buyer frequency, top trade lanes,
   country concentration. Click any tile → **see the exact rows** behind it.
3. **Ask anything (Part B)** — type a question and get a number / short list /
   small table. Examples:
   - `Top 5 buyers by value`
   - `Top 10 suppliers by quantity`
   - `Who is buying from <supplier>?`  (with frequency)
   - `How much did <buyer> buy, and at what average rate?`
   - `Cheapest testliner 140 GSM — which supplier and rate?`
   - `Top destination ports by volume`
   - `Which importers bought from Vietnam?`
   - Hinglish works too: `Nine Dragons se kaun khareed raha hai?`

Every answer shows the **SQL used**, lets you **Show rows**, and **Export CSV**.

---

## What it can and cannot answer (honest by design)
**Answerable** (present in the data): buyer/supplier names, countries, cities,
ports; quantity, value (USD), unit price; best/lowest/highest/average rate;
buying frequency & recency; derived grade/GSM/BF; trade lanes; trends over the
file's window; RoDTEP/Drawback **if** those columns exist.

**Not in the data** (reported as "not in this data", never invented): emails &
phone numbers, contact persons, production capacity, contract terms, GST /
registration / website. Those are a separate enrichment project.

> Grade, GSM and BF are **derived** from the free-text product description and
> are labelled *(derived)* everywhere — indicative, verify before commercial use.

---

## Free-form questions (optional)
The deterministic engine already covers the documented question shapes with zero
setup. To also answer completely free-form questions, set an API key before
starting — the bot will have Claude write a **read-only** SQL query, which is
still validated and run on your real data:

```bash
set ANTHROPIC_API_KEY=sk-ant-...      &  python app.py      (Windows cmd)
$env:ANTHROPIC_API_KEY="sk-ant-..."   ;  python app.py      (PowerShell)
```
Without a key, the bot stays fully functional and simply asks you to rephrase
anything it can't map.

---

## Project layout
```
EXIM_Insight_Bot/
  app.py               Flask web app (upload · dashboard · chat · rows · export)
  run.bat              one-click launcher (Windows)
  requirements.txt
  exim/
    ingest.py          tolerant loader + header detection + schema lock
    clean.py           name/date/country normalise, grade/GSM/BF derive, price sanity
    store.py           SQLite load + read-only SELECT-guard
    insights.py        Part A — the auto-dashboard (deterministic aggregations)
    qa.py              Part B — NL→SQL engine (+ optional Claude fallback)
  templates/index.html UI
  static/              styles.css · app.js
  uploads/             your files + the generated .db (stays local)
```

---
*Confidential — H.G. Hawa & Co. The data lives in the bot's head; you simply ask.*
