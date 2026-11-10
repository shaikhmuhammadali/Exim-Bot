/* EXIM Insight & Q&A Bot  --  H.G. Hawa & Co.  --  front-end logic */
(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  const state = { unit: "USD", fx: 88.5, dashboard: null, lastRows: null };

  // -------------------- formatting --------------------
  function num(x) { if (x == null || x === "") return "—"; const n = Number(x); if (!isFinite(n)) return String(x); return n.toLocaleString("en-IN", { maximumFractionDigits: 0 }); }
  function num2(x) { if (x == null || x === "") return "—"; const n = Number(x); if (!isFinite(n)) return String(x); return n.toLocaleString("en-IN", { maximumFractionDigits: 2 }); }
  function money(x) {
    if (x == null || x === "") return "—";
    let n = Number(x); if (!isFinite(n)) return String(x);
    const sym = state.unit === "INR" ? "₹" : "$";
    if (state.unit === "INR") n = n * state.fx;
    const abs = Math.abs(n);
    if (abs >= 1e9) return sym + (n / 1e9).toFixed(2) + "B";
    if (abs >= 1e7 && state.unit === "INR") return sym + (n / 1e7).toFixed(2) + " Cr";
    if (abs >= 1e6) return sym + (n / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return sym + (n / 1e3).toFixed(1) + "K";
    return sym + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
  function money4(x) {
    if (x == null || x === "") return "—";
    let n = Number(x); if (!isFinite(n)) return String(x);
    const sym = state.unit === "INR" ? "₹" : "$";
    if (state.unit === "INR") n = n * state.fx;
    const abs = Math.abs(n);
    // keep small unit prices legible instead of rounding to 0
    let dp = 3;
    if (abs > 0 && abs < 1) dp = 6;
    else if (abs < 10) dp = 4;
    return sym + n.toLocaleString("en-IN", { maximumFractionDigits: dp });
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  const FMT = { int: num, qty: num2, usd: money, usd4: money4, num: num2 };

  // -------------------- views --------------------
  const uploadView = $("#uploadView"), loadingView = $("#loadingView"),
        workspace = $("#workspace");
  function show(view) {
    uploadView.hidden = view !== "upload";
    loadingView.hidden = view !== "loading";
    workspace.hidden = view !== "work";
    $("#newFileBtn").hidden = view !== "work";
  }

  // -------------------- upload --------------------
  const fileInput = $("#fileInput"), dropzone = $("#dropzone");
  fileInput.addEventListener("change", () => { if (fileInput.files.length) upload(fileInput.files); });
  ["dragenter", "dragover"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", e => { if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });
  $("#newFileBtn").addEventListener("click", () => { show("upload"); });

  async function upload(fileList) {
    const files = Array.from(fileList);
    show("loading");
    $("#loadingTitle").textContent = files.length === 1
      ? "Reading “" + files[0].name + "”…"
      : "Reading " + files.length + " files (merging months)…";
    const fd = new FormData(); files.forEach(f => fd.append("file", f));
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      let j;
      try { j = await r.json(); }
      catch (_) {
        // server returned HTML (e.g. a 502/413 from the host), not JSON
        if (r.status === 413) alert("That file is too large for the server.");
        else alert("The server couldn't process that file.\n\n" +
          "On the free cloud plan this usually means it ran out of memory on a very large file. " +
          "Try a smaller file (e.g. one month), or use the local app which has no size limit.");
        show("upload"); return;
      }
      if (!j.ok) { alert("Could not load the file:\n\n" + (j.error || "Unknown error")); show("upload"); return; }
      state.dashboard = j.dashboard;
      renderWorkspace(j.summary, j.quality, j.dashboard);
      show("work");
    } catch (e) { alert("Upload failed: " + e); show("upload"); }
  }

  // -------------------- units --------------------
  $("#unitSelect").addEventListener("change", e => { state.unit = e.target.value; rerender(); });
  $("#fxRate").addEventListener("change", e => { state.fx = Number(e.target.value) || 88.5; rerender(); });
  function rerender() { if (state.dashboard && !workspace.hidden) renderTiles(state.dashboard); if (lastSummary) renderRibbon(lastSummary, lastDash && lastDash.headline); }

  // -------------------- workspace --------------------
  let lastSummary = null, lastDash = null;
  function renderWorkspace(summary, quality, dash) {
    lastSummary = summary; lastDash = dash;
    renderRibbon(summary, dash.headline);
    renderFlags(summary, quality);
    renderTiles(dash);
    initChat(summary);
  }

  function renderRibbon(summary, head) {
    const rib = $("#ribbon"); rib.innerHTML = "";
    const pcount = summary.period_count || 0;
    const periodSub = pcount ? pcount + (pcount === 1 ? " month" : " months") : "";
    const items = [
      ["Rows", num(summary.total_rows), summary.source_name],
      ["Shipments", num(head.shipments), ""],
      ["Total value", money(head.total_value), "declared"],
      ["Total quantity", num(head.total_qty), "native units"],
      ["Avg unit price", money4(head.avg_price), ""],
      ["Unique buyers", num(head.uniq_buyers), (head.buyer_countries || 0) + " countries"],
      ["Unique suppliers", num(head.uniq_sellers), (head.seller_countries || 0) + " countries"],
      ["Date range", (head.date_from || "?"), "to " + (head.date_to || "?") + (periodSub ? " · " + periodSub : "")],
    ];
    if (summary.file_count > 1) items.unshift(["Files loaded", num(summary.file_count), periodSub + " merged"]);
    items.forEach(([k, v, s]) => {
      const c = el("div", "rib");
      c.innerHTML = `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>` + (s ? `<div class="s">${esc(s)}</div>` : "");
      rib.appendChild(c);
    });
  }

  function qbar(label, pct) {
    return `<div class="qbar"><div class="ql"><span>${esc(label)}</span><span>${pct}%</span></div>
      <div class="track"><div class="fill" style="width:${Math.max(2, Math.min(100, pct))}%"></div></div></div>`;
  }
  function renderFlags(summary, q) {
    const b = $("#flagsBody"); b.innerHTML = "";
    b.innerHTML += qbar("Dates parsed", q.date_parsed_pct);
    b.innerHTML += qbar("Buyer name present", q.buyer_present_pct);
    b.innerHTML += qbar("Seller name present", q.seller_present_pct);
    b.innerHTML += qbar("Value (USD) present", q.value_present_pct);
    b.innerHTML += qbar("Unit price present", q.price_present_pct);
    b.innerHTML += qbar("Grade derived", q.grade_derived_pct);
    b.innerHTML += qbar("GSM derived", q.gsm_derived_pct);
    const notes = [];
    notes.push("Grade / GSM / BF are <b>derived</b> by keyword-reading the Product Description — indicative, not a source column.");
    if (!summary.has_rodtep) notes.push("No <b>RoDTEP</b> column in this file — reported as “not in this data”.");
    if (!summary.has_drawback) notes.push("No <b>Drawback</b> column in this file — reported as “not in this data”.");
    if (summary.unknown_headers && summary.unknown_headers.length)
      notes.push("Unrecognised columns (kept aside, never guessed): " + summary.unknown_headers.map(esc).join(", "));
    if (q.price_outliers) notes.push(q.price_outliers + " row(s) flagged as <b>price outliers</b> (stated price vs value÷qty mismatch).");
    notes.push("Blank fields stay blank — the bot reports “not available”, never fabricates.");
    b.innerHTML += `<div class="note-card">${notes.map(n => "• " + n).join("<br>")}</div>`;
  }

  // -------------------- tiles --------------------
  function renderTiles(dash) {
    const wrap = $("#tiles"); wrap.innerHTML = "";
    dash.tiles.forEach(t => wrap.appendChild(buildTile(t)));
  }

  function tileHead(t) {
    const h = el("div", "tile-head");
    h.innerHTML = `<div class="tile-title">${esc(t.title)}${t.derived ? ' <span class="tag-derived">derived</span>' : ""}</div>
      <div class="tile-sub">${esc(t.subtitle || "")}</div>`;
    return h;
  }

  function buildTile(t) {
    const tile = el("div", "tile");
    tile.appendChild(tileHead(t));
    const body = el("div", "tile-body");

    if (t.type === "stats") {
      const g = el("div", "stats");
      t.stats.forEach(s => {
        const f = FMT[s.fmt] || (x => x);
        g.appendChild(el("div", "stat", `<div class="l">${esc(s.label)}</div><div class="v">${f(s.value)}</div>`));
      });
      body.appendChild(g);
      const meta = t.meta || {};
      body.appendChild(el("div", "bandwho", `Period <b>${esc(meta.date_from || "?")}</b> to <b>${esc(meta.date_to || "?")}</b> &middot; ${esc(meta.buyer_countries)} buyer / ${esc(meta.seller_countries)} seller countries`));
    }
    else if (t.type === "ranktable") {
      body.appendChild(rankTable(t));
    }
    else if (t.type === "rateband") {
      body.appendChild(rateBand(t));
    }
    else if (t.type === "twolist") {
      const tl = el("div", "twolist");
      tl.appendChild(miniList(t.left, "left"));
      tl.appendChild(miniList(t.right, "right"));
      body.appendChild(tl);
    }
    tile.appendChild(body);

    // footer: expand to rows for the whole tile (where evidence is static)
    if (t.evidence && (t.evidence.sql && (t.evidence.params || t.evidence.params === undefined) && !t.evidence.params_from)) {
      const foot = el("div", "tile-foot");
      const btn = el("button", "link", "See all rows &rarr;");
      btn.addEventListener("click", () => openRows(t.title, t.evidence.sql, t.evidence.params || []));
      foot.appendChild(el("span", null, "Verify any figure against the source"));
      foot.appendChild(btn);
      tile.appendChild(foot);
    }
    return tile;
  }

  const CURR_KEYS = new Set(["value", "value_usd", "avg_price", "unit_price", "min_price", "max_price"]);
  const QTY_KEYS = new Set(["qty", "quantity", "total_qty"]);
  function cellFmt(key, val) {
    if (CURR_KEYS.has(key)) return key.indexOf("price") >= 0 ? money4(val) : money(val);
    if (QTY_KEYS.has(key)) return num2(val);
    if (key === "shipments" || key === "purchases" || key === "distinct_days" || key === "active_days" || key === "counterparties" || key === "buyers" || key === "sellers") return num(val);
    if (key === "avg_gsm" || key === "gsm_derived") return val == null ? "—" : num(val);
    if (key === "share") return val == null ? "—" : num2(val) + "%";
    return esc(val == null ? "—" : val);
  }
  const NUMERIC = new Set([...CURR_KEYS, ...QTY_KEYS, "shipments", "purchases", "distinct_days", "active_days", "avg_gsm", "gsm_derived", "counterparties", "buyers", "sellers", "share"]);

  function rankTable(t) {
    const tbl = el("table", "mini");
    const cols = t.columns;
    tbl.innerHTML = "<thead><tr>" + cols.map(([k, l]) => `<th class="${NUMERIC.has(k) ? "num" : ""}">${esc(l)}</th>`).join("") + "</tr></thead>";
    const tb = el("tbody");
    (t.rows || []).forEach(row => {
      const tr = el("tr");
      if (t.evidence && t.evidence.params_from) {
        tr.className = "rowclick";
        tr.addEventListener("click", () => openRows(cols[0][1] + ": " + (row[t.evidence.params_from]), t.evidence.sql, [row[t.evidence.params_from]]));
      }
      tr.innerHTML = cols.map(([k], i) => `<td class="${NUMERIC.has(k) ? "num" : ""} ${i === 0 ? "name" : ""}">${cellFmt(k, row[k])}</td>`).join("");
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    return tbl;
  }

  function rateBand(t) {
    const w = el("div");
    const band = el("div", "band");
    const b = t.band || {};
    band.innerHTML =
      `<div class="b lo"><div class="l">Lowest</div><div class="v">${money4(b.lo)}</div></div>
       <div class="b"><div class="l">Average</div><div class="v">${money4(b.avg)}</div></div>
       <div class="b hi"><div class="l">Highest</div><div class="v">${money4(b.hi)}</div></div>`;
    w.appendChild(band);
    if (t.low) w.appendChild(el("div", "bandwho", `Lowest: <b>${esc(t.low.seller || "?")}</b> → <b>${esc(t.low.buyer || "?")}</b> &middot; ${esc(t.low.product_desc || "").slice(0, 60)}`));
    if (t.high) w.appendChild(el("div", "bandwho", `Highest: <b>${esc(t.high.seller || "?")}</b> → <b>${esc(t.high.buyer || "?")}</b> &middot; ${esc(t.high.product_desc || "").slice(0, 60)}`));
    return w;
  }

  function miniList(block, side) {
    const w = el("div");
    w.appendChild(el("h4", null, esc(block.heading)));
    const tbl = el("table", "mini");
    tbl.innerHTML = "<thead><tr><th>Country</th><th class='num'>Value</th></tr></thead>";
    const tb = el("tbody");
    (block.rows || []).forEach(r => {
      const tr = el("tr", "rowclick");
      tr.addEventListener("click", () => openRows(block.heading + ": " + r.country, block.evidence.sql, [r.country]));
      tr.innerHTML = `<td class="name">${esc(r.country)}</td><td class="num">${money(r.value)}</td>`;
      tb.appendChild(tr);
    });
    tbl.appendChild(tb); w.appendChild(tbl);
    return w;
  }

  // -------------------- rows modal --------------------
  const modal = $("#modal");
  $("#modalClose").addEventListener("click", () => modal.hidden = true);
  modal.addEventListener("click", e => { if (e.target === modal) modal.hidden = true; });
  $("#modalExport").addEventListener("click", () => exportRows(state.lastRows, "exim_rows"));

  async function openRows(title, sql, params) {
    $("#modalTitle").textContent = title;
    $("#modalSql").textContent = sql + (params && params.length ? "   -- params: " + JSON.stringify(params) : "");
    $("#modalBody").innerHTML = '<div class="typing" style="padding:20px"><i></i><i></i><i></i> fetching rows…</div>';
    modal.hidden = false;
    try {
      const r = await fetch("/api/rows", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql, params, limit: 1000 }) });
      const j = await r.json();
      if (!j.ok) { $("#modalBody").innerHTML = `<div class="note-card" style="margin:14px">${esc(j.error)}</div>`; return; }
      state.lastRows = j.rows;
      $("#modalBody").innerHTML = "";
      $("#modalBody").appendChild(fullTable(j.rows));
      $("#modalTitle").textContent = title + "  (" + j.count + " row" + (j.count === 1 ? "" : "s") + (j.count >= 1000 ? ", capped" : "") + ")";
    } catch (e) { $("#modalBody").innerHTML = `<div class="note-card" style="margin:14px">Failed: ${esc(e)}</div>`; }
  }

  function fullTable(rows) {
    if (!rows || !rows.length) return el("div", "note-card", "No rows.");
    const cols = Object.keys(rows[0]);
    const tbl = el("table", "mini");
    tbl.innerHTML = "<thead><tr>" + cols.map(c => `<th class="${NUMERIC.has(c) ? "num" : ""}">${esc(c)}</th>`).join("") + "</tr></thead>";
    const tb = el("tbody");
    rows.forEach(row => {
      const tr = el("tr");
      tr.innerHTML = cols.map(c => `<td class="${NUMERIC.has(c) ? "num" : ""}">${cellFmt(c, row[c])}</td>`).join("");
      tb.appendChild(tr);
    });
    tbl.appendChild(tb); return tbl;
  }

  async function exportRows(rows, name) {
    if (!rows || !rows.length) { alert("Nothing to export."); return; }
    const r = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows, name }) });
    const blob = await r.blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name + ".csv"; a.click();
  }

  // -------------------- chat --------------------
  const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];
  function monthLabel(ym) { const m = /^\d{4}-(\d{2})$/.exec(ym || ""); return m ? MONTH_NAMES[+m[1] - 1] : ym; }

  function initChat(summary) {
    const log = $("#chatlog");
    log.innerHTML = `<div class="empty-chat">Ask in plain English or Hinglish.<br>
      Every answer is run as a real query on your ${num(summary.total_rows)} rows — with the rows one click away.</div>`;
    // base + advanced suggestions, period-aware
    const sugg = ["Overview", "Top 5 buyers by value", "Top trade lanes",
      "Cheapest testliner 140 GSM", "How many suppliers?"];
    const p = (summary.periods || []);
    if (p.length >= 2) {
      sugg.splice(1, 0, `Compare ${monthLabel(p[p.length - 2])} and ${monthLabel(p[p.length - 1])}`);
      sugg.splice(2, 0, `New buyers in ${monthLabel(p[p.length - 1])}`);
    }
    const chips = $("#suggestChips"); chips.innerHTML = "";
    sugg.forEach(s => { const c = el("div", "chip", esc(s)); c.addEventListener("click", () => { $("#askInput").value = s; doAsk(); }); chips.appendChild(c); });
  }

  $("#askForm").addEventListener("submit", e => { e.preventDefault(); doAsk(); });

  async function doAsk() {
    const input = $("#askInput"); const q = input.value.trim(); if (!q) return;
    const log = $("#chatlog");
    const empty = log.querySelector(".empty-chat"); if (empty) empty.remove();
    log.appendChild(el("div", "msg user", esc(q)));
    const typing = el("div", "msg bot"); typing.innerHTML = `<div class="typing"><i></i><i></i><i></i> querying the data…</div>`;
    log.appendChild(typing); log.scrollTop = log.scrollHeight;
    input.value = "";
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
      const j = await r.json();
      typing.remove();
      if (!j.ok) { log.appendChild(botError(j.error || "Query failed.")); }
      else { log.appendChild(renderAnswer(j.answer)); }
      log.scrollTop = log.scrollHeight;
    } catch (e) { typing.remove(); log.appendChild(botError(String(e))); }
  }

  function botError(msg) { const m = el("div", "msg bot"); m.innerHTML = `<div class="clarify"><div class="c-title">Hmm</div>${esc(msg)}</div>`; return m; }

  function renderAnswer(a) {
    const m = el("div", "msg bot");
    if (a.kind === "clarify" || (!a.ok && a.kind !== "error")) {
      const c = el("div", "clarify");
      c.innerHTML = `<div class="c-title">${esc(a.title)}</div><div>${esc(a.note || "")}</div>`;
      if (a.suggestions && a.suggestions.length) {
        const ul = el("ul");
        a.suggestions.forEach(s => { const li = el("li", null, esc(s)); li.addEventListener("click", () => { $("#askInput").value = s; doAsk(); }); ul.appendChild(li); });
        c.appendChild(ul);
      }
      m.appendChild(c); return m;
    }
    const box = el("div", "answer");
    const title = el("div", "a-title");
    title.innerHTML = `<span>${esc(a.title)}</span><span class="eng ${a.engine}">${a.engine === "llm" ? "AI SQL" : "exact"}</span>`;
    box.appendChild(title);
    if (a.note) box.appendChild(el("div", "a-note", esc(a.note)));

    if (a.kind === "scalar") {
      box.appendChild(el("div", "a-scalar", num(a.scalar)));
    } else if (a.rows && a.rows.length) {
      const body = el("div", "a-body");
      const tbl = answerTable(a);
      body.appendChild(tbl);
      box.appendChild(body);
      const hint = el("div", "scroll-hint", "↔ scroll the table sideways for all columns, or open “Show rows”");
      hint.style.display = "none";
      box.appendChild(hint);
      requestAnimationFrame(() => { if (body.scrollWidth > body.clientWidth + 2) hint.style.display = "block"; });
    } else {
      box.appendChild(el("div", "a-note", "No matching rows."));
    }

    // footer: SQL toggle + show rows + export
    const foot = el("div", "a-foot");
    const left = el("div");
    const sqlBtn = el("button", "link", "SQL");
    const sqlBox = el("div", "sql-mono", esc(a.sql || "") + (a.params && a.params.length ? "\n-- params: " + esc(JSON.stringify(a.params)) : ""));
    sqlBtn.addEventListener("click", () => sqlBox.classList.toggle("show"));
    left.appendChild(sqlBtn);
    const right = el("div");
    if (a.evidence && a.evidence.sql) {
      const rowsBtn = el("button", "link", "Show rows");
      rowsBtn.style.marginRight = "12px";
      rowsBtn.addEventListener("click", () => {
        let params = a.evidence.params || a.params || [];
        openRows(a.title, a.evidence.sql, params);
      });
      right.appendChild(rowsBtn);
    }
    if (a.rows && a.rows.length) {
      const exp = el("button", "link", "Export");
      exp.addEventListener("click", () => exportRows(a.rows, "exim_answer"));
      right.appendChild(exp);
    }
    foot.appendChild(left); foot.appendChild(right);
    box.appendChild(foot); box.appendChild(sqlBox);
    m.appendChild(box);
    return m;
  }

  function answerTable(a) {
    const cols = a.columns && a.columns.length ? a.columns : Object.keys(a.rows[0]).map(k => [k, k]);
    const tbl = el("table", "mini");
    tbl.innerHTML = "<thead><tr>" + cols.map(([k, l]) => `<th class="${NUMERIC.has(k) ? "num" : ""}">${esc(l)}</th>`).join("") + "</tr></thead>";
    const tb = el("tbody");
    a.rows.forEach(row => {
      const tr = el("tr");
      tr.innerHTML = cols.map(([k], i) => {
        let cell;
        if ((k === "v" || k === "a" || k === "b") && row.fmt) { const f = FMT[row.fmt]; cell = row.fmt === "text" ? esc(row[k]) : (f ? f(row[k]) : esc(row[k])); }
        else cell = cellFmt(k, row[k]);
        const rightAlign = NUMERIC.has(k) || ((k === "v" || k === "a" || k === "b") && row.fmt && row.fmt !== "text") || k === "chg";
        return `<td class="${rightAlign ? "num" : ""} ${i === 0 ? "name" : ""}">${cell}</td>`;
      }).join("");
      tb.appendChild(tr);
    });
    tbl.appendChild(tb); return tbl;
  }

  // -------------------- auto-load UI --------------------
  function setupAuto(auto) {
    if (!auto) return;
    const badge = $("#autoBadge"), rbtn = $("#refreshBtn");
    if (auto.enabled) {
      badge.hidden = false;
      badge.textContent = "Auto · watching folder";
      badge.className = "badge badge-on";
      badge.title = "Auto-loading the latest EXIM report from: " + (auto.folder || "");
      rbtn.hidden = false;
    } else { badge.hidden = true; rbtn.hidden = true; }
  }
  $("#refreshBtn").addEventListener("click", refreshData);
  async function refreshData() {
    show("loading");
    $("#loadingTitle").textContent = "Re-checking for the latest EXIM report…";
    $("#loadingAuto").textContent = "";
    try {
      const j = await (await fetch("/api/refresh", { method: "POST" })).json();
      if (j.ok) { state.dashboard = j.dashboard; renderWorkspace(j.summary, j.quality, j.dashboard); setupAuto(j.auto); show("work"); }
      else { alert(j.error || "Refresh failed."); show(lastSummary ? "work" : "upload"); }
    } catch (e) { alert("Refresh failed: " + e); show(lastSummary ? "work" : "upload"); }
  }

  // -------------------- boot --------------------
  async function boot() {
    try {
      const h = await (await fetch("/api/health")).json();
      if (h.llm) { const b = $("#llmBadge"); b.textContent = "AI SQL: on"; b.className = "badge badge-on"; }
      setupAuto(h.auto);
    } catch (e) {}

    const s = await (await fetch("/api/state")).json().catch(() => ({}));
    if (s.loaded) { state.dashboard = s.dashboard; renderWorkspace(s.summary, s.quality, s.dashboard); setupAuto(s.auto); show("work"); return; }

    // Not loaded yet. If auto-load is enabled and working, wait for it.
    const auto = s.auto || {};
    if (auto.enabled && auto.status !== "no_files" && auto.status !== "error") {
      show("loading");
      $("#loadingTitle").textContent = "Auto-loading your latest EXIM report…";
      $("#loadingAuto").textContent = "Watching " + (auto.folder || "your Downloads folder");
      const deadline = Date.now() + 120000;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 2500));
        const st = await (await fetch("/api/state")).json().catch(() => ({}));
        if (st.loaded) { state.dashboard = st.dashboard; renderWorkspace(st.summary, st.quality, st.dashboard); setupAuto(st.auto); show("work"); return; }
        if (st.auto && st.auto.message) $("#loadingAuto").textContent = st.auto.message;
        if (st.auto && (st.auto.status === "no_files" || st.auto.status === "error")) break;
      }
    }
    show("upload");
  }
  boot();
})();
