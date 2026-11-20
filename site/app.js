/* Sigma Channel Watch: renders signals.json. Shared by the GitHub Pages site and the Claude artifact.
   SigmaApp.start({ load: () => Promise<signals>, loadChart: ticker => Promise<chart|null>, refreshMs }) */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const GAUGE_MIN = -5, GAUGE_MAX = 3;
  const FILTERS = [
    ["ACTIONABLE", "Buy + near"], ["BUY", "Buy"], ["NEAR", "Near"], ["NEUTRAL", "Waiting"], ["EXCLUDED", "Excluded"], ["ALL", "All"],
  ];
  const STATUS_LABEL = { BUY: "Buy zone", NEAR: "Near", NEUTRAL: "Waiting", EXCLUDED: "Excluded" };
  const ORDER = { BUY: 0, NEAR: 1, NEUTRAL: 2, EXCLUDED: 3 };

  let opts, data = null, filter = "ACTIONABLE", query = "", sortKey = null, sortDir = 1, selected = null, chart = null, chartReq = 0;

  const money = v => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = v => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  const sig = v => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "σ";
  const bigMoney = v => v == null ? "—" : v >= 1e12 ? "$" + (v / 1e12).toFixed(2) + "T" : "$" + (v / 1e9).toFixed(0) + "B";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tvUrl = r => "https://www.tradingview.com/chart/?symbol=" + encodeURIComponent(r.tv_symbol || r.ticker);
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const gx = z => ((Math.max(GAUGE_MIN, Math.min(GAUGE_MAX, z)) - GAUGE_MIN) / (GAUGE_MAX - GAUGE_MIN)) * 100;

  function gauge(r) {
    if (r.z == null) return '<span class="zval">—</span>';
    const buy = gx(r.buy_sigma);
    return `<div class="gcell"><div class="gauge" aria-hidden="true">
      <div class="track"></div><div class="zone" style="left:0;width:${buy}%"></div>
      <div class="tick" style="left:${gx(-4)}%"></div><div class="tick" style="left:${gx(-3)}%"></div>
      <div class="tick reg" style="left:${gx(0)}%"></div>
      <div class="pt ${r.status}" style="left:${gx(r.z)}%"></div></div>
      <span class="zval">${sig(r.z)}</span></div>`;
  }

  function renderMeta() {
    const d = data;
    const when = new Date(d.generated_at);
    const open = d.market === "open";
    $("meta").innerHTML =
      `<div><span class="dot ${open ? "open" : ""}"></span>NYSE <b>${esc(d.market)}</b></div>` +
      `<div>Last scan <b>${when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</b></div>` +
      `<div>Prices as of ${esc(d.last_bar_date)}</div>`;
  }

  function renderSummary() {
    const c = data.counts;
    const tiles = [
      ["BUY", "buy", "Buy zone", "At or below the buy line"],
      ["NEAR", "near", "Near", `Within ${data.config.near_sigma}σ of the buy line`],
      ["NEUTRAL", "", "Waiting", "Valid channel, not cheap yet"],
      ["EXCLUDED", "off", "Excluded", "Fails a channel rule"],
    ];
    $("summary").innerHTML = tiles.map(([k, cls, label, help]) =>
      `<button type="button" class="stat ${cls}" data-f="${k}"><span class="k">${label}</span><span class="v">${c[k] ?? 0}</span><span class="h">${help}</span></button>`
    ).join("");
    $("summary").querySelectorAll(".stat").forEach(b => b.onclick = () => { filter = b.dataset.f; renderChips(); renderRows(); });
  }

  function renderChips() {
    $("chips").innerHTML = FILTERS.map(([k, label]) =>
      `<button type="button" role="tab" class="chip" data-f="${k}" aria-selected="${k === filter}">${label}</button>`).join("");
    $("chips").querySelectorAll(".chip").forEach(b => b.onclick = () => { filter = b.dataset.f; renderChips(); renderRows(); });
  }

  function visibleRows() {
    let rows = data.stocks.filter(r =>
      filter === "ALL" ? true : filter === "ACTIONABLE" ? (r.status === "BUY" || r.status === "NEAR") : r.status === filter);
    if (query) {
      const q = query.toLowerCase();
      rows = rows.filter(r => r.ticker.toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q));
    }
    if (sortKey) {
      rows = rows.slice().sort((a, b) => {
        const va = a[sortKey], vb = b[sortKey];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (sortKey === "status") return (ORDER[va] - ORDER[vb]) * sortDir;
        return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sortDir;
      });
    }
    return rows;
  }

  function renderRows() {
    const rows = visibleRows();
    if (!rows.length) {
      const msg = filter === "ACTIONABLE"
        ? "No stock is in or near its buy zone right now. Cash is a position too. Alerts fire as soon as one crosses its buy line."
        : "No stocks match this filter.";
      $("rows").innerHTML = `<tr><td colspan="8" class="empty">${msg}</td></tr>`;
      return;
    }
    $("rows").innerHTML = rows.map(r => `
      <tr data-t="${esc(r.ticker)}" tabindex="0" class="${selected === r.ticker ? "sel" : ""}">
        <td><span class="tk">${esc(r.ticker)}</span><span class="nm">${esc(r.name)}</span></td>
        <td class="hide-s"><span class="tier ${r.tier}">${r.tier === "top" ? "Top 10 · −3σ" : "Other · −4σ"}</span></td>
        <td class="num">${money(r.price)}<br><span class="chg ${r.change_pct > 0 ? "up" : r.change_pct < 0 ? "dn" : ""}">${pct(r.change_pct)}</span></td>
        <td>${gauge(r)}</td>
        <td class="num hide-s">${money(r.buy_line)}</td>
        <td class="num">${r.status === "BUY" ? "in zone" : pct(r.dist_to_buy_pct == null ? null : -r.dist_to_buy_pct)}</td>
        <td class="num hide-s">${pct(r.upside_to_reg_pct)}</td>
        <td>${r.status === "EXCLUDED"
          ? `<span class="pill EXCLUDED">Excluded</span><div class="why">${esc(r.reason)}</div>`
          : `<span class="pill ${r.status}">${STATUS_LABEL[r.status]}</span>`}</td>
      </tr>`).join("");
    $("rows").querySelectorAll("tr[data-t]").forEach(tr => {
      tr.onclick = () => select(tr.dataset.t, true);
      tr.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(tr.dataset.t, true); } };
    });
  }

  function fact(label, value) { return `<div><dt>${label}</dt><dd>${value}</dd></div>`; }

  async function select(ticker, scroll) {
    const r = data.stocks.find(s => s.ticker === ticker);
    if (!r) return;
    selected = ticker;
    document.querySelectorAll("#rows tr").forEach(tr => tr.classList.toggle("sel", tr.dataset.t === ticker));
    $("d-ticker").innerHTML = `${esc(r.ticker)} <span class="pill ${r.status}">${STATUS_LABEL[r.status]}</span>`;
    $("d-name").textContent = `${r.name}${r.market_cap ? " · " + bigMoney(r.market_cap) : ""}${r.rank ? " · #" + r.rank + " by market cap" : ""}`;
    $("tv-link").href = tvUrl(r);
    $("facts").innerHTML = r.z == null ? "" :
      fact("Price", money(r.price)) +
      fact("Channel position", sig(r.z)) +
      fact(`Buy line (${r.buy_sigma}σ)`, money(r.buy_line)) +
      fact("Target (regression)", money(r.reg)) +
      fact("Upside to target", pct(r.upside_to_reg_pct)) +
      fact("Trend growth / yr", pct(r.slope_pct_per_year));
    const old = $("detail").querySelector(".reason");
    if (old) old.remove();
    if (r.status === "EXCLUDED") $("facts").insertAdjacentHTML("afterend", `<p class="reason">Excluded: ${esc(r.reason)}</p>`);
    if (scroll) $("detail").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    const req = ++chartReq;
    const c = await opts.loadChart(ticker).catch(() => null);
    if (req === chartReq) drawChart(c);
  }

  function drawChart(c) {
    const el = $("chart");
    if (chart) { chart.remove(); chart = null; }
    el.innerHTML = "";
    if (!c || !window.LightweightCharts) {
      el.innerHTML = `<div class="empty">${!window.LightweightCharts ? "The chart library did not load." : (opts.noChartText || "No chart: this stock has less than 10 years of history.")}</div>`;
      return;
    }
    chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { type: "solid", color: css("--panel") }, textColor: css("--muted"), fontFamily: css("--f-mono") },
      grid: { vertLines: { color: css("--gauge") }, horzLines: { color: css("--gauge") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line") },
      crosshair: { mode: 0 },
    });
    // c.i / c.n: bar indexes of a down-sampled series (artifact); otherwise every bar is present
    const n = c.n || c.d.length;
    const at = (j, k) => {
      const i = c.i ? c.i[j] : j;
      return c.reg0 + (c.reg1 - c.reg0) * (i / (n - 1)) + k * c.sigma;
    };
    const line = (k, color, style, width) => {
      const s = chart.addLineSeries({ color, lineWidth: width || 1, lineStyle: style, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
      s.setData(c.d.map((d, i) => ({ time: d * 86400, value: +at(i, k).toFixed(2) })));
    };
    line(2, css("--off"), 2); line(1, css("--off"), 2);
    line(0, css("--accent"), 0, 2);
    line(-3, css("--near"), 2); line(-4, css("--buy"), 2);
    const price = chart.addLineSeries({ color: css("--fg"), lineWidth: 1.5, priceLineVisible: false });
    price.setData(c.d.map((d, i) => ({ time: d * 86400, value: c.c[i] })));
    chart.timeScale().fitContent();
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
    if (!data.counts) {
      data.counts = {};
      data.stocks.forEach(s => { data.counts[s.status] = (data.counts[s.status] || 0) + 1; });
    }
    renderMeta(); renderSummary(); renderChips(); renderRows();
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
    // redraw the chart in the new palette when the theme changes
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => selected && select(selected, false));
    new MutationObserver(() => selected && select(selected, false)).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    refresh(true);
    if (o.refreshMs) setInterval(() => refresh(false), o.refreshMs);
  }

  window.SigmaApp = { start, refresh: () => opts && refresh(false) };
})();
