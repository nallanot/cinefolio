# Cinéfolio

Application personnelle de suivi de films et séries, inspirée des fonctionnalités de TV Time.

## Démarrage Docker

1. `cp .env.example .env`
2. Ajouter votre clé TMDB dans `.env`.
3. `docker compose up -d --build`

Par défaut, le service écoute sur `127.0.0.1:8098` uniquement sur le serveur. Pour un accès depuis le LAN, modifier le binding du port dans `compose.yaml` (par exemple `192.168.1.151:8098:8000` si cette adresse appartient au serveur Docker).

**Sécurité :** version initiale mono-utilisateur, sans authentification. Ne pas exposer sur Internet avant l'ajout d'un contrôle d'accès.

Données conservées dans le volume Docker `cinefolio_data` (SQLite).

Métadonnées fournies par TMDB. Projet indépendant, non affilié à TV Time ni TMDB.
