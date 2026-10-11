# MCP — outils externes pour certains agents (client MCP de l'orchestrateur)

> ## ⚠ À lire avant tout : où partent les données lues
> Quand un agent lit une page web, un agenda, un mail ou un fichier Drive, **le texte lu est envoyé au modèle qui répond, c'est-à-dire à un
> fournisseur de modèles EN LIGNE** (via LiteLLM). Il quitte donc le serveur.
> - **Jamais de données de clients** (devis, mails, documents, agenda de rendez-vous clients) dans le périmètre MCP **tant que l'on n'est pas
>   passé aux modèles locaux du Mac Pro**.
> - **Jamais d'ABI.** L'instance ABI garde MCP **coupé** : pas de `mcp.yaml`, pas de variable `PK_MCP_*`, et un garde-fou dans le code refuse
>   `PK_MCP_ACTIF=1` pour toute autre marque que PixelKréol.
> - Pour l'instant, ne brancher que ce qui peut être lu par un fournisseur tiers : pages publiques, et vos propres dossiers/libellés **dédiés**
>   (libellé Gmail « bureau-ia », un dossier Drive de travail) où vous ne mettez rien de confidentiel.

## En bref
- **Éteint par défaut.** Sans `PK_MCP_ACTIF=1` et sans `agents/mcp.yaml`, l'orchestrateur est celui d'avant (testé).
- Un **client MCP** dans l'orchestrateur (HTTP seulement ; aucun programme lancé par l'orchestrateur). Chaque serveur MCP tourne dans son propre
  conteneur, sur le réseau interne, **sans port publié**.
- Un agent n'a accès à un outil que si **sa fiche** déclare `outils: [mcp]` **et** que `agents/mcp.yaml` nomme cet outil et cet agent.
- **Trois classes de risque**, fixées par vous dans `mcp.yaml` (jamais par le serveur) :

| Classe | Exemples | Ce qui se passe |
|---|---|---|
| **vert** (lecture) | lire une page, lister l'agenda, chercher un mail, lire un fichier | exécuté tout de suite, journalisé |
| **orange** (écriture) | brouillon de mail, création d'événement | **jamais exécuté** : mis en attente 24 h, Will valide ou refuse |
| **rouge** (envoi, engagement) | envoyer un mail | **jamais exécuté** par le Bureau IA : l'agent prépare le texte, Will l'envoie lui-même |

- **Aucun outil de paiement** (paiement, virement, remboursement, achat, abonnement…) : refusé au chargement du fichier, même s'il est dans la
  liste blanche, et refusé encore à l'appel.
- Un nom d'outil qui ressemble à une écriture ne peut pas être classé « vert » ; un nom qui ressemble à un envoi ne peut pas être classé en dessous
  de « rouge » (relevé automatiquement, avec un message dans les journaux).
- Les « indices » donnés par un serveur (lecture seule, destructif…) sont **ignorés** : un serveur peut mentir.

## Ce que fait le code (et ce que vous gardez à faire)
| Garde | Détail |
|---|---|
| Liste blanche | outil par outil, agent par agent ; tout le reste n'existe pas |
| Adresses web | HTTPS, domaine dans la liste, jamais d'IP ni de nom interne, jamais de port ni d'identifiants |
| Périmètre Gmail / Drive | `gabarit` (la recherche est enveloppée dans le libellé / le dossier) + `refuser_motifs` (opérateurs refusés) |
| Bornes | délai par serveur, taille du résultat, tours (4) et appels (6) par demande, 30 appels/minute |
| Texte reçu | encadré comme **donnée non fiable**, balises d'appel d'outil retirées, **jamais** transformé en appel d'outil |
| Journal | qui, quand, quel outil, quelle classe, quel statut, quelle taille — **jamais le contenu lu** ; secrets masqués |
| Jetons | on écrit le **nom** de la variable (`jeton_env`), jamais le jeton ; refusé s'il ressemble à une clé |

**Honnêteté sur Gmail et Drive.** Gmail ne permet pas de limiter un jeton à un libellé, ni Drive à un dossier avec un simple jeton. Le périmètre
imposé par l'orchestrateur est une **seconde barrière**, pas la première. La première, c'est la configuration du serveur MCP lui-même :
- **Drive** : utiliser un **compte de service** Google partagé **uniquement** sur le dossier voulu (il ne voit alors rien d'autre).
- **Gmail** : configurer le serveur MCP pour ne renvoyer que les messages du libellé `bureau-ia` (ou, mieux, y faire arriver par un filtre Gmail
  seulement ce que l'on veut faire lire).
Les noms d'outils et d'arguments de `mcp.exemple.yaml` sont des **exemples** : lisez les vrais avec la commande ci-dessous et adaptez.

## Fichiers (étape 1)
| Fichier | Rôle |
|---|---|
| `orchestrateur/mcp_client.py` | le client, la configuration, le journal, la file des actions en attente |
| `orchestrateur/app.py` | branchement (inactif par défaut), `/admin/mcp/etat`, `/admin/mcp/journal` |
| `orchestrateur/Dockerfile` | copie `mcp_client.py` dans l'image |
| `agents/mcp.exemple.yaml` | exemple : pages web, Agenda, Gmail (un libellé), Drive (un dossier) — tout en lecture |
| `docker-compose.mcp.yml` | ajout optionnel (variables et volume) ; n'existe pas pour ABI |

Le code Python est dans l'image : **reconstruire** l'orchestrateur (pas de simple copie à chaud).

## Mise en service (quand vous aurez choisi vos serveurs MCP)
1. Copier les fichiers ci-dessus sur le serveur (`/opt/pixelkreol-bureau-ia`, qui n'est pas une copie git), puis :
   `cd /opt/pixelkreol-bureau-ia && docker compose up -d --build pk_orchestrateur`
   → **comportement identique à avant** (MCP éteint). Vérifier : `curl -s localhost:4001/health` ne contient pas `"mcp"`.
2. Lancer les serveurs MCP voulus dans des conteneurs à part (réseau interne `pixelkreol_net`, aucun port publié). Choisir des serveurs
   existants fiables (licence, mises à jour, étendue des droits demandés) — à valider ensemble avant d'installer quoi que ce soit.
3. Copier `agents/mcp.exemple.yaml` en `agents/mcp.yaml`, **adapter** noms d'outils, domaines, libellé, dossier. Vérifier :
   `docker compose exec pk_orchestrateur python mcp_client.py verifier`
   Lister les vrais outils d'un serveur : `docker compose exec pk_orchestrateur python mcp_client.py lister pages`
4. Mettre les jetons dans `.env` (noms : `PK_MCP_PAGES_JETON`, `PK_MCP_AGENDA_JETON`, `PK_MCP_COURRIER_JETON`, `PK_MCP_DRIVE_JETON`) et ajouter
   `COMPOSE_FILE=docker-compose.yml:docker-compose.mcp.yml` (en gardant ce qui y figure déjà, ex. `:docker-compose.caddy.yml`).
5. Donner `outils: [mcp]` aux seuls agents voulus (dans leur fiche : `agents/registre.yaml` ou `agents/perso.yaml`).
6. `docker compose up -d pk_orchestrateur` — **un redémarrage est nécessaire** pour relire `mcp.yaml`.
7. Essai : `curl -s localhost:4001/health` doit afficher `"mcp":{"actif":true,"outils":N}`. Demander à l'agent concerné une lecture simple, puis
   `curl -s -H "Authorization: Bearer $PK_ADMIN_KEY" localhost:4001/admin/mcp/journal`.

**Retour arrière immédiat** : retirer `docker-compose.mcp.yml` de `COMPOSE_FILE` (ou mettre `PK_MCP_ACTIF=0`) puis `docker compose up -d pk_orchestrateur`.

## Écritures et validation de Will
Une action **orange** est enregistrée (agent, outil, arguments, 24 h de validité) et **non exécutée** ; l'agent répond « en attente de validation ».
`GET /admin/mcp/etat` en indique le nombre. **Étape 2 (à venir dans ce lot)** : routes pour valider ou refuser, page web minimale
(protégée par la clé admin) et notification (n8n ou mail) quand une action attend. **Tant qu'elle n'est pas livrée, ne configurez aucun outil
orange ni rouge** — l'exemple fourni n'en contient aucun.

## Tests
`python -m pytest tests/test_mcp.py` : faux serveur MCP en mémoire (aucun accès réseau) — désactivé par défaut, ABI coupée, liste blanche, classes,
paiement refusé (14 noms), arguments contrôlés (16 adresses refusées), bornes, injection (résultat piégé, description piégée, appel écrit en texte),
journal sans contenu, jetons non divulgués. Étape 3 (à venir) : serveur MCP exposant les agents en lecture seule.
