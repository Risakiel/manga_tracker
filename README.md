# Manga Tracker

Application de suivi de mangas/manhwas/manhwas adultes, pensée pour compléter une
bibliothèque [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) + [Komga](https://komga.org/).

- Import du fichier Excel de suivi existant (colonnes `Libraries`, `Titre Anglais`,
  `Titre Dossier Serveur`, `MangaUpdates.com (URL)`, `Last Scan`, `Statut du Manga`).
- Synchronisation automatique avec [MangaUpdates](https://www.mangaupdates.com/) (statut,
  auteur/artiste, genres, titres alternatifs, couverture, dernier chapitre) via son API
  publique — pas de clé requise.
- Enrichissement complémentaire via [AniList](https://anilist.co/) quand MangaUpdates ne
  suffit pas (couverture, titres alternatifs).
- Réconciliation périodique avec l'API GraphQL de Suwayomi : nombre de chapitres
  réellement téléchargés, détection des séries présentes dans Suwayomi mais absentes du
  suivi.
- Interface web simple (FastAPI + Jinja2 + HTMX, SQLite), packagée en image Docker.

## Lancer en local (développement)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # ou .venv/bin/pip sous Linux/Mac
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Ouvrir http://127.0.0.1:8000, puis "Import Excel" pour charger le fichier de suivi.

Lancer les tests : `.venv/Scripts/python -m pytest`

## Configuration

Copier `.env.example` en `.env` (ou définir les variables d'environnement directement,
par exemple dans `docker-compose.yml` ou le template Unraid) :

| Variable | Description | Défaut |
|---|---|---|
| `SUWAYOMI_URL` | URL de base de ton instance Suwayomi (sans `/api/graphql`) | vide (sync Suwayomi désactivée) |
| `SUWAYOMI_USERNAME` / `SUWAYOMI_PASSWORD` | Si Basic Auth activé sur Suwayomi | vide |
| `SYNC_INTERVAL_HOURS` | Fréquence de sync MangaUpdates | 24 |
| `SUWAYOMI_SYNC_INTERVAL_HOURS` | Fréquence de réconciliation Suwayomi | 6 |
| `ENABLE_SCHEDULER` | Désactive les jobs automatiques (sync manuelle uniquement) | true |

Si ta version de Suwayomi expose des noms de champs GraphQL différents de ceux utilisés
dans `app/services/suwayomi_client.py` (vérifié contre le code source du projet, pas contre
une instance réelle), une introspection GraphQL rapide contre ton serveur (`{ __schema { types { name } } }`
ou simplement l'explorateur GraphQL intégré à Suwayomi) permettra d'ajuster la requête.

## Déploiement sur Unraid

1. Le repo GitHub construit et publie l'image sur `ghcr.io/<toi>/manga_tracker` à chaque
   push sur `main` (voir `.github/workflows/docker-publish.yml`).
2. Sur Unraid, via le plugin **Docker Compose Manager** (ou manuellement) :
   - Copier `docker-compose.yml` dans un dossier de ton appdata (ex: `/mnt/user/appdata/manga-tracker/`).
   - Remplacer `build: .` par `image: ghcr.io/<toi>/manga_tracker:latest` si tu ne veux pas
     builder localement.
   - Ajuster `SUWAYOMI_URL` avec l'IP/port de ton conteneur Suwayomi.
   - `docker compose up -d`.
3. Le volume `./data` contient la base SQLite — à sauvegarder comme le reste de ton appdata.
4. Import initial : ouvrir `http://<unraid>:8000/import` et uploader ton fichier Excel.

## Attribution

Les données de statut/auteur/genres proviennent de [MangaUpdates](https://www.mangaupdates.com/)
et [AniList](https://anilist.co/) via leurs API publiques respectives. Cette application ne
republie pas leur contenu publiquement — usage strictement personnel/privé.

## Architecture

```
app/
├── main.py                  # FastAPI app, lifespan (DB + scheduler)
├── config.py                # Settings (variables d'env)
├── database.py               # Engine/session SQLite (SQLModel)
├── models.py                  # Manga, SyncLog
├── routers/pages.py            # Toutes les routes (pages + actions)
├── services/
│   ├── mangaupdates_client.py  # Décodage URL -> series_id, fetch, search fallback
│   ├── anilist_client.py        # Enrichissement complémentaire
│   ├── suwayomi_client.py        # GraphQL: bibliothèque + nb chapitres téléchargés
│   ├── excel_importer.py         # Import non-destructif de l'Excel
│   ├── matching.py                # Normalisation + fuzzy match (rapidfuzz)
│   ├── sync_service.py             # Orchestration des sources externes
│   └── scheduler.py                 # Jobs périodiques (APScheduler)
├── templates/                       # Jinja2 + HTMX + Pico.css (vendored, pas de CDN)
└── static/
```
