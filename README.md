# Manga Tracker

Application de suivi de mangas/manhwas/manhwas adultes, pensée pour compléter une
bibliothèque [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) + [Komga](https://komga.org/).

- Suwayomi fait foi pour la liste des mangas suivis : la synchronisation (manuelle ou
  automatique toutes les 24h) importe tout nouveau titre de la bibliothèque, met à jour le
  nombre de chapitres téléchargés, et copie les catégories Suwayomi (tes propres groupes
  "À suivre"/"Terminé"/...) sur chaque manga.
- Import initial (ou complémentaire) du fichier Excel de suivi (colonnes `Libraries`,
  `Titre Anglais`, `Titre Dossier Serveur`, `MangaUpdates.com (URL)`, `Last Scan`, `Statut
  du Manga`) — non-destructif, utile surtout pour le premier peuplement.
- Synchronisation automatique avec [MangaUpdates](https://www.mangaupdates.com/) (statut,
  auteur/artiste, genres, titres alternatifs, couverture, dernier chapitre) via son API
  publique — pas de clé requise. Le lien MangaUpdates est éditable à tout moment sur la
  fiche d'un manga (utile si le rattachement automatique s'est trompé, ou pour un titre
  fraîchement importé depuis Suwayomi qui n'en a pas encore).
- Enrichissement complémentaire via [AniList](https://anilist.co/) quand MangaUpdates ne
  suffit pas (couverture, titres alternatifs).
- Interface web simple (FastAPI + Jinja2 + HTMX, SQLite), packagée en image Docker. Le
  tableau de bord distingue le **Type** (Manga/Pornhwa, propre à cette appli) des
  **Catégories Suwayomi** (tes groupes de bibliothèque) comme deux filtres séparés.

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
| `SUWAYOMI_SYNC_INTERVAL_HOURS` | Fréquence de réconciliation/import Suwayomi | 24 |
| `ENABLE_SCHEDULER` | Désactive les jobs automatiques (sync manuelle uniquement) | true |
| `LIBRARY_ROOT` | Chemin (côté conteneur) du partage NAS monté en lecture seule | `/library` |

### Partage NAS ("Dossier serveur")

Pour que le champ "Dossier serveur" d'une fiche manga propose une vraie liste des dossiers
existants sur ton NAS (au lieu d'une simple saisie libre), monte le partage en lecture seule
dans le conteneur — il doit contenir exactement deux sous-dossiers, `Manga` et `Pornhwa` :

```yaml
volumes:
  - /mnt/remotes/192.168.1.41_Komga:/library:ro
```

Sur Unraid (template natif, pas docker-compose) : édite le conteneur, **Add another Path**
avec Container Path `/library`, Host Path `/mnt/remotes/192.168.1.41_Komga`, Access Mode
"Read Only". Sans ce montage, le champ reste un simple texte libre — la fonctionnalité est
entièrement optionnelle.

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
4. Peuplement initial : soit importer l'Excel existant (`/import`), soit directement lancer
   une synchronisation Suwayomi (`/suwayomi`, bouton "Synchroniser maintenant") qui importe
   tout depuis la bibliothèque — les deux sont non-destructifs et peuvent se combiner.

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
│   ├── suwayomi_client.py        # GraphQL: bibliothèque, genres, catégories, chapitres
│   ├── excel_importer.py         # Import non-destructif de l'Excel
│   ├── matching.py                # Normalisation + fuzzy match (rapidfuzz)
│   ├── sync_service.py             # Orchestration : reconciliation/auto-import Suwayomi,
│   │                                 sync MangaUpdates, enrichissement AniList
│   └── scheduler.py                 # Jobs périodiques (APScheduler)
├── templates/                       # Jinja2 + HTMX + Pico.css (vendored, pas de CDN)
└── static/
```
