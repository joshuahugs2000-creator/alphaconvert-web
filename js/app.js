// ── CONFIG ──────────────────────────────────────────────────
const BACKEND = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? 'http://localhost:8000'
  : 'https://alphaconvert-web-production-58f7.up.railway.app';

const DAILY_LIMIT = 3;

// ── FIREBASE ─────────────────────────────────────────────────
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getFirestore, doc, getDoc, setDoc, updateDoc, increment, collection }
  from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

const firebaseConfig = {
  apiKey: "AIzaSyBJgtQp4ZrOPuUhmwiQw6FmOvt7nywudOc",
  authDomain: "alphaconvert-d6d65.firebaseapp.com",
  projectId: "alphaconvert-d6d65",
  storageBucket: "alphaconvert-d6d65.firebasestorage.app",
  messagingSenderId: "599445275974",
  appId: "1:599445275974:web:9c19afd3c4f8219e3f9147"
};
const fbApp = initializeApp(firebaseConfig);
const db = getFirestore(fbApp);

// ── FINGERPRINT ───────────────────────────────────────────────
function getFingerprint() {
  const raw = [
    navigator.userAgent,
    navigator.language,
    screen.width + 'x' + screen.height,
    screen.colorDepth,
    new Date().getTimezoneOffset(),
    navigator.hardwareConcurrency || '',
    navigator.platform || ''
  ].join('|');
  let hash = 0;
  for (let i = 0; i < raw.length; i++) {
    hash = ((hash << 5) - hash) + raw.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

// ── IP ────────────────────────────────────────────────────────
async function getIP() {
  try {
    const r = await fetch('https://api.ipify.org?format=json');
    const d = await r.json();
    return d.ip || 'unknown';
  } catch { return 'unknown'; }
}

// ── CLIENT ID ─────────────────────────────────────────────────
let clientId = null;
async function getClientId() {
  if (clientId) return clientId;
  const fp = getFingerprint();
  const ip = await getIP();
  clientId = `${fp}_${ip.replace(/\./g, '-')}`;
  return clientId;
}

// ── TODAY KEY ─────────────────────────────────────────────────
function todayKey() {
  return new Date().toISOString().split('T')[0];
}

// ── PREMIUM ───────────────────────────────────────────────────
let isPremium = false;
let premiumExpiry = null;

async function checkPremiumCode(code) {
  try {
    const ref = doc(db, 'premiumCodes', code.toUpperCase().trim());
    const snap = await getDoc(ref);
    if (!snap.exists()) return { valid: false, msg: '❌ Code invalide.' };
    const data = snap.data();
    if (data.used) return { valid: false, msg: '❌ Ce code a déjà été utilisé.' };
    const expiry = new Date(data.expiresAt);
    if (expiry < new Date()) return { valid: false, msg: '❌ Ce code est expiré.' };
    const cid = await getClientId();
    await updateDoc(ref, { used: true, usedBy: cid, usedAt: new Date().toISOString() });
    localStorage.setItem('premiumCode', code.toUpperCase().trim());
    localStorage.setItem('premiumExpiry', data.expiresAt);
    return { valid: true, expiry: data.expiresAt, label: data.label };
  } catch (e) {
    return { valid: false, msg: '❌ Erreur de vérification.' };
  }
}

async function checkSavedPremium() {
  const code = localStorage.getItem('premiumCode');
  const expiry = localStorage.getItem('premiumExpiry');
  if (!code || !expiry) return false;
  const exp = new Date(expiry);
  if (exp < new Date()) {
    localStorage.removeItem('premiumCode');
    localStorage.removeItem('premiumExpiry');
    return false;
  }
  // On fait confiance au localStorage en premier — Firebase sert uniquement à vérifier révocation
  isPremium = true;
  premiumExpiry = expiry;
  try {
    const ref = doc(db, 'premiumCodes', code);
    const snap = await getDoc(ref);
    if (!snap.exists() || snap.data().revoked) {
      localStorage.removeItem('premiumCode');
      localStorage.removeItem('premiumExpiry');
      isPremium = false;
      premiumExpiry = null;
      return false;
    }
    // data.used est normal (le code a été activé par cet utilisateur), ne pas bloquer
  } catch {
    // Firebase indispo → on garde le premium actif
  }
  return true;
}

// ── DOWNLOAD LIMIT ────────────────────────────────────────────
async function canDownload() {
  if (isPremium) return { allowed: true };
  const cid = await getClientId();
  const today = todayKey();
  const ref = doc(db, 'limits', `${cid}_${today}`);
  try {
    const snap = await getDoc(ref);
    if (!snap.exists()) return { allowed: true, count: 0 };
    const count = snap.data().count || 0;
    if (count >= DAILY_LIMIT) return { allowed: false, count };
    return { allowed: true, count };
  } catch { return { allowed: true, count: 0 }; }
}

async function recordDownload() {
  if (isPremium) return;
  const cid = await getClientId();
  const today = todayKey();
  const ref = doc(db, 'limits', `${cid}_${today}`);
  try {
    const snap = await getDoc(ref);
    if (!snap.exists()) {
      await setDoc(ref, { count: 1, clientId: cid, date: today });
    } else {
      await updateDoc(ref, { count: increment(1) });
    }
  } catch {}
}

async function getDownloadCount() {
  if (isPremium) return 0;
  const cid = await getClientId();
  const today = todayKey();
  const ref = doc(db, 'limits', `${cid}_${today}`);
  try {
    const snap = await getDoc(ref);
    return snap.exists() ? (snap.data().count || 0) : 0;
  } catch { return 0; }
}

// ── UI PREMIUM BADGE ─────────────────────────────────────────
function updatePremiumBadge() {
  const badge = document.getElementById('premiumBadge');
  const counter = document.getElementById('dlCounter');
  if (!badge || !counter) return;

  if (isPremium) {
    badge.innerHTML = `⭐ Premium actif`;
    badge.style.color = '#f59e0b';
    badge.style.display = 'block';
    counter.style.display = 'none';
  } else {
    badge.style.display = 'none';
    getDownloadCount().then(count => {
      const left = DAILY_LIMIT - count;
      counter.textContent = `${left} téléchargement${left > 1 ? 's' : ''} gratuit${left > 1 ? 's' : ''} restant aujourd'hui`;
      counter.style.color = left <= 1 ? '#ef4444' : '#6b7280';
      counter.style.display = 'block';
    });
  }
}

// ── TABS ─────────────────────────────────────────────────────
const tabPlaceholders = {
  yt: 'Colle ton lien YouTube ici…',
  tt: 'Colle ton lien TikTok ici…'
};

const tabFormats = {
  yt: [
    { label: 'MP4 1080p HD', fmt: 'mp4', q: '1080' },
    { label: 'MP4 720p',     fmt: 'mp4', q: '720'  },
    { label: 'MP4 480p',     fmt: 'mp4', q: '480'  },
    { label: 'MP4 360p',     fmt: 'mp4', q: '360'  },
    { label: 'MP3 Audio',    fmt: 'mp3', q: 'best' },
  ],
  tt: [
    { label: 'MP4 HD',    fmt: 'mp4', q: '1080' },
    { label: 'MP4 SD',    fmt: 'mp4', q: '720'  },
    { label: 'MP3 Audio', fmt: 'mp3', q: 'best' },
  ]
};

let currentTab = 'yt';

function switchTab(btn, platform) {
  currentTab = platform;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  const input = document.getElementById('urlInput');
  if (input) {
    input.value = '';
    input.placeholder = tabPlaceholders[platform] || '';
  }
  hideResult();
}

function selectFmt(el) {
  document.querySelectorAll('.fmt-chip').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
}

function hideResult() {
  document.getElementById('resultRow').classList.remove('show');
  document.getElementById('loader').classList.remove('show');
}

function fmtDur(sec) {
  if (!sec) return '';
  const s = Math.round(Number(sec));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

function dlIcon() {
  return `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="7 10 12 15 17 10"/>
    <line x1="12" y1="15" x2="12" y2="3"/>
  </svg>`;
}

// ── ANALYZE ───────────────────────────────────────────────────
async function analyze() {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) { document.getElementById('urlInput').focus(); return; }

  const { allowed } = await canDownload();
  if (!allowed) { showLimitModal(); return; }

  hideResult();
  const loader = document.getElementById('loader');
  loader.classList.add('show');
  document.getElementById('loaderText').textContent = 'Analyse du lien en cours…';

  try {
    const res = await fetch(`${BACKEND}/info?url=${encodeURIComponent(url)}`);
    if (!res.ok) throw new Error('Erreur serveur');
    const data = await res.json();

    loader.classList.remove('show');

    document.getElementById('rThumb').src = data.thumbnail || '';
    document.getElementById('rTitle').textContent = data.title || 'Vidéo';
    const dur = data.duration ? ` · ${fmtDur(data.duration)}` : '';
    document.getElementById('rMeta').textContent = (data.platform || '') + dur;

    const formats = tabFormats[currentTab] || tabFormats.yt;

    document.getElementById('dlGrid').innerHTML = formats.map(f => `
      <a class="dl-chip" href="#" onclick="handleDownload(event,'${encodeURIComponent(url)}','${f.fmt}','${f.q}')">
        ${dlIcon()} ${f.label}
      </a>
    `).join('');

    document.getElementById('resultRow').classList.add('show');

  } catch (e) {
    loader.classList.remove('show');
    alert('Impossible d\'analyser ce lien.\nVérifie qu\'il est public et réessaie.');
  }
}

// ── DOWNLOAD HANDLER ─────────────────────────────────────────
async function handleDownload(e, encodedUrl, fmt, q) {
  e.preventDefault();
  const { allowed } = await canDownload();
  if (!allowed) { showLimitModal(); return; }

  await recordDownload();
  updatePremiumBadge();

  // Lien temporaire pour forcer le téléchargement
  const a = document.createElement('a');
  a.href = `${BACKEND}/download?url=${encodedUrl}&format=${fmt}&quality=${q}`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── LIMIT MODAL ───────────────────────────────────────────────
function showLimitModal() {
  document.getElementById('limitModal').style.display = 'flex';
}
function closeLimitModal() {
  document.getElementById('limitModal').style.display = 'none';
}

// ── PREMIUM CODE MODAL ────────────────────────────────────────
function openCodeModal() {
  closeLimitModal();
  document.getElementById('codeModal').style.display = 'flex';
  document.getElementById('codeInput').value = '';
  document.getElementById('codeMsg').textContent = '';
}
function closeCodeModal() {
  document.getElementById('codeModal').style.display = 'none';
}

async function activateCode() {
  const code = document.getElementById('codeInput').value.trim();
  if (!code) return;
  const btn = document.getElementById('activateBtn');
  btn.disabled = true;
  btn.textContent = 'Vérification…';
  document.getElementById('codeMsg').textContent = '';

  const result = await checkPremiumCode(code);
  if (result.valid) {
    isPremium = true;
    premiumExpiry = result.expiry;
    const exp = new Date(result.expiry).toLocaleDateString('fr', {day:'2-digit', month:'long', year:'numeric'});
    document.getElementById('codeMsg').style.color = '#10b981';
    document.getElementById('codeMsg').textContent = `✅ Premium activé ! Expire le ${exp}`;
    updatePremiumBadge();
    setTimeout(() => closeCodeModal(), 2000);
  } else {
    document.getElementById('codeMsg').style.color = '#ef4444';
    document.getElementById('codeMsg').textContent = result.msg;
  }
  btn.disabled = false;
  btn.textContent = 'Activer';
}

// ── INIT ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await checkSavedPremium();
  updatePremiumBadge();

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') analyze();
  });
});

// Expose functions to global scope
window.switchTab = switchTab;
window.selectFmt = selectFmt;
window.analyze = analyze;
window.handleDownload = handleDownload;
window.showLimitModal = showLimitModal;
window.closeLimitModal = closeLimitModal;
window.openCodeModal = openCodeModal;
window.closeCodeModal = closeCodeModal;
window.activateCode = activateCode;
