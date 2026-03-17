# AlphaConvert — Site Web

## Structure des fichiers
```
alphaconvert-web/
├── index.html       ← page principale
├── api.py           ← backend FastAPI (local + Railway)
├── css/
│   └── style.css    ← tous les styles
└── js/
    └── app.js       ← toute la logique JS
```

## Lancer en local

### 1. Installer les dépendances
```bash
pip install fastapi uvicorn yt-dlp bgutil-ytdlp-pot-provider
```

### 2. Démarrer le backend
```bash
uvicorn api:app --reload --port 8000
```

### 3. Ouvrir le site
Ouvre `index.html` directement dans ton navigateur.
Le JS détecte automatiquement si tu es en local → utilise http://localhost:8000

## Déployer sur Railway (backend)
Le fichier `api.py` peut remplacer `bot.py` ou tourner en parallèle.
Ajoute dans Railway :
```
uvicorn api:app --host 0.0.0.0 --port $PORT
```

## Déployer sur Vercel (frontend)
Upload `index.html`, `css/` et `js/` sur Vercel.
Dans `js/app.js`, la constante BACKEND bascule automatiquement
vers l'URL Railway en production.
