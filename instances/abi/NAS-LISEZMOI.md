# Lecture du NAS d'ABI Holding par l'Archiviste — procédure serveur

> **Environnement de test.** L'Archiviste (et les autres agents ABI quand ils ont besoin d'une pièce) peuvent
> **chercher et lire des extraits** de documents du NAS du client, **en lecture seule**. Rien ne peut être écrit,
> déplacé, renommé ni supprimé. Par défaut cette fonction est **éteinte** (`ABI_NAS_ACTIF=0`).

## Comment ça marche (et pourquoi c'est sûr)

```
NAS du client ──(Tailscale : réseau privé chiffré, aucun port ouvert)── VPS ──(montage SMB lecture seule)── /mnt/nas-abi
                                                                                  └─ conteneur abi_orchestrateur (/nas en :ro)
```

La lecture seule est imposée à **quatre niveaux** indépendants :
1. **le compte `bureau-ia` du NAS** n'a que le droit de lecture, sur un seul dossier de test ;
2. **le montage SMB** du serveur est en lecture seule (`ro`) ;
3. **le conteneur** voit le partage en lecture seule (`:ro`) et n'est relié au NAS que si on l'active ;
4. **le programme** n'écrit jamais dans le NAS (même pas une tentative : vérifié par des tests) et refuse de sortir du dossier
   (`../`, chemins absolus, liens symboliques).

Autres protections : les **dossiers exclus** (RH, paie, pièces d'identité… réglable) sont invisibles pour les agents ; seul un
**extrait** utile est envoyé au modèle, jamais un document entier ; un **journal** note qui a demandé quel fichier et quand,
jamais le contenu ; l'index (SQLite) est dans un volume dédié ABI, mis à jour **chaque nuit** (02:30, heure de Paris) et **à la demande**.

**Où taper les commandes.** Les commandes précédées de *(terminal Hostinger)* se tapent dans le terminal de l'hébergeur
(hPanel → VPS → *Terminal*), connecté en administrateur (`root`). Les étapes *(sur le NAS)* se font dans l'interface web du NAS,
par le client ou son informaticien.

---

## Étape 1 — Préparer le NAS *(sur le NAS, avec le client)*

### 1.1 Installer l'application Tailscale sur le NAS
Tailscale relie le NAS et le serveur dans un réseau privé chiffré, sans ouvrir aucun port sur Internet. Un compte Tailscale gratuit
suffit (le créer avec une adresse de PixelKréol, sur https://login.tailscale.com).

- **Synology (DSM 7)** : *Centre de paquets* → chercher « Tailscale » → *Installer* → ouvrir l'application et cliquer sur le lien de
  connexion pour rattacher le NAS à votre compte Tailscale. Si le paquet n'apparaît pas dans votre version de DSM, utiliser la page
  Synology de Tailscale (https://tailscale.com/kb/1131/synology) qui explique l'installation manuelle du paquet `.spk`.
- **QNAP (QTS / QuTS hero)** : *App Center* → catégorie *Communications* → *Tailscale* → *Installer* (disponible pour les modèles
  x86 64 bits et ARM 64 bits). Sinon, installer le fichier `.qpkg` avec l'option d'installation manuelle de l'App Center
  (https://tailscale.com/kb/1273/qnap).
- **Autre marque** : voir https://tailscale.com/kb/1131/synology et la liste des appareils pris en charge sur tailscale.com.

Pour finir : dans la console Tailscale (https://login.tailscale.com/admin/machines), repérer le NAS et noter son **adresse en
`100.x.y.z`** (ou son nom). Cliquer sur les trois points du NAS → *Disable key expiry* (sinon il se déconnecterait au bout de 6 mois).

### 1.2 Créer un compte dédié « bureau-ia » en lecture seule
- **Synology** : *Panneau de configuration* → *Utilisateur et groupe* → *Créer* un utilisateur `bureau-ia` avec un mot de passe long.
  À l'étape *Permissions des dossiers partagés*, cocher **Lecture seule** (et **Aucun accès** pour tous les autres dossiers) sur le seul
  dossier de test. À l'étape *Permissions d'application*, **refuser tout** (ni DSM, ni File Station, ni FTP…) sauf **SMB**.
- **QNAP** : *Panneau de configuration* → *Privilèges* → *Utilisateurs* → *Créer un utilisateur* `bureau-ia`. Dans *Modifier les
  permissions des dossiers partagés*, choisir **Lecture seule** sur le dossier de test et **Refuser l'accès** sur les autres.
  Désactiver pour ce compte les autres services (FTP, WebDAV, interface d'administration).

**Dossier de test :** demander au client de créer (ou désigner) UN dossier partagé contenant seulement quelques dizaines de documents
représentatifs, sans dossier RH/paie. C'est ce dossier, et lui seul, que le compte pourra lire.

### 1.3 SMB et pare-feu du NAS
- Vérifier que le service **SMB** est activé et que la version minimale est **SMB 3** (Synology : *Services de fichiers* → *SMB* →
  *Paramètres avancés* ; QNAP : *Services réseau et de fichiers* → *Win/Mac/NFS* → *Options avancées*).
- Si le pare-feu du NAS est activé, y autoriser le port **445** seulement pour les adresses Tailscale (`100.64.0.0/10`).
- Idéal (facultatif) : dans la console Tailscale, *Access controls*, n'autoriser que le serveur de PixelKréol à joindre le NAS, et
  seulement sur le port 445.

---

## Étape 2 — Installer Tailscale sur le VPS et l'appairer *(terminal Hostinger)*
```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=vps-pixelkreol --accept-dns=false
```
La commande affiche une adresse `https://login.tailscale.com/a/…` : l'ouvrir dans un navigateur et se connecter au **même compte**
Tailscale que le NAS. Puis vérifier que le NAS est visible :
```bash
tailscale status
ping -c 2 100.x.y.z        # remplacer par l'adresse du NAS vue à l'étape 1.1
```
Dans la console Tailscale, désactiver aussi l'expiration de clé pour `vps-pixelkreol` (*Disable key expiry*).

---

## Étape 3 — Monter le partage en lecture seule *(terminal Hostinger)*

### 3.1 Outil de montage
```bash
apt-get update && apt-get install -y cifs-utils
mkdir -p /mnt/nas-abi
```

### 3.2 Identifiants : un fichier réservé à root, hors du dépôt
Les identifiants ne sont **jamais** dans le dépôt ni dans `.env`. Cette suite de commandes demande le mot de passe sans l'afficher
ni le garder dans l'historique :
```bash
install -m 600 /dev/null /root/.nas-abi-identifiants
read -r -p "Compte NAS (bureau-ia) : " U; read -r -s -p "Mot de passe : " P; echo
printf 'username=%s\npassword=%s\n' "$U" "$P" > /root/.nas-abi-identifiants; unset U P
ls -l /root/.nas-abi-identifiants          # doit afficher -rw------- (600)
```

### 3.3 Montage automatique au démarrage
Remplacer `100.x.y.z` par l'adresse du NAS et `PARTAGE` par le nom du dossier partagé de test :
```bash
echo '//100.x.y.z/PARTAGE /mnt/nas-abi cifs credentials=/root/.nas-abi-identifiants,ro,uid=1000,gid=1000,file_mode=0444,dir_mode=0555,vers=3.0,iocharset=utf8,nofail,_netdev,x-systemd.requires=tailscaled.service,noserverino 0 0' >> /etc/fstab
systemctl daemon-reload
mount /mnt/nas-abi
```
(`ro` = lecture seule ; `nofail` = le serveur démarre normalement même si le NAS est injoignable.)

### 3.4 Vérifier — la lecture marche, l'écriture est impossible
```bash
ls /mnt/nas-abi | head
touch /mnt/nas-abi/essai.txt        # DOIT échouer : « Read-only file system » ou « Permission denied »
```
Si `touch` réussit, **arrêter là**, démonter (`umount /mnt/nas-abi`) et corriger les droits du compte sur le NAS.

---

## Étape 4 — Activer la lecture du NAS dans l'instance ABI *(terminal Hostinger)*

### 4.1 Mettre le code à jour (une seule fois)
```bash
cd /opt/pk-abi && git pull
cd /opt/pk-abi/instances/abi
docker compose up -d --build           # reconstruit l'orchestrateur (nouvel outil) ; sans effet tant que le NAS n'est pas activé
```

### 4.2 Régler et activer
```bash
cd /opt/pk-abi/instances/abi
reglage() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
reglage ABI_NAS_CHEMIN /mnt/nas-abi
reglage ABI_NAS_ACTIF 1
reglage COMPOSE_FILE docker-compose.yml:docker-compose.nas.yml
docker compose up -d --no-deps abi_orchestrateur       # relance SEULEMENT l'orchestrateur ; le chat et la base ne bougent pas
docker compose logs --tail 20 abi_orchestrateur         # chercher « Outil documents actif (lecture seule) »
```
(La première ligne définit `reglage`, une petite aide qui modifie ou ajoute une ligne dans `.env`.)

Autres réglages dans `.env` (même aide `reglage NOM valeur`, puis relancer comme ci-dessus) : `ABI_NAS_EXCLUS` (dossiers invisibles, séparés par des virgules ; vide =
liste par défaut RH / paie / pièces d'identité), `ABI_NAS_TAILLE_MAX_MO`, `ABI_NAS_FICHIERS_MAX`, `ABI_NAS_CARACTERES_MAX`,
`ABI_NAS_DELAI`, `ABI_NAS_NUIT`, `ABI_NAS_MODE`. Voir `.env.exemple`.

### 4.3 Construire l'index et suivre son état
La première indexation démarre toute seule quelques secondes après le lancement. Pour la suivre ou la relancer à la demande :
```bash
cd /opt/pk-abi/instances/abi
CLE=$(grep '^ABI_ADMIN_KEY=' .env | cut -d= -f2-)
docker exec -e K="$CLE" abi_orchestrateur python -c "import os,urllib.request as u;r=u.Request('http://127.0.0.1:4001/admin/nas');r.add_header('Authorization','Bearer '+os.environ['K']);print(u.urlopen(r).read().decode())"
docker exec -e K="$CLE" abi_orchestrateur python -c "import os,urllib.request as u;r=u.Request('http://127.0.0.1:4001/admin/nas/reindexer',data=b'',method='POST');r.add_header('Authorization','Bearer '+os.environ['K']);print(u.urlopen(r).read().decode())"
unset CLE
```
La première commande affiche le nombre de fichiers indexés ; la seconde relance une mise à jour (les fichiers inchangés ne sont pas relus).

### 4.4 Essayer
Dans le chat ABI, choisir l'agent *Archiviste* (ou `pk-auto`) et demander par exemple : « Retrouve le PV de livraison du lot B12 »,
« Quelles pièces contient le dossier de M. Fictif ? », « Que dit l'acte du lot A02 sur l'échéancier ? ». L'agent doit répondre avec le
**chemin exact** et un court extrait, ou dire « introuvable dans le NAS ». Pour voir qui a consulté quoi (jamais le contenu) :
```bash
docker exec abi_orchestrateur python -c "import sqlite3;c=sqlite3.connect('/app/data/index.sqlite');[print(r) for r in c.execute('select ts,demandeur,agent,action,substr(cible,1,90),ok from journal order by id desc limit 20')]"
```

---

## COUPURE D'URGENCE — une seule commande *(terminal Hostinger)*
```bash
bash /opt/pk-abi/instances/abi/nas-coupure-urgence.sh
```
Elle met `ABI_NAS_ACTIF=0`, retire le montage du NAS dans le conteneur (relance **seulement** `abi_orchestrateur`), **démonte** le
partage et met sa ligne en commentaire dans `/etc/fstab` (il ne revient pas au redémarrage). Elle ne supprime rien : ni l'index, ni le
journal, ni les documents du NAS. Pour couper aussi le réseau privé : `bash …/nas-coupure-urgence.sh --tailscale`.

**Pour réactiver ensuite** : décommenter la ligne dans `/etc/fstab`, `systemctl daemon-reload`, `mount /mnt/nas-abi`, puis refaire l'étape 4.2.

*Le client peut aussi couper de son côté, à tout moment* : désactiver le compte `bureau-ia` sur le NAS, ou retirer le NAS du réseau
Tailscale (console Tailscale → le NAS → *Remove*).

## Désactiver sans tout démonter *(terminal Hostinger)*
```bash
cd /opt/pk-abi/instances/abi
sed -i 's|^ABI_NAS_ACTIF=.*|ABI_NAS_ACTIF=0|' .env && sed -i '/^COMPOSE_FILE=/d' .env
docker compose up -d --no-deps --force-recreate abi_orchestrateur
```

## Désinstallation complète *(terminal Hostinger, puis sur le NAS)*
```bash
# 1. couper l'accès (désactive l'outil, démonte le partage)
bash /opt/pk-abi/instances/abi/nas-coupure-urgence.sh
# 2. supprimer la ligne du partage dans /etc/fstab, les identifiants et le dossier de montage
sed -i '/\/mnt\/nas-abi/d' /etc/fstab && systemctl daemon-reload
shred -u /root/.nas-abi-identifiants
rmdir /mnt/nas-abi
# 3. supprimer l'index et le journal des consultations (volume dédié ABI ; ne touche à aucune autre donnée)
cd /opt/pk-abi/instances/abi && docker volume rm abi_nas_index
# 4. retirer les réglages NAS de .env
sed -i '/^ABI_NAS_/d;/^COMPOSE_FILE=/d' .env
# 5. désinstaller Tailscale du serveur
tailscale logout && systemctl disable --now tailscaled && apt-get purge -y tailscale
```
*(Sur le NAS, par le client)* : supprimer le compte `bureau-ia`, désinstaller l'application Tailscale, et retirer le serveur
`vps-pixelkreol` et le NAS de la console Tailscale (https://login.tailscale.com/admin/machines).

---

## Limites connues
- **Pas d'OCR** : un PDF scanné (image sans texte) est trouvé par son nom mais son contenu n'est pas lu ; l'agent le dit.
- **Formats lus** : PDF (texte intégré), Word `.docx`, Excel `.xlsx`, `.txt`, `.csv`. Les anciens `.doc` / `.xls`, les images et les archives
  sont trouvés par leur nom seulement.
- **Fichiers volumineux** : au-delà de la taille réglée (15 Mo par défaut), le fichier est trouvé par son nom mais pas lu.
- **Index** : un document ajouté dans la journée n'est cherchable par son contenu qu'après la mise à jour (la nuit, ou à la demande,
  étape 4.3) ; il peut toutefois être lu directement par son chemin.
- **Réponse en bloc** : quand un agent consulte le NAS, sa réponse arrive d'un bloc au lieu de s'afficher mot à mot.
- **Modèles sans appel d'outils** : l'orchestrateur bascule automatiquement sur une recherche dans l'index d'après la question
  (`ABI_NAS_MODE=repli` pour le forcer, utile avec des modèles locaux sur le Mac Pro) ; la qualité dépend alors de la question posée.
- **Noms de documents inventés par un modèle** : la réponse est complétée d'un avertissement « Non vérifié dans le NAS » si elle cite un
  fichier que le NAS ne contient pas.
- **Exclusions par mot entier** : « rh » exclut « Dossiers RH » mais pas « Rhône ». Une exclusion est insensible aux accents et aux majuscules.
- **Mémoire** : le conteneur passe à 384 Mo quand le NAS est activé (lecture des PDF).
- **Plus tard, sur le Mac Pro du client** : l'outil fonctionne quel que soit le chemin du partage (`ABI_NAS_CHEMIN`) ; l'index conserve des
  chemins relatifs au dossier du NAS.
