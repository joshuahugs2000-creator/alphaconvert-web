// AlphaConvert — app.js
const API = "";  // même origine — Railway sert l'API et le frontend

// ── État ──────────────────────────────────────────────────────────────────────
let currentPlatform = "yt";
let currentFmt      = "mp4";
let currentQ        = "1080";

// ── Limite gratuite ───────────────────────────────────────────────────────────
const FREE_LIMIT = 3;

function getTodayKey() {
  return "dl_" + new Date().toISOString().slice(0, 10);
}
function getCount() {
  return parseInt(localStorage.getItem(getTodayKey()) || "0");
}
function incrementCount() {
  const k = getTodayKey();
  localStorage.setItem(k, String(getCount() + 1));
}
function isPremium() {
  const code   = localStorage.getItem("premiumCode");
  const expiry = localStorage.getItem("premiumExpiry");
  return code && expiry && new Date(expiry) > new Date();
}

function renderCounter() {
  const el = document.getElementById("dlCounter");
  if (!el) return;
  if (isPremium()) { el.style.display = "none"; return; }
  const used      = getCount();
  const remaining = Math.max(0, FREE_LIMIT - used);
  el.style.display = "block";
  if (remaining === 0) {
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;justify-content:center;flex-wrap:wrap;">
        <span style="color:#ef4444;font-weight:600;">🚫 Limite atteinte pour aujourd'hui</span>
        <a href="premium.html" style="color:#a78bfa;font-weight:700;text-decoration:underline;">⭐ Passe Premium pour continuer</a>
      </div>
      <div style="background:#ef4444;height:4px;border-radius:4px;margin-top:6px;"></div>`;
  } else {
    const pct   = ((FREE_LIMIT - remaining) / FREE_LIMIT) * 100;
    const color = remaining === 1 ? "#f97316" : "#7c3aed";
    const icon  = remaining === 1 ? "⚠️" : "🔓";
    el.innerHTML = `
      <span style="color:${color};font-weight:600;">${icon} ${remaining} téléchargement${remaining > 1 ? "s" : ""} gratuit${remaining > 1 ? "s" : ""} restant${remaining > 1 ? "s" : ""} aujourd'hui</span>
      <div style="background:#e5e7eb;height:4px;border-radius:4px;margin-top:6px;overflow:hidden;">
        <div style="background:${color};height:100%;width:${pct}%;transition:width .4s;"></div>
      </div>`;
  }
}

// ── Onglets YouTube / TikTok ──────────────────────────────────────────────────
function switchTab(btn, platform) {
  currentPlatform = platform;
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  btn.classList.add("active");
  const input = document.getElementById("urlInput");
  if (input) {
    input.value       = "";
    input.placeholder = platform === "yt"
      ? "Colle ton lien YouTube ici…"
      : "Colle ton lien TikTok ici…";
  }
  renderFormats();
  hideResult();
}

function renderFormats() {
  const row = document.querySelector(".format-row");
  if (!row) return;
  const ytF = [
    { label: "MP4 1080p HD", fmt: "mp4", q: "1080" },
    { label: "MP4 720p",     fmt: "mp4", q: "720"  },
    { label: "MP4 480p",     fmt: "mp4", q: "480"  },
    { label: "MP4 360p",     fmt: "mp4", q: "360"  },
    { label: "MP3 Audio",    fmt: "mp3", q: "best" },
  ];
  const ttF = [
    { label: "MP4 HD",    fmt: "mp4", q: "1080" },
    { label: "MP4 SD",    fmt: "mp4", q: "720"  },
    { label: "MP3 Audio", fmt: "mp3", q: "best" },
  ];
  const formats = currentPlatform === "tt" ? ttF : ytF;
  row.innerHTML = formats
    .map((f, i) => `<div class="fmt-chip${i === 0 ? " selected" : ""}" data-fmt="${f.fmt}" data-q="${f.q}" onclick="selectFmt(this)">${f.label}</div>`)
    .join("");
  currentFmt = formats[0].fmt;
  currentQ   = formats[0].q;
}

function selectFmt(el) {
  document.querySelectorAll(".fmt-chip").forEach(c => c.classList.remove("selected"));
  el.classList.add("selected");
  currentFmt = el.dataset.fmt;
  currentQ   = el.dataset.q;
}

// ── UI ────────────────────────────────────────────────────────────────────────
function showLoader(msg) {
  const l = document.getElementById("loader");
  const t = document.getElementById("loaderText");
  if (l) l.style.display = "flex";
  if (t) t.textContent = msg || "Analyse en cours…";
  hideResult();
}
function hideLoader() {
  const l = document.getElementById("loader");
  if (l) l.style.display = "none";
}
function showResult(title, meta, thumb, btns) {
  const row    = document.getElementById("resultRow");
  const rTitle = document.getElementById("rTitle");
  const rMeta  = document.getElementById("rMeta");
  const rThumb = document.getElementById("rThumb");
  const dlGrid = document.getElementById("dlGrid");
  if (!row) return;
  if (rTitle) rTitle.textContent = title;
  if (rMeta)  rMeta.textContent  = meta;
  if (rThumb && thumb) { rThumb.src = thumb; rThumb.style.display = "block"; }
  if (dlGrid) dlGrid.innerHTML = btns;
  row.style.display = "flex";
}
function hideResult() {
  const r = document.getElementById("resultRow");
  if (r) r.style.display = "none";
}
function showToast(msg, color) {
  let t = document.getElementById("premToast");
  if (!t) { t = document.createElement("div"); t.id = "premToast"; document.body.appendChild(t); }
  t.textContent = msg;
  if (color) t.style.background = color;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 4000);
}
function formatDuration(s) {
  if (!s) return "";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// ── Analyse ───────────────────────────────────────────────────────────────────
async function analyze() {
  const url = (document.getElementById("urlInput")?.value || "").trim();
  if (!url) { showToast("⚠️ Colle un lien d'abord !"); return; }

  if (!isPremium() && getCount() >= FREE_LIMIT) {
    showToast("🚫 Limite gratuite atteinte — passe Premium !", "#ef4444");
    renderCounter();
    return;
  }

  showLoader("Analyse du lien en cours…");

  try {
    const res  = await fetch(`${API}/info?url=${encodeURIComponent(url)}`);
    const data = await res.json();

    if (!res.ok || data.error || data.detail) {
      hideLoader();
      showToast("❌ " + (data.detail || data.error || "Impossible d'analyser ce lien. Vérifie qu'il est public."), "#ef4444");
      return;
    }

    hideLoader();

    const dur  = formatDuration(data.duration);
    const meta = [data.uploader, dur].filter(Boolean).join(" · ");

    const fmts = currentPlatform === "tt"
      ? [{ label: "MP4 HD", fmt: "mp4", q: "1080" }, { label: "MP4 SD", fmt: "mp4", q: "720" }, { label: "MP3 Audio", fmt: "mp3", q: "best" }]
      : [{ label: "MP4 1080p HD", fmt: "mp4", q: "1080" }, { label: "MP4 720p", fmt: "mp4", q: "720" }, { label: "MP4 480p", fmt: "mp4", q: "480" }, { label: "MP4 360p", fmt: "mp4", q: "360" }, { label: "MP3 Audio", fmt: "mp3", q: "best" }];

    const btns = fmts.map(f =>
      `<a class="dl-btn${f.fmt === "mp3" ? " dl-btn-audio" : ""}"
          href="${API}/download?url=${encodeURIComponent(url)}&format=${f.fmt}&quality=${f.q}"
          download
          onclick="onDownloadClick()">
        ⬇ ${f.label}
      </a>`
    ).join("");

    showResult(data.title || "Vidéo", meta, data.thumbnail || "", btns);

    if (!isPremium()) {
      incrementCount();
      renderCounter();
    }

  } catch (err) {
    hideLoader();
    showToast("❌ Erreur réseau — vérifie ta connexion.", "#ef4444");
    console.error(err);
  }
}

function onDownloadClick() {
  renderCounter();
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  renderFormats();
  renderCounter();
});

// Exports globaux
window.analyze         = analyze;
window.switchTab       = switchTab;
window.selectFmt       = selectFmt;
window.renderFormats   = renderFormats;
window.onDownloadClick = onDownloadClick;
