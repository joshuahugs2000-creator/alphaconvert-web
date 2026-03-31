// ── CONFIG ──────────────────────────────────────────────────
const BACKEND = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? 'http://localhost:8000'
  : 'https://alphaconvert-web-production-58f7.up.railway.app';

const DAILY_LIMIT = 3;

// ── FIREBASE ─────────────────────────────────────────────────
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getFirestore, doc, getDoc, setDoc, updateDoc, increment }
  from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";
import { getAuth, onAuthStateChanged }
  from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";

const firebaseConfig = {
  apiKey: "AIzaSyBJgtQp4ZrOPuUhmwiQw6FmOvt7nywudOc",
  authDomain: "alphaconvert-d6d65.firebaseapp.com",
  projectId: "alphaconvert-d6d65",
  storageBucket: "alphaconvert-d6d65.firebasestorage.app",
  messagingSenderId: "599445275974",
  appId: "1:599445275974:web:9c19afd3c4f8219e3f9147"
};
const fbApp = initializeApp(firebaseConfig);
const db    = getFirestore(fbApp);
const auth  = getAuth(fbApp);

// ── FINGERPRINT (fallback sans compte) ───────────────────────
function getFingerprint() {
  const raw = [
    navigator.userAgent, navigator.language,
    screen.width + 'x' + screen.height, screen.colorDepth,
    new Date().getTimezoneOffset(),
    navigator.hardwareConcurrency || '', navigator.platform || ''
  ].join('|');
  let h = 0;
  for (let i = 0; i < raw.length; i++) { h = ((h << 5) - h) + raw.charCodeAt(i); h |= 0; }
  return Math.abs(h).toString(36);
}

async function getIP() {
  try { const r = await fetch('https://api.ipify.org?format=json'); return (await r.json()).ip || 'unknown'; }
  catch { return 'unknown'; }
}

let _clientId = null;
async function getClientId() {
  const user = auth.currentUser;
  if (user) return `user_${user.uid}`;
  if (_clientId) return _clientId;
  _clientId = `${getFingerprint()}_${(await getIP()).replace(/\./g, '-')}`;
  return _clientId;
}

function todayKey() { return new Date().toISOString().split('T')[0]; }

// ── PREMIUM ───────────────────────────────────────────────────
let isPremium    = false;
let premiumExpiry = null;

// Vérification au démarrage : compte Firebase en priorité, localStorage en fallback
async function checkPremiumStatus() {
  isPremium = false;
  premiumExpiry = null;

  const user = auth.currentUser;

  // 1. Compte connecté → vérifier users/{uid} dans Firebase
  if (user) {
    try {
      const snap = await getDoc(doc(db, 'users', user.uid));
      if (snap.exists()) {
        const d = snap.data();
        const active    = d.premiumStatus === 'active';
        const unlimited = d.unlimited === true;
        const valid     = unlimited || !d.premiumExpiry || new Date(d.premiumExpiry) > new Date();
        if (active && valid) {
          isPremium     = true;
          premiumExpiry = unlimited ? null : d.premiumExpiry;
          return true;
        }
      }
    } catch { /* Firebase indispo → continuer vers localStorage */ }
  }

  // 2. Fallback localStorage (code activé sans compte ou Firebase down)
  const exp = localStorage.getItem('premiumExpiry');
  if (exp && new Date(exp) > new Date()) {
    isPremium     = true;
    premiumExpiry = exp;
    return true;
  }
  // Nettoyer si expiré
  localStorage.removeItem('premiumCode');
  localStorage.removeItem('premiumExpiry');
  return false;
}

// Activation d'un code premium
async function checkPremiumCode(code) {
  const upper = code.toUpperCase().trim();
  try {
    const ref  = doc(db, 'premiumCodes', upper);
    const snap = await getDoc(ref);
    if (!snap.exists())     return { valid: false, msg: '❌ Code invalide.' };
    const d = snap.data();
    if (d.revoked)          return { valid: false, msg: '❌ Ce code a été révoqué.' };
    const unlimited = d.unlimited === true;
    if (!unlimited && new Date(d.expiresAt) < new Date())
                            return { valid: false, msg: '❌ Ce code est expiré.' };

    // Marquer utilisé
    const usedBy = auth.currentUser ? auth.currentUser.uid : await getClientId();
    await updateDoc(ref, { used: true, usedBy, usedAt: new Date().toISOString() });

    // Enregistrer sur le compte Google si connecté
    const user = auth.currentUser;
    if (user) {
      await setDoc(doc(db, 'users', user.uid), {
        premiumStatus: 'active',
        unlimited,
        premiumExpiry:  unlimited ? null : d.expiresAt,
        premiumLabel:   d.label,
        activatedAt:    new Date().toISOString(),
        activatedCode:  upper,
        email:          user.email
      }, { merge: true });
    }

    // localStorage en fallback
    if (!unlimited) {
      localStorage.setItem('premiumCode',   upper);
      localStorage.setItem('premiumExpiry', d.expiresAt);
    }

    isPremium     = true;
    premiumExpiry = unlimited ? null : d.expiresAt;
    return { valid: true, expiry: d.expiresAt, label: d.label, unlimited };
  } catch (e) {
    return { valid: false, msg: '❌ Erreur de vérification.' };
  }
}

// ── DOWNLOAD LIMIT ────────────────────────────────────────────
async function canDownload() {
  if (isPremium) return { allowed: true };
  const cid   = await getClientId();
  const today = todayKey();
  const ref   = doc(db, 'limits', `${cid}_${today}`);
  try {
    const snap  = await getDoc(ref);
    if (!snap.exists()) return { allowed: true, count: 0 };
    const count = snap.data().count || 0;
    return count >= DAILY_LIMIT ? { allowed: false, count } : { allowed: true, count };
  } catch { return { allowed: true, count: 0 }; }
}

async function recordDownload() {
  if (isPremium) return;
  const cid   = await getClientId();
  const today = todayKey();
  const ref   = doc(db, 'limits', `${cid}_${today}`);
  try {
    const snap = await getDoc(ref);
    if (!snap.exists()) await setDoc(ref, { count: 1, clientId: cid, date: today });
    else                await updateDoc(ref, { count: increment(1) });
  } catch {}
}

async function getDownloadCount() {
  if (isPremium) return 0;
  const cid = await getClientId();
  const ref = doc(db, 'limits', `${cid}_${todayKey()}`);
  try { const s = await getDoc(ref); return s.exists() ? (s.data().count || 0) : 0; }
  catch { return 0; }
}

// ── UI PREMIUM BADGE ─────────────────────────────────────────
function updatePremiumBadge() {
  const badge   = document.getElementById('premiumBadge');
  const counter = document.getElementById('dlCounter');
  if (!badge || !counter) return;

  if (isPremium) {
    badge.innerHTML = premiumExpiry
      ? `⭐ Premium actif — expire le ${new Date(premiumExpiry).toLocaleDateString('fr', {day:'2-digit',month:'long',year:'numeric'})}`
      : `⭐ Premium actif`;
    badge.style.color   = '#f59e0b';
    badge.style.display = 'block';
    counter.style.display = 'none';
  } else {
    badge.style.display = 'none';
    getDownloadCount().then(count => {
      const left = DAILY_LIMIT - count;
      counter.textContent = `${left} téléchargement${left > 1 ? 's' : ''} gratuit${left > 1 ? 's' : ''} restant aujourd'hui`;
      counter.style.color   = left <= 1 ? '#ef4444' : '#6b7280';
      counter.style.display = 'block';
    });
  }
}

// ── TABS ─────────────────────────────────────────────────────
const tabPlaceholders = { yt: 'Colle ton lien YouTube ici…', tt: 'Colle ton lien TikTok ici…' };
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
  if (input) { input.value = ''; input.placeholder = tabPlaceholders[platform] || ''; }
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
  const s = Math.round(Number(sec)), m = Math.floor(s / 60);
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
    document.getElementById('rThumb').src           = data.thumbnail || '';
    document.getElementById('rTitle').textContent   = data.title    || 'Vidéo';
    document.getElementById('rMeta').textContent    = (data.platform || '') + (data.duration ? ` · ${fmtDur(data.duration)}` : '');

    const formats = tabFormats[currentTab] || tabFormats.yt;
    document.getElementById('dlGrid').innerHTML = formats.map(f => `
      <a class="dl-chip" href="#" onclick="handleDownload(event,'${encodeURIComponent(url)}','${f.fmt}','${f.q}')">
        ${dlIcon()} ${f.label}
      </a>
    `).join('');

    document.getElementById('resultRow').classList.add('show');
  } catch {
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

  // window.location.href → le navigateur suit directement le stream
  // Le backend envoie Content-Disposition: attachment → téléchargement forcé
  window.location.href = `${BACKEND}/download?url=${encodedUrl}&format=${fmt}&quality=${q}`;
}

// ── MODALS ────────────────────────────────────────────────────
function showLimitModal()  { document.getElementById('limitModal').style.display = 'flex'; }
function closeLimitModal() { document.getElementById('limitModal').style.display = 'none'; }

function openCodeModal() {
  closeLimitModal();
  document.getElementById('codeModal').style.display = 'flex';
  document.getElementById('codeInput').value = '';
  document.getElementById('codeMsg').textContent = '';
}
function closeCodeModal() { document.getElementById('codeModal').style.display = 'none'; }

async function activateCode() {
  const code = document.getElementById('codeInput').value.trim();
  if (!code) return;
  const btn = document.getElementById('activateBtn');
  btn.disabled = true; btn.textContent = 'Vérification…';
  document.getElementById('codeMsg').textContent = '';

  const result = await checkPremiumCode(code);
  if (result.valid) {
    const expText = result.unlimited
      ? 'Accès illimité permanent !'
      : `Expire le ${new Date(result.expiry).toLocaleDateString('fr', {day:'2-digit',month:'long',year:'numeric'})}`;
    document.getElementById('codeMsg').style.color   = '#10b981';
    document.getElementById('codeMsg').textContent   = `✅ Premium activé ! ${expText}`;
    updatePremiumBadge();
    setTimeout(() => closeCodeModal(), 2000);
  } else {
    document.getElementById('codeMsg').style.color   = '#ef4444';
    document.getElementById('codeMsg').textContent   = result.msg;
  }
  btn.disabled = false; btn.textContent = 'Activer';
}

// ── INIT ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Écouter connexion/déconnexion Google → rafraîchir le statut premium
  onAuthStateChanged(auth, async () => {
    await checkPremiumStatus();
    updatePremiumBadge();
  });

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') analyze();
  });
});

// Expose to global scope
window.switchTab      = switchTab;
window.selectFmt      = selectFmt;
window.analyze        = analyze;
window.handleDownload = handleDownload;
window.showLimitModal = showLimitModal;
window.closeLimitModal= closeLimitModal;
window.openCodeModal  = openCodeModal;
window.closeCodeModal = closeCodeModal;
window.activateCode   = activateCode;
