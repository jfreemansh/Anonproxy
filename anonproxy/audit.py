"""
Audit dashboard — a self-contained page for reviewing what was anonymized.

Served by the proxy at ``/audit``.  It shows the live ``original → surrogate``
mapping for the current engagement, filterable by entity type, with counts, so
you can verify coverage at a glance during an engagement and export the table
for your evidence trail at close.

Security: the page exposes the reverse lookup, so it is bound to the proxy's
listen address (localhost by default — reach a VPS instance only over the SSH
tunnel) and honours ``ANONPROXY_API_TOKEN`` if set.  Disable entirely with
``ANONPROXY_AUDIT=false``.
"""
from __future__ import annotations

import html
import json


def render_page(engagement: str, token_required: bool) -> str:
    # token (if any) is read from the page URL ?token=... and forwarded as a
    # header on the data fetch, so it never needs to be embedded server-side.
    return _PAGE.replace("__ENGAGEMENT__", html.escape(engagement)) \
                .replace("__TOKEN_REQUIRED__", json.dumps(token_required))


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anonproxy audit — __ENGAGEMENT__</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; background: #0d1117; color: #c9d1d9; }
  header { padding: 16px 20px; border-bottom: 1px solid #21262d;
           display: flex; gap: 20px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 16px; margin: 0; color: #58a6ff; }
  .eng { color: #8b949e; }
  .toolbar { padding: 12px 20px; display: flex; gap: 10px; flex-wrap: wrap;
             align-items: center; border-bottom: 1px solid #21262d; }
  input, select, button { background: #161b22; color: #c9d1d9;
            border: 1px solid #30363d; border-radius: 6px; padding: 6px 10px;
            font: inherit; }
  button { cursor: pointer; }
  button:hover { border-color: #58a6ff; }
  .stats { padding: 10px 20px; color: #8b949e; display: flex; gap: 16px;
           flex-wrap: wrap; }
  .pill { background: #161b22; border: 1px solid #30363d; border-radius: 999px;
          padding: 2px 10px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 20px; border-bottom: 1px solid #21262d;
           vertical-align: top; word-break: break-all; }
  th { position: sticky; top: 0; background: #0d1117; color: #8b949e;
       font-weight: 600; cursor: pointer; }
  tr:hover td { background: #11161d; }
  .type { color: #d2a8ff; }
  .orig { color: #ff7b72; }
  .surr { color: #7ee787; }
  .muted { color: #6e7681; }
  .err { color: #ff7b72; padding: 20px; }
</style>
</head>
<body>
<header>
  <h1>🛡️ Anonproxy audit</h1>
  <span class="eng">engagement: <b>__ENGAGEMENT__</b></span>
  <span class="muted" id="updated"></span>
</header>
<div class="toolbar">
  <input id="q" placeholder="filter (original / surrogate / type)" size="34">
  <select id="type"><option value="">all types</option></select>
  <button id="refresh">↻ refresh</button>
  <button id="csv">⬇ export CSV</button>
  <label class="muted"><input type="checkbox" id="auto"> auto-refresh 5s</label>
</div>
<div class="stats" id="stats"></div>
<table>
  <thead><tr>
    <th data-k="entity_type">type</th>
    <th data-k="original">original</th>
    <th data-k="surrogate">surrogate</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<div id="error" class="err"></div>

<script>
const TOKEN_REQUIRED = __TOKEN_REQUIRED__;
const params = new URLSearchParams(location.search);
const token = params.get("token") || "";
let data = [], sortKey = "entity_type", sortAsc = true, timer = null;

function headers() {
  const h = {};
  if (TOKEN_REQUIRED && token) h["X-Anonproxy-Token"] = token;
  return h;
}

async function load() {
  document.getElementById("error").textContent = "";
  try {
    const [ex, st] = await Promise.all([
      fetch("/anonproxy/export", {headers: headers()}),
      fetch("/anonproxy/stats", {headers: headers()}),
    ]);
    if (!ex.ok) throw new Error("export " + ex.status + (ex.status===401?" — add ?token=…":""));
    data = (await ex.json()).mappings || [];
    renderStats(await st.json());
    populateTypes();
    render();
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("error").textContent = "Error: " + e.message;
  }
}

function renderStats(s) {
  const el = document.getElementById("stats");
  const parts = [`<span class="pill">total: <b>${s.total||0}</b></span>`];
  for (const [k, v] of Object.entries(s.by_type || {}))
    parts.push(`<span class="pill">${k}: ${v}</span>`);
  el.innerHTML = parts.join("");
}

function populateTypes() {
  const sel = document.getElementById("type");
  const cur = sel.value;
  const types = [...new Set(data.map(d => d.entity_type))].sort();
  sel.innerHTML = '<option value="">all types</option>' +
    types.map(t => `<option ${t===cur?"selected":""}>${t}</option>`).join("");
}

function render() {
  const q = document.getElementById("q").value.toLowerCase();
  const t = document.getElementById("type").value;
  let rows = data.filter(d =>
    (!t || d.entity_type === t) &&
    (!q || (d.original+d.surrogate+d.entity_type).toLowerCase().includes(q)));
  rows.sort((a,b) => {
    const x=(a[sortKey]||"").toString(), y=(b[sortKey]||"").toString();
    return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
  });
  document.getElementById("rows").innerHTML = rows.map(d => `<tr>
    <td class="type">${esc(d.entity_type)}</td>
    <td class="orig">${esc(d.original)}</td>
    <td class="surr">${esc(d.surrogate)}</td></tr>`).join("") ||
    '<tr><td colspan="3" class="muted">no mappings yet</td></tr>';
}

function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

function toCSV() {
  const head = "entity_type,original,surrogate\n";
  const body = data.map(d =>
    [d.entity_type, d.original, d.surrogate]
      .map(v => '"'+(v||"").replace(/"/g,'""')+'"').join(",")).join("\n");
  const blob = new Blob([head+body], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "anonproxy-__ENGAGEMENT__.csv";
  a.click();
}

document.getElementById("q").oninput = render;
document.getElementById("type").onchange = render;
document.getElementById("refresh").onclick = load;
document.getElementById("csv").onclick = toCSV;
document.getElementById("auto").onchange = e => {
  clearInterval(timer);
  if (e.target.checked) timer = setInterval(load, 5000);
};
document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  sortAsc = sortKey === k ? !sortAsc : true;
  sortKey = k; render();
});
load();
</script>
</body>
</html>"""
