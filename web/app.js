/* DATA-01 · giao diện thử nghiệm (vanilla JS + Chart.js) */
"use strict";

const S = { token: null, user: null, cfg: null, view: "chat", conv: null, convs: [], charts: [] };
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
};

// ------------------------------------------------------------------ DOM helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) {
  let t = esc(s || "");
  t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const lines = t.split("\n");
  let out = "", inList = false;
  for (const ln of lines) {
    if (/^\s*- /.test(ln)) { if (!inList) { out += "<ul>"; inList = true; } out += "<li>" + ln.replace(/^\s*- /, "") + "</li>"; }
    else { if (inList) { out += "</ul>"; inList = false; } out += ln ? ln + "<br>" : "<br>"; }
  }
  if (inList) out += "</ul>";
  return out.replace(/(<br>)+$/, "");
}
function toast(msg, ms = 2600) {
  const t = h("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), ms);
}
function modal(title, body, actions = []) {
  const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) bg.remove(); } });
  const close = () => bg.remove();
  bg.append(h("div", { class: "card modal" },
    h("div", { class: "hd" }, h("b", { class: "grow" }, title), h("button", { class: "btn sm", onclick: close }, "Đóng")),
    h("div", { class: "bd" }, body),
    actions.length ? h("div", { class: "bd row", style: { justifyContent: "flex-end", borderTop: "1px solid var(--border)" } }, actions.map(a => a(close))) : null));
  document.body.append(bg);
  return close;
}
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: Object.assign({ "Content-Type": "application/json" }, S.token ? { Authorization: "Bearer " + S.token } : {}),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("Phiên đăng nhập đã hết hạn"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("Lỗi " + res.status));
  return data;
}

// ------------------------------------------------------------------ định dạng số (vi-VN)
const nf0 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const nfa = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 });
function fmtCompact(x, f) {
  if (x == null || isNaN(x)) return "—";
  if (f === "currency") {
    const a = Math.abs(x);
    if (a >= 1e9) return nf2.format(x / 1e9) + " tỷ ₫";
    if (a >= 1e6) return nf1.format(x / 1e6) + " tr ₫";
    return nf0.format(x) + " ₫";
  }
  if (f === "percent") return nf1.format(x) + "%";
  if (f === "ratio") return nf2.format(x);
  return Number.isInteger(x) ? nf0.format(x) : nfa.format(x);
}
function fmtAxis(x, f) {
  if (f === "currency") { const a = Math.abs(x); if (a >= 1e9) return nfa.format(x / 1e9) + " tỷ"; if (a >= 1e6) return nfa.format(x / 1e6) + " tr"; return nf0.format(x); }
  if (f === "percent") return nfa.format(x) + "%";
  return nfa.format(x);
}
function fmtCell(v, f) {
  if (v == null) return "—";
  if (typeof v !== "number") return String(v);
  if (f === "currency") return nf0.format(v);
  if (f === "percent") return nf2.format(v) + "%";
  if (f === "ratio") return nf2.format(v);
  return Number.isInteger(v) ? nf0.format(v) : nfa.format(v);
}
function fmtBytes(n) {
  n = Number(n || 0);
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? nf1.format(n) : nf0.format(n)) + " " + u[i];
}
function fmtTime(ts) { const d = new Date(ts * 1000); return d.toLocaleString("vi-VN"); }

// ------------------------------------------------------------------ biểu đồ
function seriesColors() { return [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar("--series-" + i)); }
function renderChart(container, spec) {
  if (!spec) return;
  if (spec.type === "kpi") {
    container.append(h("div", { class: "kpis", style: { marginTop: "12px" } }, spec.items.map(it =>
      h("div", { class: "kpi" }, h("div", { class: "l" }, it.label), h("div", { class: "v" }, fmtCompact(it.value, it.format)),
        it.delta_pct != null ? h("div", { class: "d" }, (it.delta_pct >= 0 ? "▲ +" : "▼ ") + nf1.format(it.delta_pct) + "% so với " + (it.prev_label || "kỳ trước") + " (" + fmtCompact(it.prev, it.format) + ")") : null))));
    return;
  }
  if (typeof Chart === "undefined") { container.append(h("div", { class: "callout warn" }, "Không tải được thư viện biểu đồ (Chart.js) — xem bảng dữ liệu.")); return; }
  const colors = seriesColors();
  const n = spec.labels.length;
  const hgt = spec.horizontal ? Math.max(260, Math.min(900, n * 26 + 70)) : 300;
  const box = h("div", { class: "chart-box", style: { height: hgt + "px" } });
  const cv = h("canvas", { role: "img", "aria-label": spec.title || "Biểu đồ" });
  box.append(cv);
  if (spec.title) container.append(h("div", { class: "sec", style: { marginTop: "12px", fontWeight: 600, fontSize: "13px" } }, spec.title));
  container.append(box);
  const surface = cssVar("--surface-1"), grid = cssVar("--grid"), muted = cssVar("--text-muted"), sec = cssVar("--text-secondary");
  const isLine = spec.type === "line", isPie = spec.type === "doughnut";
  const datasets = spec.datasets.map((d, i) => {
    const base = { label: d.label, data: d.data };
    if (isPie) return Object.assign(base, { backgroundColor: spec.labels.map((_, k) => colors[k % 8]), borderColor: surface, borderWidth: 2 });
    let col = colors[i % 8];
    if (d.role === "previous") col = cssVar("--series-muted");
    if (d.role === "current") col = colors[0];
    if (isLine) return Object.assign(base, { borderColor: col, backgroundColor: col, borderWidth: 2, pointRadius: n <= 24 ? 3 : 0,
      pointHoverRadius: 5, pointBorderColor: surface, pointBorderWidth: 2, tension: 0, spanGaps: true });
    const bg = d.role === "delta" ? d.data.map(v => (v >= 0 ? cssVar("--pos") : cssVar("--neg"))) : col;
    return Object.assign(base, { backgroundColor: bg, borderRadius: 4, borderSkipped: "start", maxBarThickness: 28,
      categoryPercentage: 0.72, barPercentage: 0.9, borderColor: surface, borderWidth: spec.stacked ? { top: 2 } : 0 });
  });
  const valAxis = { grid: { color: grid, drawTicks: false }, border: { display: false },
    ticks: { color: muted, padding: 6, callback: v => fmtAxis(v, spec.format) }, stacked: !!spec.stacked, beginAtZero: !isLine };
  const catAxis = { grid: { display: false }, border: { color: grid }, ticks: { color: muted, autoSkip: true, maxRotation: 0 }, stacked: !!spec.stacked };
  const ch = new Chart(cv, {
    type: isPie ? "doughnut" : (isLine ? "line" : "bar"),
    data: { labels: spec.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      indexAxis: spec.horizontal ? "y" : "x", cutout: isPie ? "62%" : undefined,
      interaction: isPie ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false },
      plugins: {
        legend: { display: isPie || datasets.length > 1, position: isPie ? "right" : "top", align: "start",
          labels: { color: sec, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 10, boxHeight: 10 } },
        tooltip: { backgroundColor: cssVar("--surface-1"), titleColor: cssVar("--text-primary"), bodyColor: sec,
          borderColor: cssVar("--border"), borderWidth: 1, padding: 10, usePointStyle: true,
          callbacks: { label: c => ` ${c.dataset.label}: ${fmtCompact(isPie ? c.parsed : (spec.horizontal ? c.parsed.x : c.parsed.y), spec.format)}` } },
      },
      scales: isPie ? {} : (spec.horizontal ? { x: valAxis, y: catAxis } : { x: catAxis, y: valAxis }),
    },
  });
  S.charts.push(ch);
}

function dataTable(columns, rows, meta, highlights) {
  const mm = {}; (meta || []).forEach(m => (mm[m.name] = m));
  const hl = {}; (highlights || []).forEach(x => (hl[x.row] = x.type === "down" ? "hl-down" : x.type === "up" ? "hl-up" : "hl-up"));
  return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, columns.map(c => h("th", { title: c, class: (mm[c] || {}).kind === "metric" ? "num" : "" }, (mm[c] && mm[c].label) || c)))),
    h("tbody", {}, rows.slice(0, 500).map((r, i) => h("tr", { class: hl[i] || "" }, r.map((v, j) => {
      const m = mm[columns[j]] || {};
      return h("td", { class: typeof v === "number" ? "num" : "" }, fmtCell(v, m.format));
    }))))));
}
function downloadCSV(columns, rows, name = "ket_qua.csv") {
  const q = v => (v == null ? "" : /[",\n;]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
  const csv = "﻿" + [columns.map(q).join(","), ...rows.map(r => r.map(q).join(","))].join("\n");
  const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: name });
  document.body.append(a); a.click(); a.remove();
}
function codeBlock(text) {
  return h("div", { class: "code" }, h("button", { class: "btn sm copy", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(text); toast("Đã sao chép"); } }, "Sao chép"), h("pre", {}, text));
}
function tabs(defs) {
  const bar = h("div", { class: "tabs" }); const pane = h("div", { class: "tabpane" });
  const items = defs.filter(Boolean);
  let active = -1;
  const show = i => {
    if (active === i) { active = -1; pane.innerHTML = ""; [...bar.children].forEach(b => b.classList.remove("active")); return; }
    active = i; pane.innerHTML = "";
    [...bar.children].forEach((b, k) => b.classList.toggle("active", k === i));
    pane.append(items[i].render());
  };
  items.forEach((d, i) => bar.append(h("button", { onclick: () => show(i) }, d.label)));
  return h("div", {}, bar, pane);
}
function sevIcon(sev) {
  const m = { critical: ["!", "Nghiêm trọng"], warning: ["!", "Cảnh báo"], info: ["i", "Thông tin"], ok: ["✓", "Ổn"], error: ["!", "Lỗi"] };
  const [ic, lab] = m[sev] || m.info;
  return h("span", { class: "sev " + (sev === "error" ? "critical" : sev) }, h("span", { class: "ic" }, ic), lab);
}

// ------------------------------------------------------------------ hiển thị câu trả lời
const SRC_LABEL = { semantic: "Semantic layer", "semantic+repair": "Semantic + tự sửa", llm: "Claude sinh SQL", fewshot: "Few-shot đã xác nhận", user: "SQL người dùng", user_edit: "SQL đã chỉnh sửa" };

function metaBadges(r) {
  const b = [];
  b.push(h("span", { class: "badge" }, r.mode === "claude" ? "🤖 Claude" : "⚙️ Offline NLU"));
  if (r.sql_source) b.push(h("span", { class: "badge info" }, SRC_LABEL[r.sql_source] || r.sql_source));
  if (r.cost && r.cost.human) b.push(h("span", { class: "badge " + (r.cost.level === "warn" ? "warn" : "") }, "Quét ~" + r.cost.human));
  if (r.audit) b.push(h("span", { class: "badge " + (r.audit.ok ? "ok" : "err"), title: "Mọi con số trong diễn giải được tái tính từ kết quả truy vấn" },
    r.audit.ok ? `✓ ${r.audit.facts_matched}/${r.audit.facts_checked} số liệu khớp kết quả` : "✗ Số liệu chưa kiểm chứng"));
  if (r.row_count != null) b.push(h("span", { class: "badge" }, nf0.format(r.row_count) + " dòng" + (r.truncated ? " (đã cắt)" : "")));
  if (r.repairs && r.repairs.length) b.push(h("span", { class: "badge warn" }, "🔧 Tự sửa " + r.repairs.length + " lần"));
  if (r.approved_by) b.push(h("span", { class: "badge ok" }, "Duyệt bởi " + r.approved_by));
  if (r.validation && r.validation.rls_applied && r.validation.rls_applied.length) b.push(h("span", { class: "badge info", title: r.validation.rls_applied.join(", ") }, "🔒 RLS"));
  b.push(h("span", { class: "badge" }, nf0.format(r.elapsed_ms || 0) + " ms"));
  return h("div", { class: "meta" }, b);
}
function callouts(r) {
  const out = [];
  if (r.assumptions && r.assumptions.length) out.push(h("div", { class: "callout info" }, h("span", {}, "ℹ️"), h("div", {}, h("b", {}, "Giả định: "), h("ul", {}, r.assumptions.map(a => h("li", {}, a))))));
  if (r.notes && r.notes.length) out.push(h("div", { class: "callout info" }, h("span", {}, "📝"), h("ul", {}, r.notes.map(a => h("li", {}, a)))));
  const lint = (r.lint || []).filter(l => l.severity !== "info");
  if (lint.length) out.push(h("div", { class: "callout warn" }, h("span", {}, "⚠️"), h("div", {}, h("b", {}, "Phân tích tĩnh SQL: "),
    h("ul", {}, lint.map(l => h("li", {}, h("code", {}, l.code), " ", l.message, l.hint ? h("span", { class: "muted" }, " — " + l.hint) : null))))));
  return out;
}
function detailTabs(r) {
  const v = r.validation || {};
  return tabs([
    r.sql && { label: "SQL", render: () => h("div", {}, r.sql_source === "semantic" || r.sql_source === "semantic+repair" ? h("div", { class: "muted", style: { marginBottom: "6px", fontSize: "12.5px" } }, "Sinh từ semantic layer — đúng định nghĩa chỉ số chuẩn.") : null, codeBlock(r.sql),
      v.sql_exec && v.sql_exec !== r.sql ? h("div", { style: { marginTop: "8px" } }, h("div", { class: "muted", style: { fontSize: "12.5px", marginBottom: "4px" } }, "SQL thực thi (đã tiêm RLS / LIMIT):"), codeBlock(v.sql_exec)) : null) },
    r.plan && { label: "Kế hoạch truy vấn", render: () => codeBlock(JSON.stringify(r.plan, null, 2)) },
    v.checks && { label: "Kiểm duyệt", render: () => h("div", {}, h("ul", { class: "checks" }, v.checks.map(c => h("li", {}, c.passed ? "✅" : "❌", h("b", {}, c.name), h("span", { class: "muted" }, c.detail || "")))),
      (r.metrics_matched || []).length ? h("div", { class: "callout ok" }, "✓ Khớp định nghĩa semantic layer: ", r.metrics_matched.map(m => m.label + (m.filter_matches ? "" : " (⚠ khác điều kiện lọc chuẩn)")).join(", ")) : null,
      (r.lint || []).filter(l => l.severity === "info").map(l => h("div", { class: "callout info" }, h("code", {}, l.code), " ", l.message))) },
    r.cost && r.cost.tables && r.cost.tables.length && { label: "Chi phí quét", render: () => costView(r.cost) },
    r.lineage && r.lineage.length && { label: "Lineage", render: () => h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "Cột kết quả"), h("th", {}, "Nguồn trong kho (dw)"), h("th", {}, "Nguồn gốc (raw)"))),
      h("tbody", {}, r.lineage.map(l => h("tr", {}, h("td", {}, h("b", {}, l.column)), h("td", { class: "mono" }, l.sources.join("\n")), h("td", { class: "mono", style: { whiteSpace: "pre-line" } }, l.raw_sources.join("\n"))))))) },
    { label: "Pipeline agent", render: () => h("div", {}, h("div", { class: "steps" }, (r.trace || []).map((t, i) => [i ? h("span", { class: "muted" }, "→") : null, h("span", { class: "step" }, t.node, " ", h("span", { class: "ms" }, t.ms + "ms"))])),
      h("div", { class: "trace-list", style: { marginTop: "8px" } }, (r.trace || []).filter(t => t.detail).map(t => h("div", {}, h("b", {}, t.node + ": "), t.detail))),
      (r.llm_trace || []).length ? h("div", { style: { marginTop: "8px" } }, h("b", {}, "Tool calls của Claude"), h("div", { class: "trace-list" }, r.llm_trace.map(t => h("div", {}, h("code", {}, t.tool), " ", JSON.stringify(t.input).slice(0, 160), " ", h("span", { class: "muted" }, t.ms + "ms"))))) : null) },
    r.repairs && r.repairs.length && { label: "Tự sửa lỗi", render: () => h("div", {}, r.repairs.map(x => h("div", { class: "finding" }, h("div", {}, h("b", {}, "Lần " + x.attempt + " · " + (x.agent === "claude" ? "Claude" : "Rule-based") + ": "), x.fix),
      h("div", { class: "muted", style: { fontSize: "12.5px" } }, (x.error || {}).code + " — " + (x.error || {}).message), x.sql_after ? h("details", {}, h("summary", {}, "SQL trước / sau"), codeBlock(x.sql_before), h("div", { style: { height: "6px" } }), codeBlock(x.sql_after)) : null))) },
    r.audit && { label: "Kiểm chứng số liệu", render: () => h("div", {}, h("div", { class: "callout " + (r.audit.ok ? "ok" : "err") }, r.audit.ok ? `Tất cả ${r.audit.numbers_in_text} token số trong diễn giải đều thuộc ${r.audit.facts_checked} dữ kiện được tái tính từ kết quả truy vấn.` : "Có số không kiểm chứng được: " + r.audit.unverified_numbers.join(", ")),
      h("div", { class: "tbl-wrap", style: { marginTop: "8px" } }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Dữ kiện"), h("th", {}, "Giá trị hiển thị"), h("th", {}, "Tái tính"), h("th", {}, "Khớp"))),
        h("tbody", {}, (r.audit.details || []).map(d => h("tr", {}, h("td", { class: "mono" }, d.fact), h("td", {}, d.text), h("td", { class: "mono" }, d.recomputed == null ? (d.source || "ngữ cảnh") : String(d.recomputed)), h("td", {}, d.match ? "✅" : "❌"))))))) },
  ]);
}
function costView(c) {
  return h("div", {},
    h("div", { class: "row", style: { marginBottom: "8px" } }, h("span", { class: "badge" }, "Tổng: " + c.human), h("span", { class: "badge " + (c.decision === "approval" ? "warn" : c.decision === "blocked" ? "err" : "ok") }, { auto: "Tự động chạy", approval: "Cần duyệt (HITL)", blocked: "Bị chặn" }[c.decision] || c.decision),
      c.thresholds ? h("span", { class: "muted", style: { fontSize: "12.5px" } }, `Ngưỡng vai trò: cảnh báo ${fmtBytes(c.thresholds.warn_bytes)} · duyệt ${fmtBytes(c.thresholds.approval_bytes)} · chặn ${fmtBytes(c.thresholds.max_bytes)}`) : null),
    h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Bảng", "Cột được đọc", "Dòng quét / tổng", "Cắt phân vùng", "Bytes"].map(x => h("th", {}, x)))),
      h("tbody", {}, c.tables.map(t => h("tr", {}, h("td", { class: "mono" }, t.table, t.large ? h("span", { class: "badge warn", style: { marginLeft: "4px" } }, "bảng lớn") : null),
        h("td", { class: "mono" }, t.columns.join(", ")), h("td", { class: "num" }, nf0.format(t.rows_scanned) + " / " + nf0.format(t.rows_total)),
        h("td", {}, t.partition_filter ? `${t.partition_column} ∈ [${t.partition_filter[0] || "…"}, ${t.partition_filter[1] || "…"}) · bỏ ${nf1.format(t.pruned_pct)}%` : (t.partition_column ? "không (quét toàn bảng)" : "—")),
        h("td", { class: "num" }, fmtBytes(t.bytes))))))),
    (c.reasons || []).length ? h("div", { class: "callout warn" }, c.reasons.join(" ")) : null);
}
function feedbackRow(r) {
  if (!r.run_id || !r.sql) return null;
  const box = h("div", { class: "feedback" }, "Kết quả có đúng không?");
  const up = h("button", { class: "btn sm", onclick: async () => { const x = await api("/api/feedback", { body: { run_id: r.run_id, rating: 1 } }); box.replaceChildren("Cảm ơn! " + (x.fewshot_added ? "Đã lưu thành ví dụ few-shot đã xác nhận." : "")); } }, "👍 Đúng");
  const down = h("button", { class: "btn sm", onclick: () => {
    const ta = h("textarea", { rows: 8, class: "mono" }, r.sql);
    const cm = h("input", { type: "text", placeholder: "Sai ở đâu? (không bắt buộc)", style: { width: "100%", marginBottom: "8px" } });
    modal("Góp ý / sửa SQL", h("div", {}, cm, h("div", { class: "muted", style: { fontSize: "12.5px", marginBottom: "4px" } }, "Nếu bạn sửa SQL đúng, hệ thống sẽ học thành ví dụ few-shot (sau khi kiểm duyệt):"), ta),
      [close => h("button", { class: "btn primary", onclick: async () => {
        const corrected = ta.value.trim() !== r.sql.trim() ? ta.value : null;
        const x = await api("/api/feedback", { body: { run_id: r.run_id, rating: -1, comment: cm.value, corrected_sql: corrected } });
        close(); box.replaceChildren(x.ok ? ("Đã ghi nhận góp ý. " + (x.fewshot_added ? "SQL sửa đã được lưu làm ví dụ." : "")) : x.error);
      } }, "Gửi")]);
  } }, "👎 Chưa đúng");
  box.append(up, down);
  return box;
}

function renderAnswer(r, ctx = {}) {
  const card = h("div", { class: "card msg-bot" });
  const body = h("div", { class: "answer" });
  card.append(body);
  const T = r.type;
  if (T === "clarification") {
    body.append(h("div", { class: "txt" }, "🤔 ", r.text));
    const opts = h("div", { class: "opts" });
    (r.clarification.options || []).forEach((o, i) => opts.append(h("button", { class: "btn", onclick: e => {
      [...opts.children].forEach(b => (b.disabled = true)); e.target.classList.add("primary");
      sendMessage(o, { id: r.clarification.id, option: i });
    } }, o)));
    body.append(opts, h("div", { class: "muted", style: { fontSize: "12.5px", marginTop: "6px" } }, "Lựa chọn sẽ được ghi nhớ cho các câu hỏi sau trong hội thoại này."));
    return card;
  }
  if (T === "pending_approval") {
    body.append(h("div", { class: "txt" }, h("b", {}, "⏸ Cần xác nhận trước khi chạy (Human-in-the-loop)")), h("div", { class: "sec", style: { marginTop: "4px" } }, r.text));
    body.append(metaBadges(r), ...callouts(r));
    if (r.cost) body.append(h("div", { style: { marginTop: "8px" } }, costView(r.cost)));
    const ta = h("textarea", { rows: Math.min(18, (r.sql || "").split("\n").length + 1), class: "mono", style: { marginTop: "10px" } }, r.sql || "");
    const msg = h("div", {});
    const act = async (action) => {
      [...btns.children].forEach(b => (b.disabled = true));
      msg.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), action === "approve" ? "Đang kiểm duyệt lại & chạy…" : "Đang hủy…"));
      try { const res = await api("/api/approve", { body: { run_id: r.run_id, action, sql: ta.value } }); msg.replaceChildren(); appendBot(res); }
      catch (e) { msg.replaceChildren(h("div", { class: "callout err" }, e.message)); }
    };
    const btns = h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { class: "btn primary", onclick: () => act("approve") }, "✓ Duyệt & chạy"),
      h("button", { class: "btn danger", onclick: () => act("reject") }, "✕ Từ chối"),
      h("span", { class: "muted", style: { fontSize: "12.5px" } }, "Bạn có thể chỉnh SQL — bản sửa sẽ được kiểm duyệt lại (cú pháp, phân quyền, chi phí)."));
    body.append(h("div", { class: "muted", style: { marginTop: "10px", fontSize: "12.5px" } }, "SQL sẽ chạy:"), ta, btns, msg, detailTabs(r));
    return card;
  }
  if (T === "error" || T === "blocked") {
    body.append(h("div", { class: "callout err" }, h("span", {}, T === "blocked" ? "⛔" : "⚠️"), h("div", {}, h("b", {}, T === "blocked" ? "Đã chặn: " : "Không thực hiện được: "), r.text || r.error)));
    body.append(...callouts(r), metaBadges(r));
    if (r.sql || r.validation) body.append(detailTabs(r));
    return card;
  }
  if (T === "rejected") { body.append(h("div", { class: "muted" }, "Đã từ chối chạy truy vấn. (Đã ghi log)")); return card; }
  if (T === "info") {
    body.append(h("div", { class: "txt", html: md(r.text) }));
    if (r.metric_cards) body.append(h("div", { class: "tbl-wrap", style: { marginTop: "10px" } }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["Chỉ số", "Chủ sở hữu", "Công thức", "Điều kiện", "Phân tích được theo"].map(x => h("th", {}, x)))),
      h("tbody", {}, r.metric_cards.map(c => h("tr", {}, h("td", {}, h("b", {}, c.label), h("div", { class: "muted mono" }, c.name)), h("td", {}, c.owner), h("td", { class: "mono" }, c.sql), h("td", { class: "mono" }, c.filter || "—"), h("td", { style: { fontSize: "12px" } }, c.dimensions.join(", "))))))));
    if (r.schema_tables) r.schema_tables.forEach(t => body.append(h("details", { style: { marginTop: "8px" } }, h("summary", {}, h("b", { class: "mono" }, t.table), " — ", t.description),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("tbody", {}, t.columns.map(c => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", { class: "muted" }, c.type), h("td", {}, c.description, c.restricted ? h("span", { class: "badge err", style: { marginLeft: "6px" } }, "🔒 hạn chế") : null)))))))));
    if (r.suggestions) body.append(h("div", { class: "row", style: { marginTop: "10px" } }, r.suggestions.map(s => h("button", { class: "chip", onclick: () => sendMessage(s) }, s))));
    return card;
  }
  // ---------------- result / variance
  body.append(h("div", { class: "txt", html: md(r.text) }));
  body.append(metaBadges(r), ...callouts(r));
  if (T === "variance") body.append(varianceView(r));
  else {
    const viz = h("div", {});
    renderChart(viz, r.chart);
    body.append(viz);
    if (r.columns && r.rows && r.rows.length && !(r.chart && r.chart.type === "kpi" && r.rows.length === 1 && r.columns.length <= 2)) {
      body.append(h("div", { class: "row", style: { marginTop: "10px", justifyContent: "space-between" } }, h("span", { class: "muted", style: { fontSize: "12.5px" } }, "Bảng kết quả"),
        h("button", { class: "btn sm", onclick: () => downloadCSV(r.columns, r.rows) }, "⬇ CSV")), dataTable(r.columns, r.rows, r.columns_meta, r.chart && r.chart.highlights));
    }
  }
  body.append(detailTabs(r), feedbackRow(r));
  return card;
}

function varianceView(r) {
  const v = r.variance, f = v.format;
  const wrap = h("div", {});
  const t = v.total;
  wrap.append(h("div", { class: "kpis", style: { marginTop: "12px" } },
    h("div", { class: "kpi" }, h("div", { class: "l" }, v.metric_label + " · " + v.period_now.label), h("div", { class: "v" }, fmtCompact(t.now, f))),
    h("div", { class: "kpi" }, h("div", { class: "l" }, v.metric_label + " · " + v.period_prev.label), h("div", { class: "v" }, fmtCompact(t.prev, f))),
    h("div", { class: "kpi" }, h("div", { class: "l" }, "Chênh lệch"), h("div", { class: "v" }, (t.delta >= 0 ? "+" : "") + fmtCompact(t.delta, f)), h("div", { class: "d" }, t.pct == null ? "" : (t.pct >= 0 ? "▲ +" : "▼ ") + nf1.format(t.pct) + "%")),
    v.significance ? h("div", { class: "kpi" }, h("div", { class: "l" }, "Kiểm định bất thường"), h("div", { class: "v" }, "z = " + nfa.format(v.significance.z)), h("div", { class: "d" }, "p = " + v.significance.p_value + (v.significance.significant ? " · bất thường" : " · bình thường"))) : null));
  const causes = (v.root_causes || []).filter(c => c.kind !== "significance");
  if (causes.length) wrap.append(h("div", { style: { marginTop: "12px" } }, h("b", {}, "Kiểm tra nguyên nhân gốc (xác định bằng code)"),
    causes.map(c => h("div", { class: "finding " + (c.severity === "critical" ? "critical" : c.severity === "warning" ? "warning" : "info"), style: { marginTop: "8px" } },
      h("div", { class: "row" }, sevIcon(c.severity === "critical" ? "critical" : c.severity === "warning" ? "warning" : "info"), h("b", {}, c.title),
        h("span", { class: "badge" }, { data_quality: "Chất lượng dữ liệu", business: "Kinh doanh", calendar: "Lịch / mùa vụ", info: "Khác" }[c.kind] || c.kind)),
      h("div", { style: { marginTop: "4px" } }, c.message), c.action ? h("div", { class: "muted", style: { fontSize: "12.5px", marginTop: "4px" } }, "➜ " + c.action) : null,
      c.test ? h("div", { class: "muted mono", style: { fontSize: "12px", marginTop: "4px" } }, JSON.stringify(c.test)) : null))));
  const viz = h("div", {}); renderChart(viz, r.chart); wrap.append(viz);
  const mkTbl = items => h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, ["Thành phần", v.period_now.label, v.period_prev.label, "Chênh lệch", "%", "Đóng góp vào biến động"].map(x => h("th", {}, x)))),
    h("tbody", {}, items.map(i => h("tr", {}, h("td", {}, String(i.member)), h("td", { class: "num" }, fmtCell(i.now, f)), h("td", { class: "num" }, fmtCell(i.prev, f)),
      h("td", { class: "num" }, fmtCell(i.delta, f)), h("td", { class: "num" }, i.pct == null ? "—" : nf1.format(i.pct) + "%"), h("td", { class: "num" }, i.contribution_pct == null ? "—" : nf1.format(i.contribution_pct) + "%"))))));
  wrap.append(tabs([
    ...(v.drivers || []).map(d => ({ label: "Theo " + d.label.toLowerCase() + (d.dimension === v.best_dimension ? " ★" : ""), render: () => h("div", {}, h("div", { class: "muted", style: { fontSize: "12.5px", marginBottom: "6px" } }, `Thành phần dẫn đầu giải thích ${nf1.format(d.top_share * 100)}% biến động (tỷ trọng gốc ${nf1.format(d.base_share * 100)}%).`), mkTbl(d.items)) })),
    ...(v.drill || []).map(d => ({ label: "Drill: " + d.within + " × " + d.label.toLowerCase(), render: () => mkTbl(d.items) })),
    v.decomposition && { label: "Sản lượng × giá trị", render: () => { const x = v.decomposition; return h("div", { class: "kpis" },
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Số đơn (" + x.segment + ")"), h("div", { class: "v" }, nf0.format(x.orders_now)), h("div", { class: "d" }, "kỳ trước " + nf0.format(x.orders_prev) + " · " + nf1.format(x.orders_pct) + "%")),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Giá trị đơn TB"), h("div", { class: "v" }, fmtCompact(x.aov_now, "currency")), h("div", { class: "d" }, "kỳ trước " + fmtCompact(x.aov_prev, "currency") + " · " + nf1.format(x.aov_pct) + "%")),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Hiệu ứng số đơn"), h("div", { class: "v" }, fmtCompact(x.volume_effect, "currency"))),
      h("div", { class: "kpi" }, h("div", { class: "l" }, "Hiệu ứng giá trị đơn"), h("div", { class: "v" }, fmtCompact(x.value_effect, "currency")))); } },
    { label: `Truy vấn đã chạy (${(v.queries || []).length})`, render: () => h("div", {}, (v.queries || []).map(q => h("details", {}, h("summary", {}, q.purpose, " · ", h("span", { class: "muted" }, fmtBytes(q.bytes) + " · " + q.rows + " dòng · " + q.ms + "ms")), codeBlock(q.sql)))) },
  ]));
  return wrap;
}

// ------------------------------------------------------------------ chat
function appendBot(resp) {
  const msgs = $("#msgs"); if (!msgs) return;
  msgs.append(renderAnswer(resp));
  msgs.scrollTop = msgs.scrollHeight;
  if (resp.conversation_id && (!S.conv || S.conv !== resp.conversation_id)) { S.conv = resp.conversation_id; }
  loadConvs();
}
async function sendMessage(text, clarification = null) {
  const msgs = $("#msgs"); if (!msgs) return;
  if (!text && !clarification) return;
  $(".empty", msgs) && $(".empty", msgs).remove();
  msgs.append(h("div", { class: "msg-user" }, text));
  const steps = ["Nạp ngữ cảnh hội thoại", "Lập kế hoạch truy vấn", "Sinh SQL theo semantic layer", "Kiểm duyệt cú pháp · RBAC · RLS", "Ước lượng chi phí quét", "Chạy trên kho dữ liệu", "Diễn giải & kiểm chứng số liệu"];
  const th = h("div", { class: "card msg-bot" }, h("div", { class: "thinking" }, h("div", { class: "spinner" }), h("span", { id: "think-step" }, steps[0] + "…")));
  msgs.append(th); msgs.scrollTop = msgs.scrollHeight;
  let k = 0; const timer = setInterval(() => { k = Math.min(k + 1, steps.length - 1); const el = $("#think-step"); if (el) el.textContent = steps[k] + "…"; }, 450);
  try {
    if (!S.conv) { const c = await api("/api/conversations", { body: {} }); S.conv = c.id; }
    const resp = await api("/api/chat", { body: { message: text, conversation_id: S.conv, clarification } });
    th.remove(); appendBot(resp);
  } catch (e) { th.replaceChildren(h("div", { class: "answer" }, h("div", { class: "callout err" }, e.message))); }
  finally { clearInterval(timer); }
}
async function loadConvs() {
  try { S.convs = await api("/api/conversations"); } catch (e) { return; }
  const list = $("#convlist"); if (!list) return;
  list.replaceChildren(...S.convs.map(c => h("div", { class: "conv" + (c.id === S.conv ? " active" : ""), onclick: () => openConv(c.id) },
    h("span", { class: "t", title: c.title }, c.title),
    h("button", { class: "x", title: "Xóa", onclick: async e => { e.stopPropagation(); await api("/api/conversations/" + c.id, { method: "DELETE" }); if (S.conv === c.id) { S.conv = null; renderView(); } else loadConvs(); } }, "✕"))));
}
async function openConv(id) {
  S.conv = id;
  const c = await api("/api/conversations/" + id);
  const msgs = $("#msgs"); msgs.replaceChildren();
  for (const m of c.messages) {
    if (m.role === "user") { if (m.content.text) msgs.append(h("div", { class: "msg-user" }, m.content.text)); }
    else msgs.append(renderAnswer(m.content));
  }
  if (!c.messages.length) msgs.append(emptyState());
  msgs.scrollTop = msgs.scrollHeight;
  loadConvs();
}
function emptyState() {
  const u = S.user;
  return h("div", { class: "empty" }, h("h3", {}, "Hỏi dữ liệu bằng tiếng Việt"),
    h("div", {}, "Agent lập kế hoạch truy vấn, sinh SQL đúng định nghĩa chỉ số, kiểm duyệt phân quyền & chi phí, chạy trên kho và trả về bảng + biểu đồ + diễn giải đã kiểm chứng."),
    u.rls && Object.keys(u.rls).length ? h("div", { class: "callout info", style: { justifyContent: "center", marginTop: "10px" } }, "🔒 Tài khoản của bạn chỉ thấy dữ liệu: " + JSON.stringify(u.rls)) : null,
    h("div", { class: "chips" }, (S.cfg.samples || []).slice(0, 8).map(s => h("button", { class: "chip", onclick: () => sendMessage(s) }, s))));
}
function viewChat() {
  const ta = h("textarea", { rows: 2, placeholder: "Ví dụ: Doanh thu thuần theo miền tháng 10/2026 so với cùng kỳ…", onkeydown: e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const v = ta.value.trim(); if (v) { ta.value = ""; sendMessage(v); } }
  } });
  const el = h("div", { class: "chat" },
    h("div", { class: "convs" },
      h("div", { style: { padding: "10px" } }, h("button", { class: "btn primary", style: { width: "100%" }, onclick: () => { S.conv = null; renderView(); } }, "＋ Hội thoại mới")),
      h("div", { class: "list", id: "convlist" }),
      h("div", { class: "samples" }, h("div", { class: "muted", style: { fontSize: "11px", textTransform: "uppercase", letterSpacing: ".06em", marginBottom: "4px" } }, "Câu hỏi mẫu"),
        (S.cfg.samples || []).map(s => h("button", { class: "s", onclick: () => sendMessage(s) }, s)))),
    h("div", { class: "thread" }, h("div", { class: "msgs", id: "msgs" }, emptyState()),
      h("div", { class: "composer" }, h("div", { class: "box" }, ta, h("button", { class: "btn primary", onclick: () => { const v = ta.value.trim(); if (v) { ta.value = ""; sendMessage(v); } } }, "Gửi")),
        h("div", { class: "muted", style: { fontSize: "12px", marginTop: "4px" } }, "Enter để gửi · Shift+Enter xuống dòng · Agent ghi nhớ ngữ cảnh: có thể hỏi tiếp “còn Miền Nam thì sao?”, “so với cùng kỳ”, “theo kênh thì sao”"))));
  setTimeout(() => { loadConvs(); if (S.conv) openConv(S.conv); ta.focus(); }, 0);
  return el;
}

// ------------------------------------------------------------------ SQL console
function viewConsole() {
  const ta = h("textarea", { rows: 12, class: "mono" }, store.get("console_sql") || "SELECT cn.ten_cn, SUM(f.thanh_tien) AS doanh_thu\nFROM dw.fct_ct_dh f\nJOIN dw.dim_cn cn ON cn.ma_cn = f.ma_cn\nWHERE YEAR(f.ngay_dh) = 2026\nGROUP BY 1\nORDER BY 2 DESC\nLIMIT 10");
  const out = h("div", { style: { marginTop: "14px" } });
  const save = () => store.set("console_sql", ta.value);
  const check = async () => {
    save(); out.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), "Đang kiểm tra…"));
    try {
      const v = await api("/api/sql/validate", { body: { sql: ta.value } });
      const card = h("div", { class: "card" }, h("div", { class: "hd" }, h("b", {}, "Kết quả dry-run"), h("span", { class: "badge " + (v.ok ? "ok" : "err") }, v.ok ? "Hợp lệ" : "Không hợp lệ")), h("div", { class: "bd" },
        h("ul", { class: "checks" }, (v.checks || []).map(c => h("li", {}, c.passed ? "✅" : "❌", h("b", {}, c.name), h("span", { class: "muted" }, c.detail || "")))),
        (v.errors || []).map(e => h("div", { class: "callout err" }, h("code", {}, e.code), " ", e.message, e.hint ? h("div", { class: "muted" }, e.hint) : null)),
        (v.lint || []).map(l => h("div", { class: "callout " + (l.severity === "info" ? "info" : "warn") }, h("code", {}, l.code), " ", l.message, l.hint ? h("span", { class: "muted" }, " — " + l.hint) : null)),
        v.cost ? h("div", { style: { marginTop: "10px" } }, costView(v.cost)) : null,
        v.sql_exec ? h("details", { style: { marginTop: "8px" } }, h("summary", {}, "SQL thực thi (sau RLS/LIMIT)"), codeBlock(v.sql_exec)) : null));
      out.replaceChildren(card);
    } catch (e) { out.replaceChildren(h("div", { class: "callout err" }, e.message)); }
  };
  const run = async () => {
    save(); out.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), "Đang kiểm duyệt & chạy…"));
    try {
      const r = await api("/api/sql/run", { body: { sql: ta.value, conversation_id: S.consoleConv || null } });
      S.consoleConv = r.conversation_id;
      out.replaceChildren(h("div", { id: "msgs", style: { display: "flex", flexDirection: "column", gap: "12px" } }, renderAnswer(r)));
    } catch (e) { out.replaceChildren(h("div", { class: "callout err" }, e.message)); }
  };
  return h("div", { class: "page" }, h("h2", {}, "SQL Console"),
    h("p", { class: "sub" }, "Mọi SQL đều qua cùng pipeline: kiểm tra cú pháp → chỉ đọc → hàm an toàn → RBAC/RLS → dry-run → lint → ước lượng chi phí → (duyệt nếu bảng lớn) → chạy. Lỗi sẽ được agent tự sửa (thử câu mẫu có cột sai ‘thanh_tien’)."),
    ta, h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "btn", onclick: check }, "🔍 Kiểm tra (dry-run)"), h("button", { class: "btn primary", onclick: run }, "▶ Chạy"),
      h("span", { class: "muted", style: { fontSize: "12.5px" } }, "Thử: DROP TABLE dw.fct_dh · SELECT * FROM dw.dim_kh · SELECT * FROM read_csv('x.csv') · SUM(d.tong_tt) JOIN fct_ct_dh")), out);
}

// ------------------------------------------------------------------ schema & chỉ số
function viewSchema() {
  const page = h("div", { class: "page" }, h("h2", {}, "Schema & Semantic layer"), h("p", { class: "sub" }, "Danh mục dữ liệu (data catalog) và định nghĩa chỉ số thống nhất. Cột 🔒 bị hạn chế theo vai trò."));
  const q = h("input", { type: "text", placeholder: "Lọc bảng/cột/chỉ số…", style: { width: "320px" } });
  page.append(q);
  const content = h("div", { style: { marginTop: "12px" } });
  page.append(content);
  api("/api/schema").then(data => {
    const draw = () => {
      const f = q.value.toLowerCase();
      const match = s => !f || String(s || "").toLowerCase().includes(f);
      content.replaceChildren(
        h("div", { class: "card" }, h("div", { class: "hd" }, h("b", {}, "Chỉ số (metrics) — " + data.metrics.length)), h("div", { class: "bd" }, h("div", { class: "tbl-wrap", style: { maxHeight: "360px" } }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Chỉ số", "Chủ sở hữu", "Định nghĩa", "Công thức", "Điều kiện"].map(x => h("th", {}, x)))),
          h("tbody", {}, data.metrics.filter(m => match(m.label + m.name + m.description)).map(m => h("tr", {}, h("td", {}, h("b", {}, m.label), h("div", { class: "muted mono" }, m.name)), h("td", {}, m.owner), h("td", { style: { maxWidth: "380px" } }, m.description), h("td", { class: "mono" }, m.sql), h("td", { class: "mono" }, m.filter || "—")))))))),
        h("div", { style: { height: "14px" } }),
        ...data.tables.filter(t => match(t.table + t.description + t.columns.map(c => c.name + c.description).join(" "))).map(t => h("details", { class: "card", style: { marginBottom: "8px" } },
          h("summary", { style: { padding: "10px 14px" } }, h("b", { class: "mono" }, t.table), " ", h("span", { class: "muted" }, "· " + nf0.format(t.rows) + " dòng · " + fmtBytes(t.bytes) + (t.partition_column ? " · phân vùng " + t.partition_column : "")), h("div", { class: "sec", style: { fontSize: "13px" } }, t.description),
            S.user.can_manage_schema ? h("button", { class: "btn sm", style: { marginTop: "4px" }, onclick: e => { e.preventDefault(); editCatalog(t.table, t.description, t.synonyms); } }, "✎ Sửa mô tả bảng") : null),
          h("div", { class: "bd" }, h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Cột", "Kiểu", "Mô tả", "Giá trị mẫu", ""].map(x => h("th", {}, x)))),
            h("tbody", {}, t.columns.map(c => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", { class: "muted" }, c.type),
              h("td", {}, c.description || h("span", { class: "muted" }, "(chưa có mô tả)"), c.sensitivity ? h("span", { class: "badge " + (c.restricted ? "err" : "warn"), style: { marginLeft: "6px" } }, (c.restricted ? "🔒 " : "") + c.sensitivity) : null),
              h("td", { style: { fontSize: "12px", maxWidth: "320px" } }, c.sample_values ? c.sample_values.slice(0, 8).join(", ") : (c.range ? c.range.join(" → ") : "")),
              h("td", {}, S.user.can_manage_schema ? h("button", { class: "btn sm", onclick: () => editCatalog(t.table + "." + c.name, c.description, []) }, "✎") : null)))))))))
      );
    };
    q.oninput = draw; draw();
  }).catch(e => content.replaceChildren(h("div", { class: "callout err" }, e.message)));
  return page;
}
function editCatalog(key, desc, syn) {
  const d = h("textarea", { rows: 3 }, desc || "");
  const s = h("input", { type: "text", style: { width: "100%" }, value: (syn || []).join(", ") });
  modal("Sửa catalog: " + key, h("div", {}, h("div", { class: "muted" }, "Mô tả nghiệp vụ (dùng cho tìm kiếm schema & prompt LLM)"), d, h("div", { class: "muted", style: { marginTop: "8px" } }, "Từ đồng nghĩa (phân cách bằng dấu phẩy)"), s),
    [close => h("button", { class: "btn primary", onclick: async () => { await api("/api/admin/catalog", { method: "PUT", body: { key, description: d.value, synonyms: s.value.split(",").map(x => x.trim()).filter(Boolean) } }); close(); toast("Đã cập nhật catalog & chỉ mục tìm kiếm"); renderView(); } }, "Lưu")]);
}

// ------------------------------------------------------------------ nhật ký
function viewLogs() {
  const page = h("div", { class: "page" }, h("h2", {}, "Nhật ký truy vấn"), h("p", { class: "sub" }, S.user.role === "admin" ? "Toàn bộ truy vấn của mọi người dùng (audit log)." : "Truy vấn của bạn."));
  const box = h("div", {}); page.append(box);
  api("/api/logs").then(d => {
    const st = d.stats;
    if (st) box.append(h("div", { class: "kpis", style: { marginBottom: "12px" } }, [["Tổng truy vấn", nf0.format(st.n || 0)], ["Thành công", nf0.format(st.ok || 0)], ["Lỗi", nf0.format(st.err || 0)], ["Bị chặn", nf0.format(st.blocked || 0)],
      ["Chờ/từ chối duyệt", nf0.format((st.pending || 0) + (st.rejected || 0))], ["Lần tự sửa", nf0.format(st.retries || 0)], ["Độ trễ TB", nf0.format(st.lat || 0) + " ms"], ["Tổng bytes quét", fmtBytes(st.bytes || 0)]].map(([l, v]) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v)))));
    box.append(h("div", { class: "row", style: { marginBottom: "8px" } }, h("button", { class: "btn sm", onclick: async () => {
      const res = await fetch("/api/logs.csv", { headers: { Authorization: "Bearer " + S.token } }); const b = await res.blob();
      const a = h("a", { href: URL.createObjectURL(b), download: "query_log.csv" }); document.body.append(a); a.click(); a.remove();
    } }, "⬇ Xuất CSV"), d.feedback ? h("span", { class: "muted" }, `Phản hồi: ${d.feedback.up || 0} 👍 · ${d.feedback.down || 0} 👎`) : null));
    const badge = s => h("span", { class: "badge " + ({ success: "ok", error: "err", blocked: "err", pending_approval: "warn", rejected: "warn" }[s] || "") }, s);
    box.append(h("div", { class: "tbl-wrap", style: { maxHeight: "70vh" } }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["Thời gian", "Người dùng", "Câu hỏi", "Trạng thái", "Nguồn SQL", "Bytes ước tính", "Dòng", "Độ trễ", "Sửa", "Chế độ", "Duyệt"].map(x => h("th", {}, x)))),
      h("tbody", {}, d.logs.map(l => h("tr", { style: { cursor: "pointer" }, onclick: () => modal("Truy vấn " + l.run_id, h("div", {}, h("p", {}, h("b", {}, l.question)), l.error ? h("div", { class: "callout err" }, l.error) : null, l.sql ? codeBlock(l.sql) : h("div", { class: "muted" }, "(không có SQL)"))) },
        h("td", { style: { whiteSpace: "nowrap" } }, fmtTime(l.ts)), h("td", {}, l.username, h("div", { class: "muted" }, l.role)), h("td", { style: { maxWidth: "340px" } }, l.question), h("td", {}, badge(l.status)),
        h("td", {}, l.source), h("td", { class: "num" }, l.est_bytes ? fmtBytes(l.est_bytes) : "—"), h("td", { class: "num" }, l.rows ?? "—"), h("td", { class: "num" }, nf0.format(l.latency_ms || 0) + " ms"),
        h("td", { class: "num" }, l.retries || 0), h("td", {}, l.llm_mode), h("td", {}, l.approved_by || "")))))));
  }).catch(e => box.append(h("div", { class: "callout err" }, e.message)));
  return page;
}

// ------------------------------------------------------------------ chất lượng dữ liệu
function viewDQ() {
  const page = h("div", { class: "page" }, h("h2", {}, "Chất lượng & trôi lệch dữ liệu"),
    h("p", { class: "sub" }, "Kiểm định xác định (thống kê + quy tắc) trên kho: schema drift theo data contract, mã chưa chuẩn hóa, trùng lặp, khóa mồ côi, trôi lệch đơn vị (KS test, change-point), outlier, bất thường sản lượng, PSI/chi-square. Quy tắc sửa được kiểm duyệt AST rồi build lại model."));
  const box = h("div", {}); page.append(box);
  const draw = rep => {
    box.replaceChildren(...[
      h("div", { class: "row", style: { marginBottom: "12px" } },
        h("span", { class: "badge err" }, rep.summary.critical + " nghiêm trọng"), h("span", { class: "badge warn" }, rep.summary.warning + " cảnh báo"), h("span", { class: "badge info" }, rep.summary.info + " thông tin"),
        h("span", { class: "muted" }, "Cập nhật " + rep.generated_at + " · " + rep.elapsed_s + "s"), h("span", { class: "grow" }),
        h("button", { class: "btn", onclick: async e => { e.target.disabled = true; e.target.textContent = "Đang chạy…"; draw(await api("/api/dq/run", { body: {} })); } }, "↻ Chạy lại kiểm định")),
      (rep.active_fixes || []).length ? h("div", { class: "card", style: { marginBottom: "12px" } }, h("div", { class: "hd" }, h("b", {}, "Quy tắc sửa đang áp dụng")), h("div", { class: "bd" },
        rep.active_fixes.map(f => h("div", { class: "row", style: { marginBottom: "6px" } }, h("span", { class: "badge ok" }, "đang bật"), h("b", {}, f.title || f.id), h("span", { class: "muted" }, "bởi " + f.applied_by + " lúc " + f.applied_at), h("span", { class: "grow" }),
          h("button", { class: "btn sm danger", onclick: async e => { if (!confirm("Hoàn tác quy tắc và build lại model?")) return; e.target.disabled = true; e.target.textContent = "Đang build lại…"; const r = await api("/api/dq/revert/" + f.id, { body: {} }); toast("Đã hoàn tác"); draw(r.report); } }, "Hoàn tác"))))) : null,
      ...rep.findings.map(f => findingCard(f, draw))].filter(Boolean));
  };
  api("/api/dq").then(draw).catch(e => box.append(h("div", { class: "callout err" }, e.message)));
  return page;
}
function findingCard(f, redraw) {
  const cls = f.status === "open" ? f.severity : f.status;
  const ev = Array.isArray(f.evidence) && f.evidence.length ? f.evidence : null;
  return h("div", { class: "finding " + cls },
    h("div", { class: "row" }, sevIcon(f.severity), h("b", {}, f.title), h("span", { class: "badge" }, f.check), f.table ? h("span", { class: "badge mono" }, f.table) : null,
      h("span", { class: "badge " + ({ open: "warn", handled: "ok", fixed: "ok", ok: "ok" }[f.status] || "") }, { open: "đang mở", handled: "đã xử lý ở staging", fixed: "đã sửa", ok: "ổn" }[f.status] || f.status)),
    h("div", { style: { marginTop: "6px" } }, f.message),
    f.recommendation ? h("div", { class: "muted", style: { marginTop: "4px", fontSize: "13px" } }, "➜ " + f.recommendation) : null,
    f.test ? h("div", { class: "muted mono", style: { marginTop: "4px", fontSize: "12px" } }, "Kiểm định: " + JSON.stringify(f.test)) : null,
    ev ? h("details", { style: { marginTop: "6px" } }, h("summary", {}, "Bằng chứng (" + ev.length + ")"), dataTable(Object.keys(ev[0]), ev.slice(0, 50).map(o => Object.values(o).map(v => typeof v === "object" && v !== null ? JSON.stringify(v) : v)), [])) : null,
    f.fix && f.status === "open" ? h("div", { class: "callout info", style: { alignItems: "center" } }, h("div", { class: "grow" }, h("b", {}, "Quy tắc sửa đề xuất: "), f.fix.description),
      h("button", { class: "btn primary sm", onclick: () => confirmFix(f, redraw) }, "Áp dụng…")) : null);
}
function confirmFix(f, redraw) {
  const fx = f.fix;
  const body = h("div", {}, h("p", {}, fx.description),
    fx.slots ? h("div", {}, h("div", { class: "muted" }, "Biểu thức sẽ cài vào model " + fx.model + " (hook {{ rule() }}):"), Object.entries(fx.slots).map(([k, v]) => h("div", { style: { marginTop: "6px" } }, h("b", { class: "mono" }, k), codeBlock(v)))) : null,
    fx.seed_rows ? h("div", {}, h("div", { class: "muted" }, "Dòng mapping sẽ thêm:"), codeBlock(JSON.stringify(fx.seed_rows, null, 2))) : null,
    h("div", { class: "callout warn" }, "Quy tắc được kiểm tra cú pháp/AST (không truy vấn con, chỉ bí danh u/h/sp, không hàm bị cấm), sau đó build lại toàn bộ model raw → stg → dw và chạy lại kiểm định."));
  const res = h("div", {});
  body.append(res);
  modal("Áp dụng quy tắc sửa dữ liệu", body, [close => h("button", { class: "btn primary", onclick: async e => {
    e.target.disabled = true; res.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), "Đang kiểm duyệt quy tắc & build lại kho…"));
    try {
      const r = await api("/api/dq/fix/" + f.id, { body: {} });
      if (!r.ok) { res.replaceChildren(h("div", { class: "callout err" }, (r.errors || []).join("; "))); return; }
      const b = r.impact.before.rows, a = r.impact.after.rows;
      res.replaceChildren(h("div", { class: "callout ok" }, `✓ Đã build lại ${r.build.models.length} model trong ${r.build.seconds}s.`),
        h("div", { class: "muted", style: { marginTop: "6px" } }, "Tác động lên " + r.impact.before.metric + ":"),
        dataTable(["Tháng", "Trước khi sửa", "Sau khi sửa"], b.map((x, i) => [x.thang, x.value, a[i] ? a[i].value : null]), [{ name: "Trước khi sửa", format: "currency" }, { name: "Sau khi sửa", format: "currency" }]));
      redraw(r.report);
    } catch (err) { res.replaceChildren(h("div", { class: "callout err" }, err.message)); }
  } }, "Xác nhận áp dụng")]);
}

// ------------------------------------------------------------------ lineage
function viewLineage() {
  const page = h("div", { class: "page" }, h("h2", {}, "Lineage dữ liệu"), h("p", { class: "sub" }, "Dòng chảy dữ liệu raw/ref → staging → dw, tính bằng phân tích AST (sqlglot) trên SQL của các model. Chọn một bảng để xem cột; chọn cột để truy ngược tới cột nguồn."));
  const wrap = h("div", {});
  const canvas = h("div", { class: "lineage", style: { height: "560px" } });
  const side = h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "hd" }, h("b", {}, "Chi tiết")), h("div", { class: "bd", id: "lin-side" }, h("div", { class: "muted" }, "Chọn một bảng ở sơ đồ.")));
  wrap.append(canvas, side);
  page.append(wrap);
  api("/api/lineage").then(data => {
    const g = data.graph;
    const layers = { raw: 0, ref: 0, stg: 1, dw: 2 };
    const cols = [[], [], []];
    g.nodes.forEach(n => cols[layers[n.layer] ?? 0].push(n));
    cols.forEach(c => c.sort((a, b) => a.id.localeCompare(b.id)));
    const W = 230, X0 = 14, Y0 = 30, DY = 30;
    const pos = {};
    cols.forEach((c, i) => c.forEach((n, j) => (pos[n.id] = { x: X0 + i * (W + 70), y: Y0 + j * DY })));
    const height = Math.max(...cols.map(c => c.length)) * DY + Y0 + 10;
    const width = X0 + 3 * (W + 70);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", width); svg.setAttribute("height", height); svg.style.position = "absolute"; svg.style.left = 0; svg.style.top = 0;
    const paths = [];
    g.edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to]; if (!a || !b) return;
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const x1 = a.x + 200, y1 = a.y + 12, x2 = b.x, y2 = b.y + 12, mx = (x1 + x2) / 2;
      p.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      p.setAttribute("fill", "none"); p.setAttribute("stroke", cssVar("--border")); p.setAttribute("stroke-width", "1.5");
      svg.append(p); paths.push({ e, p });
    });
    const inner = h("div", { style: { position: "relative", width: width + "px", height: height + "px" } });
    inner.append(svg);
    ["Raw / Ref (nguồn)", "Staging (làm sạch)", "DW (mart)"].forEach((t, i) => inner.append(h("div", { class: "lin-head", style: { left: X0 + i * (W + 70) + "px" } }, t)));
    const nodes = {};
    g.nodes.forEach(n => {
      const el = h("div", { class: "lin-node", style: { left: pos[n.id].x + "px", top: pos[n.id].y + "px" }, title: n.desc || n.id, onclick: () => select(n.id) }, n.id, n.rows != null ? h("span", { class: "muted" }, " · " + nf0.format(n.rows)) : null);
      nodes[n.id] = el; inner.append(el);
    });
    canvas.append(inner);
    const up = id => { const s = new Set([id]); let ch = true; while (ch) { ch = false; g.edges.forEach(e => { if (s.has(e.to) && !s.has(e.from)) { s.add(e.from); ch = true; } }); } return s; };
    const down = id => { const s = new Set([id]); let ch = true; while (ch) { ch = false; g.edges.forEach(e => { if (s.has(e.from) && !s.has(e.to)) { s.add(e.to); ch = true; } }); } return s; };
    function select(id) {
      const rel = new Set([...up(id), ...down(id)]);
      Object.entries(nodes).forEach(([k, el]) => { el.classList.toggle("sel", k === id); el.classList.toggle("dim", !rel.has(k)); });
      paths.forEach(({ e, p }) => { const on = rel.has(e.from) && rel.has(e.to); p.setAttribute("stroke", on ? cssVar("--accent") : cssVar("--border")); p.setAttribute("stroke-width", on ? "2" : "1.2"); });
      const m = data.models[id];
      const sideEl = $("#lin-side");
      sideEl.replaceChildren(h("b", { class: "mono" }, id), h("div", { class: "sec" }, (m && m.desc) || (g.nodes.find(n => n.id === id) || {}).desc || ""),
        m ? h("div", {}, h("div", { class: "muted", style: { marginTop: "6px" } }, "Nguồn trực tiếp: " + m.sources.join(", ")),
          h("div", { class: "tbl-wrap", style: { marginTop: "8px", maxHeight: "460px" } }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Cột"), h("th", {}, "Từ cột"))),
            h("tbody", {}, Object.entries(m.columns).map(([c, srcs]) => h("tr", { style: { cursor: "pointer" }, onclick: () => colTrace(id + "." + c) }, h("td", { class: "mono" }, c), h("td", { class: "mono", style: { fontSize: "11.5px" } }, srcs.join(", ") || "(hằng số / biểu thức)"))))))) : h("div", { class: "muted", style: { marginTop: "6px" } }, "Bảng nguồn (không có model sinh ra)."));
    }
    async function colTrace(col) {
      const r = await api("/api/lineage/column?col=" + encodeURIComponent(col));
      const tree = n => h("li", {}, h("code", {}, n.column), n.sources.length ? h("ul", {}, n.sources.map(tree)) : null);
      modal("Truy ngược cột " + col, h("div", {}, h("div", {}, h("b", {}, "Cột nguồn gốc (raw): "), r.raw_sources.join(", ") || "—"), h("ul", { style: { marginTop: "8px" } }, tree(r.upstream)),
        r.downstream.length ? h("div", {}, h("b", {}, "Được dùng bởi: "), r.downstream.join(", ")) : null));
    }
    select("dw.fct_ct_dh");
  }).catch(e => page.append(h("div", { class: "callout err" }, e.message)));
  return page;
}

// ------------------------------------------------------------------ eval
function viewEval() {
  const page = h("div", { class: "page" }, h("h2", {}, "Đánh giá độ chính xác (Execution Accuracy)"),
    h("p", { class: "sub" }, "Bộ câu hỏi mẫu kiểu Spider/BIRD (eval/questions.json) với SQL chuẩn. So khớp kết quả thực thi theo chuẩn Spider 2.0 (mọi cột gold phải có trong kết quả dự đoán). Tối ưu few-shot: học từ lỗi trên train, đo trên dev."));
  const split = h("select", {}, ["all", "train", "dev"].map(s => h("option", { value: s }, s)));
  const mode = h("select", {}, h("option", { value: "" }, "theo cài đặt"), h("option", { value: "offline" }, "offline"), h("option", { value: "claude" }, "claude"));
  const out = h("div", { style: { marginTop: "12px" } });
  const hist = h("div", {});
  const fsBox = h("div", {});
  const runBtn = h("button", { class: "btn primary", onclick: async () => {
    runBtn.disabled = true; out.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), "Đang chạy eval… (có thể mất ~1 phút)"));
    try { const r = await api("/api/eval/run", { body: { split: split.value, mode: mode.value || null } }); showRun(r.summary, r.details); loadHist(); } catch (e) { out.replaceChildren(h("div", { class: "callout err" }, e.message)); }
    runBtn.disabled = false;
  } }, "▶ Chạy đánh giá");
  const optBtn = h("button", { class: "btn", onclick: async () => {
    optBtn.disabled = true; out.replaceChildren(h("div", { class: "thinking" }, h("div", { class: "spinner" }), "Đang tối ưu: dev (trước) → train (thu lỗi) → thêm few-shot → dev (sau)…"));
    try {
      const r = await api("/api/eval/optimize", { body: { mode: mode.value || null } });
      out.replaceChildren(h("div", { class: "card" }, h("div", { class: "hd" }, h("b", {}, "Kết quả tự tối ưu few-shot")), h("div", { class: "bd" },
        h("div", { class: "kpis" }, h("div", { class: "kpi" }, h("div", { class: "l" }, "Dev trước tối ưu"), h("div", { class: "v" }, r.before.execution_accuracy + "%")),
          h("div", { class: "kpi" }, h("div", { class: "l" }, "Dev sau tối ưu"), h("div", { class: "v" }, r.after.execution_accuracy + "%"), h("div", { class: "d" }, (r.after.execution_accuracy - r.before.execution_accuracy >= 0 ? "+" : "") + nf1.format(r.after.execution_accuracy - r.before.execution_accuracy) + " điểm")),
          h("div", { class: "kpi" }, h("div", { class: "l" }, "Train"), h("div", { class: "v" }, r.train.execution_accuracy + "%")), h("div", { class: "kpi" }, h("div", { class: "l" }, "Few-shot thêm mới"), h("div", { class: "v" }, r.added.length))),
        r.added.length ? h("ul", {}, r.added.map(q => h("li", {}, q))) : null)));
      loadHist(); loadFs();
    } catch (e) { out.replaceChildren(h("div", { class: "callout err" }, e.message)); }
    optBtn.disabled = false;
  } }, "🧠 Tối ưu few-shot (train → dev)");
  function showRun(sm, details) {
    const k = (l, v, d) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
    out.replaceChildren(h("div", { class: "card" }, h("div", { class: "hd" }, h("b", {}, sm.label), h("span", { class: "muted" }, sm.n + " câu · " + sm.elapsed_s + "s")), h("div", { class: "bd" },
      h("div", { class: "kpis" }, k("Execution accuracy", sm.execution_accuracy + "%"), k("Tỷ lệ ra kết quả", sm.valid_sql_rate + "%"), k("Diễn giải qua audit", sm.audit_pass_rate + "%"),
        k("Độ trễ TB", nf0.format(sm.avg_latency_ms) + " ms"), k("Tự sửa TB", sm.avg_retries), k("Bytes quét", fmtBytes(sm.total_bytes))),
      h("div", { class: "grid2", style: { marginTop: "12px" } },
        dataTable(["Độ khó", "Số câu", "Accuracy (%)"], Object.entries(sm.by_difficulty).map(([a, b]) => [a, b.n, b.acc]), []),
        dataTable(["Nhóm", "Số câu", "Accuracy (%)"], Object.entries(sm.by_tag).map(([a, b]) => [a, b.n, b.acc]), [])),
      details ? h("div", { class: "tbl-wrap", style: { marginTop: "12px", maxHeight: "60vh" } }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, ["#", "Câu hỏi", "Độ khó", "Split", "Kết quả", "Lý do", "Nguồn", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, details.map(d => h("tr", {}, h("td", { class: "mono" }, d.id), h("td", { style: { maxWidth: "360px" } }, d.question), h("td", {}, d.difficulty), h("td", {}, d.split),
          h("td", {}, d.correct ? "✅" : "❌"), h("td", { style: { fontSize: "12.5px", maxWidth: "280px" } }, d.reason), h("td", {}, d.pred_source || d.pred_type),
          h("td", {}, h("button", { class: "btn sm", onclick: () => modal("Câu " + d.id, h("div", {}, h("p", {}, h("b", {}, d.question)), h("div", { class: "muted" }, "SQL dự đoán"), d.pred_sql ? codeBlock(d.pred_sql) : h("div", { class: "muted" }, "—"), h("div", { class: "muted", style: { marginTop: "8px" } }, "SQL chuẩn (gold)"), d.gold_sql ? codeBlock(d.gold_sql) : h("div", { class: "muted" }, "—"))) }, "SQL")))))) ) : null)));
  }
  async function loadHist() {
    const d = await api("/api/eval");
    hist.replaceChildren(h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "hd" }, h("b", {}, "Lịch sử đánh giá"), h("span", { class: "muted" }, d.questions.length + " câu hỏi trong bộ eval")), h("div", { class: "bd" },
      d.runs.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Thời gian", "Nhãn", "Số câu", "Accuracy", "Few-shot trong kho", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, d.runs.map(r => h("tr", {}, h("td", {}, fmtTime(r.ts)), h("td", {}, r.label), h("td", { class: "num" }, r.summary.n), h("td", { class: "num" }, r.summary.execution_accuracy + "%"), h("td", { class: "num" }, r.summary.fewshots_in_store),
          h("td", {}, h("button", { class: "btn sm", onclick: async () => { const x = await api("/api/eval/" + r.id); showRun(x.summary, x.details); window.scrollTo(0, 0); } }, "Xem"))))))) : h("div", { class: "muted" }, "Chưa có lượt nào."))));
  }
  async function loadFs() {
    const f = await api("/api/fewshots");
    fsBox.replaceChildren(h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "hd" }, h("b", { class: "grow" }, "Kho few-shot (" + f.length + ")"),
      h("button", { class: "btn sm danger", onclick: async () => { await api("/api/fewshots?source=optimizer", { method: "DELETE" }); loadFs(); } }, "Xóa few-shot do optimizer")), h("div", { class: "bd" },
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["#", "Câu hỏi", "Nguồn", "Lượt dùng", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, f.map(x => h("tr", {}, h("td", {}, x.id), h("td", {}, x.question, h("details", {}, h("summary", {}, "SQL"), codeBlock(x.sql))), h("td", {}, h("span", { class: "badge" }, x.source)), h("td", { class: "num" }, x.uses),
          h("td", {}, h("button", { class: "btn sm danger", onclick: async () => { await api("/api/fewshots/" + x.id, { method: "DELETE" }); loadFs(); } }, "Xóa"))))))))));
  }
  page.append(h("div", { class: "row" }, "Tập:", split, "Chế độ:", mode, runBtn, optBtn), out, hist, fsBox);
  loadHist(); loadFs();
  return page;
}

// ------------------------------------------------------------------ cài đặt
function viewSettings() {
  const page = h("div", { class: "page" }, h("h2", {}, "Cài đặt hệ thống"), h("p", { class: "sub" }, "Chế độ LLM, ngưỡng HITL, tự sửa lỗi, tái sử dụng few-shot."));
  api("/api/admin/settings").then(d => {
    const s = d.settings;
    const f = {
      llm_mode: h("select", {}, ["auto", "claude", "offline"].map(x => h("option", { value: x, selected: s.llm_mode === x }, x))),
      llm_model: h("input", { type: "text", value: s.llm_model }),
      llm_effort: h("select", {}, ["low", "medium", "high", "xhigh", "max"].map(x => h("option", { value: x, selected: s.llm_effort === x }, x))),
      always_review: h("input", { type: "checkbox", checked: s.always_review }),
      max_repair_attempts: h("input", { type: "number", value: s.max_repair_attempts, min: 0, max: 6 }),
      query_timeout_s: h("input", { type: "number", value: s.query_timeout_s, min: 1, max: 120 }),
      fewshot_k: h("input", { type: "number", value: s.fewshot_k, min: 0, max: 10 }),
      fewshot_reuse_threshold: h("input", { type: "number", value: s.fewshot_reuse_threshold, min: 0.5, max: 1, step: 0.01 }),
      anchor_date: h("input", { type: "text", value: s.anchor_date || "", placeholder: "trống = ngày dữ liệu mới nhất" }),
    };
    const lab = { llm_mode: "Chế độ LLM (auto = Claude nếu có API key)", llm_model: "Model Claude", llm_effort: "Effort (độ sâu suy luận)", always_review: "Luôn yêu cầu duyệt SQL trước khi chạy",
      max_repair_attempts: "Số lần tự sửa SQL tối đa", query_timeout_s: "Timeout truy vấn (giây)", fewshot_k: "Số few-shot đưa vào prompt", fewshot_reuse_threshold: "Ngưỡng tái sử dụng SQL đã xác nhận (Jaccard)", anchor_date: "Ngày neo cho 'tháng này' (YYYY-MM-DD)" };
    page.append(h("div", { class: "card" }, h("div", { class: "bd" },
      h("div", { class: "callout " + (d.credentials ? "ok" : "warn"), style: { marginTop: 0, marginBottom: "12px" } }, d.credentials ? "✓ Đã phát hiện thông tin xác thực Anthropic — có thể dùng Claude." : "Chưa có ANTHROPIC_API_KEY — hệ thống chạy bằng bộ hiểu tiếng Việt offline. Đặt biến môi trường ANTHROPIC_API_KEY rồi khởi động lại để bật Claude."),
      Object.entries(f).map(([k, el]) => h("div", { class: "row", style: { marginBottom: "10px" } }, h("label", { style: { width: "340px" } }, lab[k]), el)),
      h("button", { class: "btn primary", onclick: async () => {
        const values = {}; for (const [k, el] of Object.entries(f)) values[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : (el.value || (k === "anchor_date" ? null : el.value));
        await api("/api/admin/settings", { method: "PUT", body: { values } }); S.cfg = await api("/api/config"); renderTop(); toast("Đã lưu cài đặt");
      } }, "Lưu"))),
      h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "hd" }, h("b", {}, "Chính sách vai trò (config/policies.yml)")), h("div", { class: "bd" }, codeBlock(JSON.stringify(d.policies, null, 2)))));
  });
  return page;
}

// ------------------------------------------------------------------ khung ứng dụng
const VIEWS = [
  { id: "chat", label: "💬 Hỏi đáp dữ liệu", fn: viewChat },
  { id: "console", label: "⌨️ SQL Console", fn: viewConsole },
  { id: "schema", label: "📚 Schema & chỉ số", fn: viewSchema },
  { id: "logs", label: "🧾 Nhật ký truy vấn", fn: viewLogs },
  { id: "dq", label: "🩺 Chất lượng dữ liệu", fn: viewDQ, admin: true },
  { id: "lineage", label: "🔀 Lineage", fn: viewLineage, admin: true },
  { id: "eval", label: "🎯 Đánh giá (Eval)", fn: viewEval, admin: true },
  { id: "settings", label: "⚙️ Cài đặt", fn: viewSettings, admin: true },
];
function renderTop() {
  const t = $("#topbar"); if (!t) return;
  const u = S.user, c = S.cfg;
  const theme = document.documentElement.getAttribute("data-theme");
  t.replaceChildren(
    h("div", { class: "brand" }, "📊 DATA-01", h("small", { class: "hide-sm" }, "AI Agent Text-to-SQL · Self-Service Analytics")),
    h("span", { class: "spacer" }),
    h("span", { class: "badge " + (c.mode === "claude" ? "ok" : "info"), title: "Chế độ sinh kế hoạch truy vấn" }, c.mode === "claude" ? "🤖 Claude · " + c.model : "⚙️ Offline NLU (chưa có API key)"),
    h("span", { class: "badge hide-sm" }, "Dữ liệu đến " + c.anchor),
    h("span", { class: "badge", title: u.full_name }, "👤 " + u.username + " · " + (u.role === "admin" ? "Admin" : "Analyst") + (u.rls && Object.keys(u.rls).length ? " · RLS " + JSON.stringify(u.rls) : "")),
    h("button", { class: "btn sm", title: "Đổi giao diện sáng/tối", onclick: () => {
      const cur = document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const nx = cur === "dark" ? "light" : "dark"; document.documentElement.setAttribute("data-theme", nx); store.set("theme", nx); renderView();
    } }, theme === "dark" ? "☀️" : "🌙"),
    h("button", { class: "btn sm", onclick: logout }, "Đăng xuất"));
}
function renderNav() {
  const n = $("#nav"); if (!n) return;
  n.replaceChildren(...VIEWS.flatMap((v, i) => {
    if (v.admin && !S.user.can_manage_schema) return [];
    const sect = v.admin && !VIEWS.slice(0, i).some(x => x.admin) ? h("div", { class: "sect" }, "Quản trị (Admin)") : null;
    return [sect, h("button", { class: S.view === v.id ? "active" : "", onclick: () => { S.view = v.id; store.set("view", v.id); renderNav(); renderView(); } }, v.label)];
  }).filter(Boolean));
}
function renderView() {
  S.charts.forEach(c => { try { c.destroy(); } catch (e) { /* ignore */ } }); S.charts = [];
  const v = VIEWS.find(x => x.id === S.view && (!x.admin || S.user.can_manage_schema)) || VIEWS[0];
  S.view = v.id;
  $("#view").replaceChildren(v.fn());
  renderTop();
}
async function boot() {
  const th = store.get("theme"); if (th) document.documentElement.setAttribute("data-theme", th);
  S.token = store.get("token");
  if (!S.token) return renderLogin();
  try { S.user = await api("/api/me"); S.cfg = await api("/api/config"); } catch (e) { return renderLogin(); }
  S.view = store.get("view") || "chat";
  $("#app").replaceChildren(h("div", { class: "shell" }, h("div", { class: "topbar", id: "topbar" }), h("div", { class: "main" }, h("nav", { class: "nav", id: "nav" }), h("div", { class: "view", id: "view" }))));
  renderNav(); renderView();
}
function logout() { S.token = null; S.user = null; S.conv = null; store.set("token", null); renderLogin(); }
function renderLogin() {
  const u = h("input", { type: "text", placeholder: "Tên đăng nhập", value: "analyst", style: { width: "100%" } });
  const p = h("input", { type: "password", placeholder: "Mật khẩu", value: "analyst123", style: { width: "100%", marginTop: "8px" } });
  const err = h("div", {});
  const go = async (un, pw) => {
    try { const r = await api("/api/login", { body: { username: un, password: pw } }); S.token = r.token; store.set("token", r.token); boot(); }
    catch (e) { err.replaceChildren(h("div", { class: "callout err" }, e.message)); }
  };
  p.onkeydown = e => { if (e.key === "Enter") go(u.value, p.value); };
  const q = (un, pw, title, desc) => h("button", { class: "btn", onclick: () => go(un, pw) }, h("b", {}, title), h("span", {}, desc));
  $("#app").replaceChildren(h("div", { class: "login" }, h("div", { class: "card" }, h("div", { class: "bd" },
    h("h1", {}, "📊 DATA-01 · Trợ lý phân tích dữ liệu"),
    h("div", { class: "sec" }, "AI Agent Text-to-SQL cho kho dữ liệu bán lẻ (dữ liệu giả lập). Đăng nhập bằng tài khoản demo:"),
    h("div", { style: { marginTop: "14px" } }, u, p, h("button", { class: "btn primary", style: { width: "100%", marginTop: "10px" }, onclick: () => go(u.value, p.value) }, "Đăng nhập")), err,
    h("div", { class: "quick" },
      q("admin", "admin123", "Admin", "Quản trị schema, DQ, eval"),
      q("analyst", "analyst123", "Analyst", "Chỉ đọc · toàn quốc"),
      q("analyst_bac", "analyst123", "Analyst Miền Bắc", "Chỉ đọc · RLS vùng B"),
      q("analyst_nam", "analyst123", "Analyst Miền Nam", "Chỉ đọc · RLS vùng N"))))));
}
boot();
