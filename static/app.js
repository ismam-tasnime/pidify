const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  formats: {}, maxMb: 100, maxBatchFiles: 50, maxBatchMb: 300,
  items: [], mode: "convert",
  resultUrl: null, working: false,
};

const GROUPS = [
  ["Documents", ["doc", "docx", "odt", "rtf", "txt", "md", "html", "epub", "rst", "tex"]],
  ["Spreadsheets", ["xls", "xlsx", "ods", "csv", "tsv"]],
  ["Presentations", ["ppt", "pptx", "odp"]],
  ["PDF", ["pdf"]],
  ["Images", ["png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "ico", "heic"]],
];
const MODES = {
  convert: { tagline: "Convert documents, spreadsheets, slides, PDFs and images into other formats.", drop: "Drop your files here" },
  merge: { tagline: "Combine PDFs, documents and images into a single PDF, in the order you choose.", drop: "Drop the files to merge" },
  scan: { tagline: "Snap a photo of any page. We straighten it, remove shadows and fingers, and give you a clean PDF.", drop: "Drop photos of your pages" },
};

const IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "ico", "heic", "heif"];
const BROWSER_IMAGES = ["png", "jpg", "jpeg", "webp", "bmp", "gif", "ico"];
const KINDS = {
  pdf: ["pdf"],
  word: ["doc", "docx", "odt", "rtf", "txt", "md", "html", "htm", "epub", "rst", "tex", "docm", "dotx"],
  sheet: ["xls", "xlsx", "ods", "csv", "tsv", "xlsm"],
  slide: ["ppt", "pptx", "odp", "pps", "ppsx"],
  image: IMAGE_EXTS,
};

const extOf = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");
const kindOf = (ext) => Object.keys(KINDS).find((k) => KINDS[k].includes(ext)) || "";
const label = (fmt, src = "") => (["png", "jpg"].includes(fmt) && !IMAGE_EXTS.includes(src) ? `${fmt.toUpperCase()} (images)` : fmt.toUpperCase());
const humanSize = (b) => (b < 1024 ? `${b} B` : b < 1024 ** 2 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1024 ** 2).toFixed(1)} MB`);

const canMerge = (item) => !item.tooBig && (item.ext === "pdf" || item.targets.includes("pdf"));
const canScan = (item) => !item.tooBig && IMAGE_EXTS.includes(item.ext);
const eligible = (item) => (state.mode === "scan" ? canScan(item) : canMerge(item));

// ========================================================================== //
// Start-up
// ========================================================================== //

async function init() {
  try {
    const res = await fetch("/api/formats");
    const data = await res.json();
    state.formats = data.formats;
    state.maxMb = data.max_upload_mb;
    state.maxBatchFiles = data.max_merge_files ?? state.maxBatchFiles;
    state.maxBatchMb = data.max_merge_mb ?? state.maxBatchMb;
    $("#maxmb").textContent = state.maxMb;
    const missing = Object.entries(data.tools).filter(([, ok]) => !ok).map(([t]) => t);
    if (missing.length) showWarn(`Some tools are missing on the server (${missing.join(", ")}), so some conversions won't work.`);
    renderSupported();
  } catch {
    showWarn("Couldn't reach the conversion server. Is it running?");
  }
  let saved = "convert";
  try { saved = localStorage.getItem("pidify-mode") || "convert"; } catch {}
  setMode(saved);
}

function showWarn(msg) { const w = $("#warn"); w.textContent = msg; w.hidden = false; }

function renderSupported() {
  const rows = GROUPS.map(([g, exts]) => {
    const outs = new Set();
    exts.forEach((e) => (state.formats[e] || []).forEach((o) => outs.add(o)));
    return `<tr><td>${g}</td><td>${exts.join(", ")} → ${[...outs].join(", ")}</td></tr>`;
  });
  rows.push(`<tr><td>Scan to PDF</td><td>Photos (jpg, png, heic, webp…) → one cleaned-up PDF</td></tr>`);
  $("#supported").innerHTML = `<table>${rows.join("")}</table>`;
}

// ========================================================================== //
// Modes + theme
// ========================================================================== //

function setMode(mode) {
  if (!MODES[mode]) mode = "convert";
  state.mode = mode;
  document.body.classList.remove("mode-convert", "mode-merge", "mode-scan");
  document.body.classList.add(`mode-${mode}`);
  $$(".mode").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("#tagline").textContent = MODES[mode].tagline;
  $("#dropTitle").textContent = MODES[mode].drop;
  $("#picker").accept = mode === "scan" ? "image/*,.heic,.heif" : "";
  try { localStorage.setItem("pidify-mode", mode); } catch {}
  clearResult();
  state.items.forEach(renderIdleStatus);
  refreshPanel();
  if (mode === "scan") queuePreviews();
}

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("pidify-theme", root.dataset.theme); } catch {}
}

// ========================================================================== //
// File list
// ========================================================================== //

function addFiles(fileList) {
  for (const file of fileList) {
    const ext = extOf(file.name);
    const targets = state.formats[ext] || [];
    const li = $("#rowTpl").content.firstElementChild.cloneNode(true);
    const item = { file, ext, targets, li, url: null, busy: false, preview: null };
    item.tooBig = file.size > state.maxMb * 1024 ** 2;
    item.blocked = !targets.length || item.tooBig;
    item.beforeUrl = BROWSER_IMAGES.includes(ext) ? URL.createObjectURL(file) : null;

    const badge = $(".badge", li);
    badge.textContent = ext || "?";
    badge.dataset.kind = kindOf(ext);
    $(".name", li).textContent = file.name;
    $(".name", li).title = file.name;
    if (item.beforeUrl) $(".thumb.before", li).style.backgroundImage = `url("${item.beforeUrl}")`;

    const sel = $(".target", li);
    sel.innerHTML = targets.map((t) => `<option value="${t}">${label(t, ext)}</option>`).join("");
    sel.hidden = !targets.length;
    sel.disabled = $(".go", li).disabled = item.blocked;

    sel.addEventListener("change", () => resetResult(item));
    $(".go", li).addEventListener("click", () => convert(item));
    $(".rm", li).addEventListener("click", () => removeItem(item));
    $(".up", li).addEventListener("click", () => move(item, -1));
    $(".down", li).addEventListener("click", () => move(item, +1));
    $(".thumbs", li).addEventListener("click", () => openViewer(item));
    wireDrag(item);

    state.items.push(item);
    $("#list").append(li);
    renderIdleStatus(item);
  }
  clearResult();
  refreshPanel();
  if (state.mode === "scan") queuePreviews();
}

// The status line shown when nothing is running for this row.
function renderIdleStatus(item) {
  if (item.busy || item.url) return;
  const size = humanSize(item.file.size);
  item.li.classList.remove("skip");
  if (item.tooBig) return setStatus(item, `Too large (max ${state.maxMb} MB)`, "err");

  if (state.mode === "merge") {
    if (!canMerge(item)) {
      item.li.classList.add("skip");
      return setStatus(item, `.${item.ext || "?"} files can't be merged, so this file will be skipped`, "err");
    }
    return setStatus(item, item.ext === "pdf" ? size : `${size} · will be turned into PDF`);
  }
  if (state.mode === "scan") {
    if (!canScan(item)) {
      item.li.classList.add("skip");
      return setStatus(item, "Not a photo, so this file will be skipped", "err");
    }
    return renderScanStatus(item);
  }
  if (!item.targets.length) return setStatus(item, `.${item.ext || "?"} files aren't supported`, "err");
  setStatus(item, size);
}

function setStatus(item, text, cls = "") { paintStatus($(".status", item.li), text, cls); }

function paintStatus(el, text, cls = "") {
  el.className = `status ${cls}`;
  if (cls === "busy") el.innerHTML = `<span class="spin"></span>`;
  else el.textContent = "";
  el.append(text);
}

function resetResult(item) {
  if (item.url) URL.revokeObjectURL(item.url);
  item.url = null;
  $(".dl", item.li).hidden = true;
  $(".go", item.li).hidden = false;
  renderIdleStatus(item);
}

function removeItem(item) {
  if (item.url) URL.revokeObjectURL(item.url);
  if (item.beforeUrl) URL.revokeObjectURL(item.beforeUrl);
  if (item.preview?.url) URL.revokeObjectURL(item.preview.url);
  item.removed = true;
  item.li.remove();
  state.items = state.items.filter((i) => i !== item);
  clearResult();
  refreshPanel();
}

function syncOrderFromDom() {
  const byLi = new Map(state.items.map((i) => [i.li, i]));
  state.items = $$("#list > .row").map((li) => byLi.get(li));
  clearResult();
  refreshPanel();
}

function move(item, delta) {
  const idx = state.items.indexOf(item);
  const to = idx + delta;
  if (to < 0 || to >= state.items.length) return;
  const other = state.items[to].li;
  if (delta < 0) other.before(item.li);
  else other.after(item.li);
  syncOrderFromDom();
  $(delta < 0 ? ".up" : ".down", item.li).focus();
}

function wireDrag(item) {
  const li = item.li;
  li.addEventListener("dragstart", (e) => {
    if (state.mode === "convert" || e.target.closest("button, select, a")) return e.preventDefault();
    li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/x-pidify-row", String(state.items.indexOf(item)));
  });
  li.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    $$(".row").forEach((r) => r.classList.remove("drop-before", "drop-after"));
  });
  li.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes("text/x-pidify-row")) return;
    e.preventDefault();
    const after = e.clientY - li.getBoundingClientRect().top > li.offsetHeight / 2;
    li.classList.toggle("drop-after", after);
    li.classList.toggle("drop-before", !after);
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-before", "drop-after"));
  li.addEventListener("drop", (e) => {
    if (!e.dataTransfer.types.includes("text/x-pidify-row")) return;
    e.preventDefault();
    e.stopPropagation();
    const from = state.items[Number(e.dataTransfer.getData("text/x-pidify-row"))];
    const after = li.classList.contains("drop-after");
    li.classList.remove("drop-before", "drop-after");
    if (!from || from.li === li) return;
    if (after) li.after(from.li);
    else li.before(from.li);
    syncOrderFromDom();
  });
}

function refreshPanel() {
  $("#panel").hidden = state.items.length === 0;

  // "Convert all to" lists formats that at least one file supports.
  const all = new Set();
  state.items.filter((i) => !i.blocked).forEach((i) => i.targets.forEach((t) => all.add(t)));
  const cur = $("#allTarget").value;
  $("#allTarget").innerHTML = `<option value="">Choose…</option>` +
    [...all].map((t) => `<option value="${t}">${label(t)}</option>`).join("");
  if (all.has(cur)) $("#allTarget").value = cur;

  // Ordered modes: numbering, drag, and whether the main button is usable.
  const ordered = state.mode !== "convert";
  let n = 0;
  state.items.forEach((i, idx) => {
    $(".order", i.li).textContent = ordered && eligible(i) ? `${++n}` : "–";
    i.li.draggable = ordered;
    $(".up", i.li).disabled = idx === 0;
    $(".down", i.li).disabled = idx === state.items.length - 1;
  });

  const merge = $("#mergeBtn");
  const mergeN = state.items.filter(canMerge).length;
  merge.disabled = state.working || mergeN < 2;
  merge.textContent = mergeN >= 2 ? `Merge ${mergeN} files into PDF` : "Merge into PDF";
  merge.title = mergeN < 2 ? "Add at least two files" : "";

  const scan = $("#scanBtn");
  const scanN = state.items.filter(canScan).length;
  scan.disabled = state.working || scanN < 1;
  scan.textContent = scanN > 1 ? `Create ${scanN}-page PDF` : "Create PDF";
}

// ========================================================================== //
// Network helper
// ========================================================================== //

async function post(url, body, fallbackName) {
  const res = await fetch(url, { method: "POST", body });
  if (!res.ok) {
    let msg = `Error ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i);
  return { blob, name: m ? decodeURIComponent(m[1]) : fallbackName, headers: res.headers };
}

// ========================================================================== //
// Convert
// ========================================================================== //

async function convert(item) {
  if (item.busy || item.blocked) return;
  const target = $(".target", item.li).value;
  const go = $(".go", item.li);
  go.disabled = true;
  resetResult(item);
  item.busy = true;
  setStatus(item, `Converting to ${target.toUpperCase()}…`, "busy");

  const body = new FormData();
  body.append("file", item.file);
  body.append("target", target);
  try {
    const { blob, name } = await post("/api/convert", body, `converted.${target}`);
    item.url = URL.createObjectURL(blob);
    const dl = $(".dl", item.li);
    dl.href = item.url;
    dl.download = name;
    dl.hidden = false;
    go.hidden = true;
    setStatus(item, `Done · ${name} · ${humanSize(blob.size)}`, "ok");
  } catch (e) {
    setStatus(item, e.message, "err");
  } finally {
    item.busy = false;
    go.disabled = false;
  }
}

// ========================================================================== //
// Shared result box (merge + scan)
// ========================================================================== //

function clearResult() {
  if (state.working) return;
  if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
  state.resultUrl = null;
  $("#result").hidden = true;
}

function showResult(text, cls) {
  const box = $("#result");
  box.hidden = false;
  paintStatus($(".status", box), text, cls);
  $(".dl", box).hidden = true;
}

function showDownload(blob, name, extra = "") {
  state.resultUrl = URL.createObjectURL(blob);
  const dl = $("#result .dl");
  dl.href = state.resultUrl;
  dl.download = name;
  dl.hidden = false;
  paintStatus($("#result .status"), `Ready · ${name} · ${humanSize(blob.size)}${extra}`, "ok");
}

function checkBatch(parts) {
  const total = parts.reduce((s, i) => s + i.file.size, 0);
  if (parts.length > state.maxBatchFiles) return `You can use up to ${state.maxBatchFiles} files at once.`;
  if (total > state.maxBatchMb * 1024 ** 2) return `These files add up to ${humanSize(total)}. The limit is ${state.maxBatchMb} MB.`;
  return "";
}

async function runBatch(url, body, fallback, busyText, parts) {
  clearResult();
  const problem = checkBatch(parts);
  if (problem) return showResult(problem, "err");
  showResult(busyText, "busy");
  state.working = true;
  refreshPanel();
  try {
    const { blob, name } = await post(url, body, fallback);
    const skipped = state.items.length - parts.length;
    showDownload(blob, name, skipped ? ` · ${skipped} skipped` : "");
  } catch (e) {
    showResult(e.message, "err");
  } finally {
    state.working = false;
    refreshPanel();
  }
}

// ========================================================================== //
// Merge
// ========================================================================== //

function merge() {
  const parts = state.items.filter(canMerge);
  if (parts.length < 2 || state.working) return;
  const body = new FormData();
  parts.forEach((i) => body.append("files", i.file));
  body.append("name", $("#mergeName").value.trim() || "merged");
  const converting = parts.some((i) => i.ext !== "pdf");
  runBatch("/api/merge", body, "merged.pdf",
    converting ? `Turning files into PDF and merging ${parts.length} files…` : `Merging ${parts.length} files…`, parts);
}

// ========================================================================== //
// Scan to PDF
// ========================================================================== //

function scanOptions() {
  return {
    mode: $('input[name="scanMode"]:checked').value,
    crop: $("#optCrop").checked,
    marks: $("#optMarks").checked,
    light: $("#optLight").checked,
  };
}
const optionsKey = () => JSON.stringify(scanOptions());

function appendOptions(body) {
  const o = scanOptions();
  body.append("mode", o.mode);
  body.append("crop", String(o.crop));
  body.append("marks", String(o.marks));
  body.append("light", String(o.light));
}

function renderScanStatus(item) {
  const p = item.preview;
  const after = $(".thumb.after", item.li);
  after.classList.toggle("loading", !!p?.loading);
  after.style.backgroundImage = p?.url ? `url("${p.url}")` : "";
  if (!p || p.loading) return setStatus(item, "Cleaning up…", "busy");
  if (p.error) return setStatus(item, p.error, "err");
  const bits = [];
  if (scanOptions().crop) bits.push(p.info.page_found ? "Page straightened" : "Page edges not found, kept whole photo");
  if (scanOptions().marks) {
    const n = p.info.marks_removed;
    bits.push(n ? `${n} finger${n > 1 ? "s" : ""} removed` : "No fingers found");
  }
  if (scanOptions().light) bits.push("shadows removed");
  setStatus(item, bits.join(" · ") || "Ready", p.info.page_found || !scanOptions().crop ? "ok" : "");
}

// Preview queue: at most two photos processed at once; stale results are dropped.
const previewQueue = [];
let previewActive = 0;

function queuePreviews() {
  const key = optionsKey();
  for (const item of state.items) {
    if (!canScan(item)) continue;
    if (item.preview && item.preview.key === key) continue;
    if (item.preview?.url) URL.revokeObjectURL(item.preview.url);
    item.preview = { key, loading: true };
    renderScanStatus(item);
    if (!previewQueue.includes(item)) previewQueue.push(item);
  }
  pumpPreviews();
}

function pumpPreviews() {
  while (previewActive < 2 && previewQueue.length) {
    const item = previewQueue.shift();
    if (item.removed) continue;
    previewActive++;
    loadPreview(item).finally(() => { previewActive--; pumpPreviews(); });
  }
}

async function loadPreview(item) {
  const key = optionsKey();
  const body = new FormData();
  body.append("file", item.file);
  appendOptions(body);
  let result;
  try {
    const { blob, headers } = await post("/api/scan/preview", body, "preview.jpg");
    let info = { page_found: false, marks_removed: 0, notes: [] };
    try { info = JSON.parse(headers.get("x-scan-info") || "{}"); } catch {}
    result = { key, url: URL.createObjectURL(blob), info };
  } catch (e) {
    result = { key, error: e.message };
  }
  // Options changed or the file was removed while we waited: throw this one away
  // (queuePreviews has already queued a fresh request for the new options).
  if (item.removed || key !== optionsKey()) {
    if (result.url) URL.revokeObjectURL(result.url);
    return;
  }
  item.preview = result;
  if (state.mode === "scan") renderScanStatus(item);
}

let optTimer = 0;
function scanOptionsChanged() {
  clearResult();
  clearTimeout(optTimer);
  optTimer = setTimeout(queuePreviews, 200);
}

function scan() {
  const parts = state.items.filter(canScan);
  if (!parts.length || state.working) return;
  const body = new FormData();
  parts.forEach((i) => body.append("files", i.file));
  appendOptions(body);
  body.append("name", $("#scanName").value.trim() || "scan");
  runBatch("/api/scan", body, "scan.pdf",
    `Cleaning ${parts.length} photo${parts.length > 1 ? "s" : ""} and building your PDF…`, parts);
}

// --- Before/after viewer ----------------------------------------------------- //

function openViewer(item) {
  if (!item.preview?.url) return;
  $("#viewerTitle").textContent = item.file.name;
  $("#cmpAfter").src = item.preview.url;
  const cmp = $("#compare");
  cmp.classList.toggle("no-before", !item.beforeUrl);
  if (item.beforeUrl) $("#cmpBefore").src = item.beforeUrl;
  $("#cmpRange").value = 50;
  cmp.style.setProperty("--pos", "50%");
  $("#viewer").showModal();
}

// ========================================================================== //
// Wiring
// ========================================================================== //

const drop = $("#drop");
$("#picker").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#picker").click(); } });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

$$(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$("#themeBtn").addEventListener("click", toggleTheme);

$("#allTarget").addEventListener("change", (e) => {
  const t = e.target.value;
  if (!t) return;
  state.items.forEach((i) => {
    if (i.targets.includes(t) && $(".target", i.li).value !== t) {
      $(".target", i.li).value = t;
      resetResult(i);
    }
  });
});

$("#convertAll").addEventListener("click", async () => {
  const queue = state.items.filter((i) => !i.blocked && !i.url);
  // Two at a time keeps LibreOffice from being overwhelmed.
  const worker = async () => { while (queue.length) await convert(queue.shift()); };
  $("#convertAll").disabled = true;
  await Promise.all([worker(), worker()]);
  $("#convertAll").disabled = false;
});

$("#mergeBtn").addEventListener("click", merge);
$("#mergeName").addEventListener("input", clearResult);
$("#mergeName").addEventListener("keydown", (e) => { if (e.key === "Enter") merge(); });

$("#scanBtn").addEventListener("click", scan);
$("#scanName").addEventListener("input", clearResult);
$("#scanName").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
$$("#optCrop, #optMarks, #optLight, input[name='scanMode']").forEach((el) => el.addEventListener("change", scanOptionsChanged));

$$(".clear").forEach((b) => b.addEventListener("click", () => [...state.items].forEach(removeItem)));

$("#cmpRange").addEventListener("input", (e) => $("#compare").style.setProperty("--pos", `${e.target.value}%`));
$("#viewerClose").addEventListener("click", () => $("#viewer").close());
$("#viewer").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

init();
