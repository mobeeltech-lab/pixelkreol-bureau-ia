# Instance de TEST « ABI Holding » — Bureau IA

> **Environnement de test, données fictives.** Cette instance sert à valider les 10 agents, le routage et les
> règles de feux (vert / orange / rouge) sur le serveur de PixelKréol, avec des modèles en ligne et le jeu de
> données du dossier `donnees-fictives/`. **Ne jamais y mettre de données réelles d'ABI Holding.**
> Le produit final sera installé chez ABI Holding sur un Mac Pro avec des modèles locaux : ici on ne teste
> pas la performance de ces modèles.

## Ce que contient l'instance

| Conteneur | Rôle | Mémoire max |
|---|---|---|
| `abi_orchestrateur` | les 10 agents + `pk-auto` + `pk-equipe` (le même programme que l'instance PixelKréol) | 256 Mo |
| `abi_postgres` | sa propre base (journal des échanges) | 256 Mo |
| `abi_openwebui` | son propre chat et ses propres comptes, sur `127.0.0.1:3101` | 1 Go |

- Aucun port n'est ouvert sur Internet : seul le chat écoute sur `127.0.0.1:3101`, et c'est le nginx du serveur qui
  le publie en HTTPS sur `https://abi.mebeeltech.online`.
- L'orchestrateur appelle le LiteLLM de l'instance PixelKréol (conteneur `pk_litellm`) : l'instance PixelKréol
  doit donc être démarrée.
- Mémoire réelle attendue : environ 0,8 à 1,2 Go au repos (voir le rapport). Tester avec `docker stats --no-stream`.

**Où taper les commandes :** toutes dans le **terminal Hostinger** (hPanel → VPS → *Terminal*), connecté en
administrateur (`root`). Elles se tapent une par une ; ne jamais coller de mot de passe dans un chat.

## Installation pas à pas

### 1. Vérifier le serveur *(terminal Hostinger)*
```bash
docker ps --format '{{.Names}}' | grep pk_litellm     # doit afficher pk_litellm
free -h                                              # mémoire disponible : viser au moins 1,5 Go libres
```
Si la mémoire est juste, ajouter un fichier d'échange (swap) de 2 Go avant de continuer :
```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### 2. Nom de domaine *(chez le gestionnaire du DNS de `mebeeltech.online`)*
Créer un enregistrement **A** : `abi` → adresse IP du VPS. Vérifier ensuite *(terminal Hostinger)* :
```bash
getent hosts abi.mebeeltech.online
```

### 3. Récupérer le code *(terminal Hostinger)*
Après la fusion de la pull request, utiliser la branche `serveur-propre` (avant : `abi-tests`).
```bash
cd /opt
git clone --branch serveur-propre https://github.com/mobeeltech-lab/pixelkreol-bureau-ia.git abi-bureau-ia
cd /opt/abi-bureau-ia/instances/abi
```

### 4. Créer le fichier de réglages `.env` *(terminal Hostinger)*
```bash
cd /opt/abi-bureau-ia/instances/abi
cp .env.exemple .env && chmod 600 .env
for V in ABI_API_KEY ABI_ADMIN_KEY ABI_POSTGRES_PASSWORD ABI_WEBUI_SECRET_KEY; do
  sed -i "s|^$V=.*|$V=$(openssl rand -hex 24)|" .env
done
```
Ces quatre valeurs sont maintenant des secrets aléatoires. **Ne pas afficher le fichier à l'écran.**

### 5. La clé qui ouvre le LiteLLM *(terminal Hostinger)*
`ABI_LITELLM_KEY` est la clé que l'orchestrateur ABI présente au LiteLLM de PixelKréol.
- **Idéal (à faire plus tard)** : une clé dédiée à ABI, avec une limite de dépense. Le LiteLLM actuel n'a pas de
  base de données et ne sait donc pas créer de clés dédiées : il faudra d'abord lui en ajouter une.
- **Pour démarrer les essais** : réutiliser la clé maître du LiteLLM (même pouvoir que PixelKréol ; acceptable pour
  un essai, à remplacer ensuite). La copie se fait sans l'afficher :
```bash
K=$(grep '^LITELLM_MASTER_KEY=' /opt/pixelkreol-bureau-ia/.env | cut -d= -f2-)
sed -i "s|^ABI_LITELLM_KEY=.*|ABI_LITELLM_KEY=$K|" .env && unset K
```

### 6. Démarrer *(terminal Hostinger)*
La commande `dc` ci-dessous remplace `docker compose` partout dans ce document : elle se place dans `/opt/abi-bureau-ia/instances/abi` (là où sont le `.env` et les
fichiers compose) et vise toujours le projet Docker Compose `abi`, quel que soit le dossier où l'on se trouve. À retaper à chaque nouvelle ouverture du terminal :
```bash
dc() { (cd /opt/abi-bureau-ia/instances/abi && docker compose -p abi --project-directory . "$@"); }
```
```bash
cd /opt/abi-bureau-ia/instances/abi
dc config -q && echo "réglages OK"
dc up -d --build
dc ps
```
Les trois conteneurs doivent être `running` (et `healthy` pour la base et l'orchestrateur). Si
`ghcr.io/open-webui/open-webui:v0.6.18` est introuvable, prendre une version qui existe (jamais `latest`) :
`docker inspect pk_openwebui --format '{{.Config.Image}}'` donne celle de l'instance PixelKréol ; l'écrire dans `.env`
sous `ABI_OPENWEBUI_IMAGE=`, puis relancer `dc up -d`.

Vérifier que les 10 agents sont chargés :
```bash
docker exec abi_orchestrateur python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:4001/health').read().decode())"
```
La réponse doit contenir `"agents":10` et `"litellm":true`.

### 7. Publier en HTTPS *(terminal Hostinger)*
```bash
cp /opt/abi-bureau-ia/instances/abi/nginx/abi.mebeeltech.online.conf /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/abi.mebeeltech.online.conf /etc/nginx/sites-enabled/abi.mebeeltech.online.conf
nginx -t && systemctl reload nginx
certbot --nginx -d abi.mebeeltech.online -m mobeeltech@gmail.com --agree-tos --redirect
```
(Si le dossier `/etc/nginx/sites-available` n'existe pas, copier le fichier dans `/etc/nginx/conf.d/` et
ne pas faire la ligne `ln`.) Ouvrir ensuite `https://abi.mebeeltech.online` : la page de connexion s'affiche.

### 8. Créer le compte administrateur, puis fermer les inscriptions *(terminal + navigateur)*
Les inscriptions sont **fermées par défaut**. Le temps de créer le compte administrateur :
```bash
cd /opt/abi-bureau-ia/instances/abi
sed -i 's|^ABI_OPENWEBUI_SIGNUP=.*|ABI_OPENWEBUI_SIGNUP=true|' .env && dc up -d
```
Dans le navigateur, créer le compte (le premier compte est administrateur). Puis refermer **tout de suite** :
```bash
sed -i 's|^ABI_OPENWEBUI_SIGNUP=.*|ABI_OPENWEBUI_SIGNUP=false|' .env && dc up -d
```
Vérifier en navigation privée que la page n'offre plus de création de compte. Les autres testeurs sont créés
depuis l'administration d'Open WebUI.

### 9. Premiers essais
Dans le chat, choisir le modèle `pk-auto` (ou `pk-equipe` pour plusieurs agents) et coller le contenu des fichiers de
`donnees-fictives/` selon l'essai (échéancier, mails, baux…). Exemples :
- « Prépare l'appel de fonds hors d'eau pour les Oliviers » (colle `echeancier-vefa.md` et `attestations-avancement.md`) ;
- « Trie ces mails » (colle `mails-entrants.md`) ; « Révise le loyer de BAIL-A selon l'IRL » (colle `baux-et-quittances.md`).

## Démarrage, arrêt, mise à jour *(terminal Hostinger, dossier `/opt/abi-bureau-ia/instances/abi`)*

| Action | Commande | Effet sur les données |
|---|---|---|
| Démarrer | `dc up -d` | — |
| Mettre en pause | `dc stop` | rien n'est perdu |
| Arrêter (retirer les conteneurs) | `dc down` | comptes et historique conservés (volumes) |
| Mettre à jour | `cd /opt/abi-bureau-ia && git pull && cd instances/abi && dc up -d --build` | conservées |
| Voir les journaux | `dc logs --tail 50 abi_orchestrateur` | — |

## Retour arrière *(terminal Hostinger)*
Avant une mise à jour, sauvegarder le chat :
```bash
D=$(date +%Y%m%d-%H%M); mkdir -p /opt/pk-sauvegardes
docker run --rm -v abi_openwebui_data:/d:ro -v /opt/pk-sauvegardes:/s alpine:3 tar -czf /s/abi-openwebui-$D.tgz -C /d .
```
Pour revenir à la version précédente du code :
```bash
cd /opt/abi-bureau-ia && git log --oneline -5          # repérer l'ancienne version
git checkout <numéro-de-la-version-précédente>
cd instances/abi && dc up -d --build
```
(Pour repartir de la version courante : `git checkout serveur-propre`.)

## Désinstallation complète *(terminal Hostinger)*
Attention : `down -v` efface **les données de l'instance ABI** (comptes, historique, base). Il ne touche jamais aux
volumes de l'instance PixelKréol (leurs noms commencent par `pixelkreol-`).
```bash
cd /opt/abi-bureau-ia/instances/abi
dc down -v
docker image rm abi/orchestrateur:2
rm -f /etc/nginx/sites-enabled/abi.mebeeltech.online.conf /etc/nginx/sites-available/abi.mebeeltech.online.conf
nginx -t && systemctl reload nginx
certbot delete --cert-name abi.mebeeltech.online
cd /opt && rm -rf abi-bureau-ia
```
Il reste ensuite à supprimer l'enregistrement DNS `abi`.

## Lecture du NAS du client (facultatif, éteinte par défaut)
L'Archiviste peut chercher et lire des extraits de documents d'un NAS, **en lecture seule**, via Tailscale et un montage SMB.
Procédure complète, coupure d'urgence et désinstallation : **[NAS-LISEZMOI.md](NAS-LISEZMOI.md)**.

## Standard téléphonique IA (messages vocaux, facultatif, éteint par défaut)
Un service séparé relève une boîte mail dédiée (messagerie vocale de l'opérateur → e-mail), transcrit les messages **sur le serveur**, les fait classer par la
Secrétaire et prévient la bonne personne par e-mail, avec une tâche de rappel. Il ne rappelle et n'écrit jamais à un client.
Procédure, questions à poser au client, texte du répondeur : **[STANDARD-LISEZMOI.md](STANDARD-LISEZMOI.md)**.

## Le Spécialiste Prompt (agent n° 10) et les modèles de demandes
Le Spécialiste Prompt (`pk-10-specialiste-prompt`) ne traite aucun dossier : il transforme une demande brouillon en demande claire adressée au bon agent
(nom exact `pk-NN-id`, ou `pk-equipe` si plusieurs métiers), avec les 5 ingrédients (contexte, tâche, pièces à fournir, forme du résultat, limites).
Il travaille seulement sur le texte qu'on lui donne : aucun outil, ni NAS ni MCP. Écrire par exemple : « améliore ma demande : relance les gens qui n'ont pas payé ».
La liste des agents qu'il connaît est **générée par l'orchestrateur à partir du registre** (champ `inclure_annuaire: true`, réservé à cet agent) : rien à recopier.
Les 10 **modèles de demandes** à compléter sont dans **[modeles-de-demandes.txt](modeles-de-demandes.txt)** ; à enregistrer à la main dans Open WebUI
(Espace de travail > Prompts), car le format d'un fichier d'import n'a pas pu être vérifié.

**Mise à jour d'une instance déjà installée** *(terminal Hostinger)* : le code de l'orchestrateur change (génération de la liste des agents), il faut donc reconstruire :
```bash
cd /opt/abi-bureau-ia && git pull && cd instances/abi && dc up -d --build abi_orchestrateur
docker exec abi_orchestrateur python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:4001/health').read().decode())"
```
La réponse doit contenir `"agents":10`.

## Règle de routage particulière
- **Bail + relecture → Juriste.** Dès qu'une demande contient « bail » (ou « baux ») ET un verbe de relecture (relis, relire, relecture,
  « vérifie les clauses »…), `pk-auto` choisit le Juriste, même si la demande parle aussi de loyer, de locataire ou d'IRL
  (règle `routage_prioritaire` du Juriste dans `agents/registre.yaml`). Sans verbe de relecture, un bail reste au Gestionnaire locatif.

## Limites connues
- **Clé LiteLLM dédiée** : impossible tant que le LiteLLM n'a pas de base de données (voir étape 5).
- **Agents en lecture seule** : le dossier `agents/` est monté en lecture seule ; l'ajout d'un agent par l'API
  (`/admin/agents`) est donc refusé. Modifier `agents/registre.yaml` puis `dc restart abi_orchestrateur`.
- **Les agents ne lisent pas les dossiers du serveur** : ils travaillent sur ce qui est collé ou joint dans le chat.
- **Noms des modèles** : ils gardent le préfixe technique `pk-` (`pk-auto`, `pk-04-appels-de-fonds`…).
- **Modèles en ligne** : c'est le contraire de la promesse finale (« aucune donnée dans le cloud ») ; d'où les données fictives.
- **Plafonds R261-14 et taux** : rappelés dans les consignes pour le test ; à faire valider par le conseil d'ABI Holding.
