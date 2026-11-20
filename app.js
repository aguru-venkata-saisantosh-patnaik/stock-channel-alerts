/* Sigma Channel Watch: renders signals.json. Shared by the GitHub Pages site and the Claude artifact.
   SigmaApp.start({ load, loadChart, refreshMs, staleCheck, noChartText })
     load():          Promise<signals.json>
     loadChart(t):    Promise<{d: [days since epoch], c: [closes]} | null>
     staleCheck:      true → health bar shows "last scan N min ago" and flags missed scans */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const GAUGE_MIN = -5, GAUGE_MAX = 3;
  const FILTERS = [
    ["ACTIONABLE", "Buy + near"], ["MINE", "Mine"], ["BUY", "Buy"], ["NEAR", "Near"],
    ["NEUTRAL", "Waiting"], ["EXCLUDED", "Excluded"], ["ALL", "All"],
  ];
  const WINDOWS = [["main", "Main"], ["10", "10y"], ["7", "7y"], ["5", "5y"], ["3", "3y"], ["1", "1y"]];
  const STATUS_LABEL = { BUY: "Buy zone", NEAR: "Near", NEUTRAL: "Waiting", EXCLUDED: "Excluded" };
  const ORDER = { BUY: 0, NEAR: 1, NEUTRAL: 2, EXCLUDED: 3 };

  let opts, data = null, filter = "ACTIONABLE", query = "", sortKey = null, sortDir = 1;
  let win = "main", selected = null, chart = null, chartReq = 0, chartData = null;
  try { win = localStorage.getItem("sigma.win") || "main"; } catch (e) { /* storage blocked */ }

  const money = v => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = v => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  const sig = v => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "σ";
  const lvl = v => (v > 0 ? "+" : "−") + Math.abs(v) + "σ";
  const bigMoney = v => v == null ? "—" : v >= 1e12 ? "$" + (v / 1e12).toFixed(2) + "T" : "$" + (v / 1e9).toFixed(0) + "B";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tvUrl = r => "https://www.tradingview.com/chart/?symbol=" + encodeURIComponent(r.tv_symbol || r.ticker);
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const gx = z => ((Math.max(GAUGE_MIN, Math.min(GAUGE_MAX, z)) - GAUGE_MIN) / (GAUGE_MAX - GAUGE_MIN)) * 100;
  const winBars = y => +y === 10 ? data.config.length : +y * data.config.bars_per_year;

  /* Same math as engine/channel.py: least-squares line over the last n closes,
     sigma = population standard deviation of the residuals. */
  function channel(c, n) {
    if (!c || c.length < n || n < 2) return null;
    const y = c.slice(c.length - n);
    const xm = (n - 1) / 2;
    let ym = 0; for (const v of y) ym += v; ym /= n;
    let sxy = 0, sxx = 0;
    for (let i = 0; i < n; i++) { sxy += (i - xm) * (y[i] - ym); sxx += (i - xm) * (i - xm); }
    const slope = sxy / sxx, intercept = ym - slope * xm;
    let ss = 0;
    for (let i = 0; i < n; i++) { const r = y[i] - (intercept + slope * i); ss += r * r; }
    const sigma = Math.sqrt(ss / n);
    const reg = intercept + slope * (n - 1);
    return { slope, intercept, sigma, reg, z: sigma > 0 ? (y[n - 1] - reg) / sigma : 0, n };
  }

  /* A row as seen through the selected lookback window. */
  function view(r) {
    if (win === "main") return r;
    const w = r.windows && r.windows[win];
    if (!w) return Object.assign({}, r, {
      status: "EXCLUDED", reason: `Less than ${win} year${win === "1" ? "" : "s"} of history`,
      z: null, buy_line: null, dist_to_buy_pct: null, upside_to_reg_pct: null, reg: null, hit_levels: [],
    });
    return Object.assign({}, r, w);
  }
  const winLabel = r => win !== "main" ? `${win}y` : (r.main_window ? `${r.main_window}y` : "—");

  function gauge(v) {
    if (v.z == null) return '<span class="zval">—</span>';
    const buy = gx(v.buy_sigma);
    return `<div class="gcell"><div class="gauge" aria-hidden="true">
      <div class="track"></div><div class="zone" style="left:0;width:${buy}%"></div>
      <div class="tick" style="left:${gx(-4)}%"></div><div class="tick" style="left:${gx(-3)}%"></div>
      <div class="tick reg" style="left:${gx(0)}%"></div>
      <div class="pt ${v.status}" style="left:${gx(v.z)}%"></div></div>
      <span class="zval">${sig(v.z)}</span></div>`;
  }

  function statusPill(v) {
    if (v.status === "EXCLUDED") return `<span class="pill EXCLUDED">Excluded</span><div class="why">${esc(v.reason)}</div>`;
    let label = STATUS_LABEL[v.status];
    if (v.status === "BUY" && v.hit_levels && v.hit_levels.length) label = "Buy " + lvl(Math.min(...v.hit_levels));
    return `<span class="pill ${v.status}">${label}</span>`;
  }

  function tierLabel(r) {
    const badge = r.short_channel ? ` <span class="badge">${esc(r.main_window)}y channel</span>` : "";
    if (r.mine) return `<span class="tier mine">Yours · ${r.buy_levels.map(lvl).join(" & ")}</span>${badge}`;
    return r.tier === "top" ? `<span class="tier top">Top 10 · −3σ</span>` : `<span class="tier">Other · −4σ</span>`;
  }

  /* ---------- health bar ---------- */
  function nyMarketOpen(d) {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "numeric", hourCycle: "h23",
    }).formatToParts(d).map(x => [x.type, x.value]));
    const mins = +p.hour * 60 + +p.minute;
    return !["Sat", "Sun"].includes(p.weekday) && mins >= 9 * 60 + 30 && mins <= 16 * 60;
  }

  function renderHealth() {
    const h = data.health || { status: "warn", checks: [] };
    const now = new Date();
    const age = Math.round((now - new Date(data.generated_at)) / 60000);
    let status = h.status;
    const extra = [];
    if (opts.staleCheck) {
      if (nyMarketOpen(now) && age > 60) { status = "fail"; extra.push(`No scan for ${age} minutes during market hours. GitHub may have skipped runs (check the Actions tab), or today is a market holiday.`); }
      else if (nyMarketOpen(now) && age > 25) { if (status === "ok") status = "warn"; extra.push(`Last scan ${age} minutes ago; scans should run every 10 minutes.`); }
    }
    const bad = h.checks.filter(c => c.status !== "ok");
    const word = { ok: "All checks pass", warn: "Needs attention", fail: "Problem detected" }[status];
    const ageText = opts.staleCheck
      ? (age < 1 ? "Last scan just now" : `Last scan ${age < 120 ? age + " min" : Math.round(age / 60) + " h"} ago`)
      : `Data from ${new Date(data.generated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} (daily copy)`;
    const el = $("health");
    el.className = "health " + status;
    el.innerHTML = `
      <details>
        <summary><span class="hdot ${status}"></span><b>${word}</b><span class="hage">${esc(ageText)}</span>
          ${bad.length || extra.length ? `<span class="hmsg">${esc([...extra, ...bad.map(c => c.label + ": " + c.detail)][0])}</span>` : ""}
          <span class="hmore">Details</span></summary>
        <ul class="hlist">
          ${extra.map(t => `<li class="fail"><span class="hdot fail"></span>${esc(t)}</li>`).join("")}
          ${h.checks.map(c => `<li class="${c.status}"><span class="hdot ${c.status}"></span><b>${esc(c.label)}</b> ${esc(c.detail)}</li>`).join("")}
        </ul>
      </details>`;
  }

  /* ---------- page ---------- */
  function renderMeta() {
    const open = data.market === "open";
    $("meta").innerHTML =
      `<div><span class="dot ${open ? "open" : ""}"></span>NYSE <b>${esc(data.market)}</b></div>` +
      `<div>Last scan <b>${new Date(data.generated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</b></div>` +
      `<div>Prices as of ${esc(data.last_bar_date)}</div>`;
  }

  function renderSummary() {
    const c = { BUY: 0, NEAR: 0, NEUTRAL: 0, EXCLUDED: 0 };
    data.stocks.forEach(r => { c[view(r).status]++; });
    const tiles = [
      ["BUY", "buy", "Buy zone", "At or below a buy line"],
      ["NEAR", "near", "Near", `Within ${data.config.near_sigma}σ of the buy line`],
      ["NEUTRAL", "", "Waiting", "Valid channel, not cheap yet"],
      ["EXCLUDED", "off", "Excluded", "Fails a channel rule"],
    ];
    $("summary").innerHTML = tiles.map(([k, cls, label, help]) =>
      `<button type="button" class="stat ${cls}" data-f="${k}"><span class="k">${label} · ${win === "main" ? "main" : win + "y"}</span><span class="v">${c[k]}</span><span class="h">${help}</span></button>`
    ).join("");
    $("summary").querySelectorAll(".stat").forEach(b => b.onclick = () => { filter = b.dataset.f; renderChips(); renderRows(); });
  }

  function renderLookback() {
    $("lookback").innerHTML = `<span class="lb-label">Channel lookback</span>` + WINDOWS.map(([k, label]) =>
      `<button type="button" class="seg" data-w="${k}" aria-pressed="${k === win}">${label}</button>`).join("") +
      `<span class="lb-help">${win === "main" ? "Main = 10 years, or the longest your ticker has" : "Showing every stock on its " + win + "-year channel"}</span>`;
    $("lookback").querySelectorAll(".seg").forEach(b => b.onclick = () => {
      win = b.dataset.w;
      try { localStorage.setItem("sigma.win", win); } catch (e) { /* storage blocked */ }
      renderLookback(); renderSummary(); renderRows();
      if (selected) renderDetail(selected), drawChart();
    });
  }

  function renderChips() {
    $("chips").innerHTML = FILTERS.map(([k, label]) =>
      `<button type="button" role="tab" class="chip" data-f="${k}" aria-selected="${k === filter}">${label}</button>`).join("");
    $("chips").querySelectorAll(".chip").forEach(b => b.onclick = () => { filter = b.dataset.f; renderChips(); renderRows(); });
  }

  function visibleRows() {
    let rows = data.stocks.map(r => [r, view(r)]).filter(([r, v]) =>
      filter === "ALL" ? true : filter === "MINE" ? r.mine :
      filter === "ACTIONABLE" ? (v.status === "BUY" || v.status === "NEAR") : v.status === filter);
    if (query) {
      const q = query.toLowerCase();
      rows = rows.filter(([r]) => r.ticker.toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q));
    }
    if (sortKey) {
      rows.sort(([, a], [, b]) => {
        const va = a[sortKey], vb = b[sortKey];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (sortKey === "status") return (ORDER[va] - ORDER[vb]) * sortDir;
        return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sortDir;
      });
    } else {
      const gap = v => v.z == null ? 99 : v.z - v.buy_sigma;
      rows.sort(([, a], [, b]) => (ORDER[a.status] - ORDER[b.status]) || (gap(a) - gap(b)));
    }
    return rows;
  }

  function renderRows() {
    const rows = visibleRows();
    if (!rows.length) {
      const msg = filter === "ACTIONABLE"
        ? `No stock is in or near its buy zone on the ${win === "main" ? "main" : win + "-year"} channel. Cash is a position too.`
        : "No stocks match this filter.";
      $("rows").innerHTML = `<tr><td colspan="8" class="empty">${msg}</td></tr>`;
      return;
    }
    $("rows").innerHTML = rows.map(([r, v]) => `
      <tr data-t="${esc(r.ticker)}" tabindex="0" class="${selected === r.ticker ? "sel" : ""}">
        <td><span class="tk">${esc(r.ticker)}</span><span class="nm">${esc(r.name)}</span></td>
        <td class="hide-s">${tierLabel(r)}</td>
        <td class="num">${money(r.price)}<br><span class="chg ${r.change_pct > 0 ? "up" : r.change_pct < 0 ? "dn" : ""}">${pct(r.change_pct)}</span></td>
        <td>${gauge(v)}</td>
        <td class="num hide-s">${money(v.buy_line)}</td>
        <td class="num">${v.status === "BUY" ? "in zone" : pct(v.dist_to_buy_pct == null ? null : -v.dist_to_buy_pct)}</td>
        <td class="num hide-s">${pct(v.upside_to_reg_pct)}</td>
        <td>${statusPill(v)}</td>
      </tr>`).join("");
    $("rows").querySelectorAll("tr[data-t]").forEach(tr => {
      tr.onclick = () => select(tr.dataset.t, true);
      tr.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(tr.dataset.t, true); } };
    });
  }

  const fact = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;

  function renderDetail(ticker) {
    const r = data.stocks.find(s => s.ticker === ticker);
    if (!r) return;
    const v = view(r);
    $("d-ticker").innerHTML = `${esc(r.ticker)} ${statusPill(v).replace(/<div class="why">.*<\/div>/, "")}`;
    $("d-name").innerHTML = esc(`${r.name}${r.market_cap ? " · " + bigMoney(r.market_cap) : ""}${r.rank ? " · #" + r.rank + " by market cap" : ""}`)
      + (r.mine ? ` · <span class="tier mine">your ticker</span>` : "");
    $("tv-link").href = tvUrl(r);
    const levels = r.buy_levels || [v.buy_sigma];
    $("facts").innerHTML = v.z == null ? "" :
      fact("Channel", `${winLabel(r)}${r.short_channel && win === "main" ? " (short)" : ""}`) +
      fact("Price", money(r.price)) +
      fact("Position", sig(v.z)) +
      levels.map(l => fact(`Buy line ${lvl(l)}`, money(v.buy_lines ? v.buy_lines[String(l)] : v.buy_line))).join("") +
      fact("Target (regression)", money(v.reg)) +
      fact("Upside to target", pct(v.upside_to_reg_pct)) +
      fact("Trend growth / yr", pct(v.slope_pct_per_year));
    const old = $("detail").querySelector(".reason");
    if (old) old.remove();
    if (v.status === "EXCLUDED") $("facts").insertAdjacentHTML("afterend", `<p class="reason">Excluded on the ${winLabel(r)} channel: ${esc(v.reason)}</p>`);
  }

  async function select(ticker, scroll) {
    if (!data.stocks.some(s => s.ticker === ticker)) return;
    selected = ticker;
    document.querySelectorAll("#rows tr").forEach(tr => tr.classList.toggle("sel", tr.dataset.t === ticker));
    renderDetail(ticker);
    if (scroll) $("detail").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    const req = ++chartReq;
    const c = await opts.loadChart(ticker).catch(() => null);
    if (req !== chartReq) return;
    chartData = c;
    drawChart();
  }

  function drawChart() {
    const el = $("chart");
    if (chart) { chart.remove(); chart = null; }
    el.innerHTML = "";
    const c = chartData;
    const r = data.stocks.find(s => s.ticker === selected);
    if (!window.LightweightCharts) { el.innerHTML = `<div class="empty">The chart library did not load.</div>`; return; }
    if (!c || !r) { el.innerHTML = `<div class="empty">${esc(opts.noChartText || "No chart data for this ticker.")}</div>`; return; }

    chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { type: "solid", color: css("--panel") }, textColor: css("--muted"), fontFamily: css("--f-mono") },
      grid: { vertLines: { color: css("--gauge") }, horzLines: { color: css("--gauge") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line") },
      crosshair: { mode: 0 },
    });
    const yw = win === "main" ? r.main_window : win;
    const n = yw ? winBars(yw) : null;
    const ch = n ? channel(c.c, n) : null;
    if (ch) {
      const start = c.c.length - n;
      const line = (k, color, style, width) => {
        const s = chart.addLineSeries({ color, lineWidth: width || 1, lineStyle: style, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
        const pts = [];
        for (let i = 0; i < n; i++) pts.push({ time: c.d[start + i] * 86400, value: +(ch.intercept + ch.slope * i + k * ch.sigma).toFixed(2) });
        s.setData(pts);
      };
      line(2, css("--off"), 2); line(1, css("--off"), 2);
      line(0, css("--accent"), 0, 2);
      line(-3, css("--near"), 2); line(-4, css("--buy"), 2);
    }
    const price = chart.addLineSeries({ color: css("--fg"), lineWidth: 1.5, priceLineVisible: false });
    price.setData(c.d.map((d, i) => ({ time: d * 86400, value: c.c[i] })));
    if (ch && win !== "main" && +win < 10) {
      // focus on the window, with a little history before it
      const from = c.d[Math.max(0, c.c.length - Math.round(n * 1.25))] * 86400;
      chart.timeScale().setVisibleRange({ from, to: c.d[c.d.length - 1] * 86400 });
    } else chart.timeScale().fitContent();
    $("chart-note").textContent = ch ? "" :
      yw ? `Not enough history for a ${yw}-year channel yet: price only.` : "Not enough history for any channel yet: price only.";
  }

  async function copyPrompt() {
    const r = data && data.stocks.find(s => s.ticker === selected);
    if (!r) return;
    const text = data.prompt_template.replace(/\{name\}/g, r.name).replace(/\{ticker\}/g, r.ticker);
    const t = $("toast");
    try {
      await navigator.clipboard.writeText(text);
      t.textContent = `Prompt for ${r.ticker} copied. Paste it into an AI assistant with web search turned on.`;
    } catch {
      t.textContent = "Copying is blocked here. Select the prompt below and copy it manually.";
      const pre = document.createElement("pre");
      pre.style.cssText = "white-space:pre-wrap;font-size:12px;margin:0";
      pre.textContent = text;
      t.after(pre);
    }
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 6000);
  }

  async function refresh(first) {
    try {
      data = await opts.load();
    } catch (e) {
      if (first) $("meta").innerHTML = `<span class="err">Could not load the scan (${esc(e.message)}). It appears after the first scheduled run.</span>`;
      return;
    }
    if (!data || !data.stocks) return;
    renderHealth(); renderMeta(); renderLookback(); renderSummary(); renderChips(); renderRows();
    const keep = selected && data.stocks.some(s => s.ticker === selected);
    const pick = keep ? selected : (data.stocks.find(s => s.z != null && s.status !== "EXCLUDED") || data.stocks[0] || {}).ticker;
    if (pick) select(pick, false);
  }

  function start(o) {
    opts = o;
    $("copy-prompt").onclick = copyPrompt;
    $("search").oninput = e => { query = e.target.value.trim(); renderRows(); };
    document.querySelectorAll("th[data-k]").forEach(th => {
      th.tabIndex = 0;
      const go = () => {
        const k = th.dataset.k;
        sortDir = sortKey === k ? -sortDir : 1; sortKey = k;
        document.querySelectorAll("th").forEach(x => x.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending");
        renderRows();
      };
      th.onclick = go;
      th.onkeydown = e => { if (e.key === "Enter") go(); };
    });
    const redraw = () => { if (selected && chartData) drawChart(); };
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
    new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    refresh(true);
    if (o.refreshMs) setInterval(() => refresh(false), o.refreshMs);
    if (o.staleCheck) setInterval(() => data && renderHealth(), 60000);
  }

  window.SigmaApp = { start, refresh: () => opts && refresh(false), channel };
})();
