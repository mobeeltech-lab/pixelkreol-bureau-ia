# Standard téléphonique IA d'ABI Holding — messages vocaux (phase 1)

> **Environnement de test, données fictives.** Cette fonction est **éteinte par défaut** (`ABI_STANDARD_ACTIF=0`) et n'existe même pas
> tant que `docker-compose.standard.yml` n'est pas ajouté à `COMPOSE_FILE`. Elle ne change rien à l'instance PixelKréol ni au reste de
> l'instance ABI. **Ne pas la brancher sur une vraie ligne avant l'accord du client** (voir « Ce qu'il faut obtenir du client »).

## Le principe

La ligne d'ABI **garde son opérateur**. Quand personne ne décroche (ou hors horaires), l'appelant laisse un message. L'opérateur envoie ce
message vocal en **pièce jointe audio** sur une **boîte mail dédiée** (fonction « messagerie vocale vers e-mail » de la plupart des
opérateurs et des box). Le standard :

```
appelant ──► opérateur ──(mail + audio)──► boîte dédiée ──IMAP──► [abi_standard] ──► transcription EN LOCAL (faster-whisper, CPU)
                                                                         │                      (aucune voix ne quitte le serveur)
                                                                         ├──► Secrétaire du Bureau IA : qui, pourquoi, catégorie, urgence 1-5, résumé, action, agent
                                                                         ├──► e-mail de synthèse + transcription ──SMTP──► la bonne personne (par catégorie)
                                                                         ├──► tâche de rappel (urgence 5 : alerte immédiate)
                                                                         └──► journal (numéro masqué) ; l'audio est supprimé du serveur
```

**Ce que le standard ne fait JAMAIS** : rappeler un client, écrire à un client, répondre à un appelant, décrocher un appel. Il **prépare** ;
toute suite donnée à l'appelant est une décision et une action **humaines**. Les adresses qui reçoivent les mails viennent **uniquement des
réglages** (jamais du contenu d'un message vocal ni de l'adresse de l'appelant) : un contrôle à l'envoi refuse toute autre adresse.

Ce qu'il fait des cas gênants : message vide, inaudible, trop long, fichier qui n'est pas de l'audio, mail sans pièce jointe, mail
illisible → **une personne est prévenue quand même** (« Message à écouter ») avec une tâche pour aller écouter l'original ; rien n'est avalé
en silence, rien ne plante.

### Pourquoi un service à part (et pas dans l'orchestrateur)
- **Mémoire** : l'orchestrateur est limité à 256–384 Mo ; un modèle de voix demande de l'ordre de 0,3 à 1 Go. Les mélanger obligerait à
  gonfler le conteneur du chat en permanence.
- **Isolation** : si la transcription sature ou plante, le chat et les agents continuent. Le standard a ses propres limites (mémoire, processeur,
  pas de swap), un système de fichiers en lecture seule, aucun port, aucun accès au NAS.
- **Dépendances** : les bibliothèques audio (ctranslate2, onnxruntime) restent hors de l'image de l'orchestrateur, qui n'est pas modifiée.
- **Désactivable** : un fichier compose séparé ; sans lui, rien n'existe. L'orchestrateur ne contient pas une ligne de ce module.

---

## Ce qu'il faut obtenir du client (à demander avant tout)

1. **Opérateur et offre** de la ligne (Orange, SFR, Bouygues, OVH, Free, standard virtuel…) et **le numéro** concerné.
2. **« Messagerie vocale vers e-mail »** : l'opérateur propose-t-il d'envoyer le message vocal **avec le fichier audio en pièce jointe** (et pas
   seulement un texto ou un lien) ? Formats habituels : WAV, MP3, M4A, AMR (tous lus). Si l'opérateur n'envoie qu'un lien, cette phase 1 ne marche pas.
3. **Une boîte mail dédiée** (par exemple `standard@abi-holding.fr`), avec accès **IMAP**, et pour chaque boîte : serveur, port, mot de passe
   d'application. Idéalement une règle de **suppression automatique après 30 jours** (voir « Données personnelles »).
4. **L'adresse d'envoi de l'opérateur** (expéditeur des mails de messagerie vocale) → `ABI_STANDARD_EXPEDITEURS_OK` (le standard ignore tout autre expéditeur).
5. **Un compte d'envoi** (SMTP) pour les synthèses, ou le serveur de messagerie du client.
6. **Qui est prévenu, par catégorie** : vente, location, appel de fonds, chantier, fournisseur, administration, autre ; **un destinataire par défaut**
   (obligatoire) ; **qui est d'astreinte** pour l'urgence 5 (sinistre, accident de chantier, dégât des eaux).
7. **Les horaires d'ouverture** (jours et heures) : ils servent à dater les rappels et à signaler « reçu hors horaires ».
8. **Le texte du répondeur** (ci-dessous), à faire valider par le client et son conseil, et à enregistrer par l'opérateur.
9. **L'accord écrit du client** pour le traitement automatisé des messages (information des appelants, durée de conservation, interlocuteur RGPD).

## Texte d'annonce du répondeur à proposer (obligation de transparence)

À compléter par le client (zones entre crochets) et à faire relire par son conseil. Durée de lecture : environ 40 secondes ; si l'opérateur limite
la durée de l'annonce, garder au minimum les phrases en gras.

> « Bienvenue chez ABI Holding. Nos bureaux sont fermés ou toutes nos lignes sont occupées. Après le bip, laissez votre nom, votre numéro de
> téléphone et l'objet de votre appel. **Votre message sera enregistré, transcrit puis analysé par un assistant automatique, avant d'être transmis à
> la personne concernée chez ABI Holding. La transcription est conservée trente jours, puis supprimée.** Vous pouvez demander l'accès à vos données ou
> leur suppression en écrivant à [adresse du référent données personnelles]. En cas d'urgence — dégât des eaux, accident, sinistre — appelez le
> [numéro d'astreinte]. Merci de votre appel. »

Points à vérifier avec le client : le numéro d'astreinte existe vraiment et est tenu ; la durée annoncée (trente jours) est celle réglée
(`ABI_STANDARD_CONSERVATION_JOURS`) **et** celle de la suppression automatique de la boîte dédiée ; le référent données personnelles est nommé.

---

## Installation pas à pas

Toutes les commandes se tapent dans le **terminal Hostinger** (hPanel → VPS → *Terminal*), en administrateur (`root`), **une par une**. Ne jamais coller
un mot de passe dans un chat ni l'afficher à l'écran.

### 0. Préparer le terminal *(terminal Hostinger)*
Le dépôt est dans `/opt/abi-bureau-ia`, la pile ABI tourne sous le nom de projet Docker Compose `abi` et ses réglages sont dans `instances/abi`.
Pour que toutes les commandes ci-dessous visent la même pile, taper **d'abord** (à refaire à chaque nouvelle ouverture du terminal) :
```bash
cd /opt/abi-bureau-ia
dc() { (cd /opt/abi-bureau-ia/instances/abi && docker compose -p abi --project-directory . "$@"); }
dc ps                      # doit lister abi_orchestrateur, abi_postgres, abi_openwebui
```
`dc` remplace `docker compose` : elle se place dans `/opt/abi-bureau-ia/instances/abi` (le `.env` et les chemins de `COMPOSE_FILE` y sont relatifs) et ajoute `-p abi --project-directory .` (même projet que la pile en marche).
Sans cela, Docker Compose pourrait créer un second projet et se plaindre de conteneurs déjà existants.

### 1. Vérifier la mémoire *(terminal Hostinger)*
```bash
free -h                      # viser au moins 1,5 Go « available » (le conteneur est plafonné à 1,25 Go avec le modèle « small »)
docker stats --no-stream     # ce que consomment déjà les conteneurs
```

### 2. Récupérer le code *(terminal Hostinger)*
Le module se trouve sur la branche `abi-standard` ; après la fusion de la pull request, sur `serveur-propre`.
```bash
cd /opt/abi-bureau-ia && git fetch origin && git checkout serveur-propre && git pull
cd instances/abi
ls docker-compose.standard.yml standard/Dockerfile       # les deux fichiers doivent exister
```

### 3. Régler le standard dans `.env` *(terminal Hostinger)*
```bash
cd /opt/abi-bureau-ia/instances/abi
nano .env          # Ctrl+O puis Entrée pour enregistrer, Ctrl+X pour quitter
```
Ajouter (ou compléter) les lignes `ABI_STANDARD_…` décrites dans `.env.exemple` (la liste complète et commentée y est). Au minimum :
`ABI_STANDARD_IMAP_HOTE`, `_IMAP_COMPTE`, `_IMAP_MDP`, `_EXPEDITEURS_OK`, `_SMTP_HOTE`, `_SMTP_EXPEDITEUR`, `_DEST_DEFAUT`, `_DEST_URGENCE`, les `_DEST_<catégorie>`,
`_HORAIRES`, `_JOURS`, `_DOMAINES_OK`. **Laisser `ABI_STANDARD_ACTIF=0` pour l'instant.** (Les réglages se font toujours dans `/opt/abi-bureau-ia/instances/abi/.env`.) Puis ajouter le fichier compose :
```bash
grep '^COMPOSE_FILE' .env || echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.standard.yml' >> .env
# Si la ligne COMPOSE_FILE existait déjà (NAS activé), la compléter au lieu d'en créer une seconde :
sed -i '/^COMPOSE_FILE=/{/docker-compose.standard.yml/!s|$|:docker-compose.standard.yml|}' .env
grep '^COMPOSE_FILE' .env          # doit se terminer par docker-compose.standard.yml
chmod 600 .env
dc config -q && echo "réglages OK"
```
Les mots de passe sont dans `.env` uniquement (droits 600). Éviter les caractères `$` et `"` dans les mots de passe (ou les doubler : `$$`).

### 4. Construire le conteneur *(terminal Hostinger)*
```bash
dc build abi_standard         # plusieurs minutes ; image de l'ordre de 0,5 à 1 Go
```

### 5. Télécharger le modèle de voix, UNE fois *(terminal Hostinger)*
C'est la seule fois où le service parle à Internet pour la voix : il récupère le modèle, puis ne se reconnecte plus (`HF_HUB_OFFLINE=1`).
```bash
dc run --rm -e HF_HUB_OFFLINE=0 abi_standard python -m abi_standard telecharger
```
Le modèle par défaut est `small` (réglage `ABI_STANDARD_WHISPER_MODELE`, plafond mémoire `ABI_STANDARD_MEM=1280m`). Pour le modèle plus léger, mettre `ABI_STANDARD_WHISPER_MODELE=base` dans `.env` (`ABI_STANDARD_MEM=768m` suffit), puis relancer la commande.

### 6. Mesurer sur CE serveur avant d'allumer *(terminal Hostinger)*
Enregistrer un message de test de quelques secondes (téléphone, ordinateur) au format WAV ou MP3, le déposer sur le serveur (`/root/essai.wav`), puis :
```bash
dc run --rm -v /root/essai.wav:/mesure/essai.wav:ro abi_standard \
  python -m abi_standard.mesure --audio /mesure/essai.wav --secondes 60
```
Le programme répète le fichier jusqu'à 60 s et affiche : temps de chargement du modèle, temps de transcription (× le temps réel), pic de mémoire.
Le conteneur est soumis aux **mêmes limites** que le service : un « Killed » signifie qu'il faut plus de mémoire ou un modèle plus léger.
**Noter les chiffres** : ils décident du modèle (`small` ou `base`) et du délai moyen d'un message. Mesure faite sur le VPS d'essai (2 processeurs), pour 60 s d'audio : `small` 16 s, pic de mémoire 675 Mo ; `base` 6 s, pic 387 Mo. Un message d'une minute est donc prêt en une vingtaine de secondes avec `small`, hors attente de la relève.

### 7. Essai à blanc avec une boîte de test *(terminal Hostinger)*
Utiliser d'abord une boîte de TEST (pas celle du client), `ABI_STANDARD_ACTIF=1`, des destinataires internes à PixelKréol :
```bash
cd /opt/abi-bureau-ia/instances/abi
sed -i 's|^ABI_STANDARD_ACTIF=.*|ABI_STANDARD_ACTIF=1|' .env
dc run --rm abi_standard python -m abi_standard etat       # « Réglages : complets »
dc up -d abi_standard
dc logs --tail 30 abi_standard                              # « standard démarré : modèle small… »
docker stats --no-stream abi_standard                                   # mémoire réelle au repos
```
Envoyer depuis l'adresse autorisée un mail avec un message vocal **fictif** en pièce jointe à la boîte dédiée. Dans les 2 minutes (`ABI_STANDARD_INTERVALLE`) :
la synthèse arrive chez le destinataire de la catégorie ; `dc exec abi_standard python -m abi_standard journal` montre la ligne (numéro masqué) ;
`… taches` montre la tâche de rappel. Tester aussi : un message qui dit « dégât des eaux » (alerte immédiate), un fichier PDF, un mail sans pièce jointe.

### 8. Brancher la vraie boîte, seulement après accord du client
Changer les réglages IMAP/SMTP et les destinataires dans `.env`, puis `dc up -d abi_standard`. Faire configurer par l'opérateur l'envoi des messages
vocaux vers la boîte dédiée, puis l'annonce du répondeur. Premier jour : surveiller (`dc logs -f --tail 50 abi_standard`, Ctrl+C pour quitter l'affichage).

## Utilisation courante *(terminal Hostinger, après l'étape 0)*

| Besoin | Commande |
|---|---|
| Tâches de rappel à faire | `dc exec abi_standard python -m abi_standard taches` |
| Marquer une tâche faite | `dc exec abi_standard python -m abi_standard fait 12` |
| Journal des 7 derniers jours (numéros masqués) | `dc exec abi_standard python -m abi_standard journal` |
| État et réglages | `dc exec abi_standard python -m abi_standard etat` |
| Voir ce qui se passe | `dc logs -f --tail 50 abi_standard` |
| **Arrêt immédiat** | `dc stop abi_standard` (rien n'est perdu : les messages restent non lus dans la boîte) |
| Éteindre durablement | `sed -i 's|^ABI_STANDARD_ACTIF=.*|ABI_STANDARD_ACTIF=0|' /opt/abi-bureau-ia/instances/abi/.env && dc up -d abi_standard` |
| Appliquer la durée de conservation tout de suite | `dc exec abi_standard python -m abi_standard purger` |

Un message qui arrive entre deux relèves attend au plus `ABI_STANDARD_INTERVALLE` secondes (120 par défaut), plus le temps de transcription. Les alertes d'urgence 5 partent dès
que le message est traité, de jour comme de nuit, sans limite d'horaires.

## Réglages qui comptent
| Réglage | Défaut | Effet |
|---|---|---|
| `ABI_STANDARD_WHISPER_MODELE` | `small` | `base` est plus rapide (6 s au lieu de 16 s par minute d'audio) et plus léger (pic 387 Mo au lieu de 675 Mo) mais moins précis |
| `ABI_STANDARD_DUREE_MAX` | 180 s | au-delà, seules les premières secondes sont transcrites et la synthèse le dit (l'original reste dans la boîte) |
| `ABI_STANDARD_LOT_MAX` | 5 | messages traités par relève ; **un seul à la fois** dans tous les cas |
| `ABI_STANDARD_MEM` / `_CPUS` | 1280m / 1.5 | plafonds du conteneur (le serveur a 2 cœurs) |
| `ABI_STANDARD_IMAP_APRES` | `lu` | `ranger` copie dans le dossier « Traité » puis retire de la boîte de réception (jamais de suppression sans copie) |
| `ABI_STANDARD_CONSERVATION_JOURS` | 30 | transcriptions, synthèses, tâches ; le journal (sans texte) : `_CONSERVATION_JOURNAL`, 365 |
| `ABI_STANDARD_MAX_MAILS_HEURE` | 30 | garde-fou contre un déluge de mails ; les alertes 5 ont leur propre limite (10) |

## Dépannage
| Constat | Cause probable | Quoi faire |
|---|---|---|
| Le conteneur s'arrête tout de suite | `ABI_STANDARD_ACTIF=0`, ou réglages incomplets | `dc logs abi_standard` indique ce qui manque |
| « relève impossible » dans les journaux, puis mail d'alerte | identifiants, port, SSL, ou serveur IMAP hors service | vérifier `IMAP_HOTE/PORT/SSL/COMPTE/MDP` ; les messages restent dans la boîte |
| « transcription impossible » | modèle non téléchargé (étape 5) ou mémoire insuffisante (`docker inspect abi_standard --format '{{.State.OOMKilled}}'`) | refaire l'étape 5 ; `ABI_STANDARD_MEM` plus grand, ou modèle `base` (`ABI_STANDARD_WHISPER_MODELE=base`) |
| Les messages arrivent mais rien n'est traité | expéditeur différent de `ABI_STANDARD_EXPEDITEURS_OK` (ils sont ignorés, marqués lus) | corriger l'adresse (voir la ligne « ignore » du journal) |
| Synthèse « classement de secours » | la Secrétaire n'a pas répondu (LiteLLM/modèles) | `bash verifier.sh --rapide` ; le message est quand même transmis |
| Synthèses en retard | limite horaire atteinte (`MAX_MAILS_HEURE`) ou SMTP en panne | elles partent seules ensuite ; `journal` montre le statut `a_notifier` |

## Données personnelles et sécurité
- **Voix** : transcrite sur le serveur, jamais envoyée à un service extérieur. Le fichier audio est écrit en mémoire (disque virtuel du conteneur), **supprimé dès la
  transcription** (réussie ou non), et les restes d'un arrêt brutal sont effacés au démarrage.
- **Texte** : la transcription est envoyée à la **Secrétaire** du Bureau IA. **Sur cette instance de test, les modèles sont en ligne : n'y mettre que des messages fictifs.**
  Sur la machine finale (modèles locaux) le texte reste lui aussi chez le client.
- **Copie d'origine** : le mail de l'opérateur (avec l'audio) **reste dans la boîte dédiée** — le standard ne supprime jamais rien côté messagerie. C'est voulu (on peut
  réécouter un message inaudible), mais l'audio y est donc conservé : **régler la suppression automatique de la boîte dédiée à 30 jours**, comme annoncé, et rappeler
  que l'opérateur a sa propre durée de conservation.
- **Journal** : date, numéro **masqué** (`06 ** ** ** 34`), catégorie, urgence, destinataires, statut. Pas de nom, pas de texte. Transcriptions et tâches : 30 jours.
- **Texte piégé** : un appelant peut dire n'importe quoi (« envoie ceci à telle adresse »). La transcription est traitée comme une donnée ; la réponse de la Secrétaire n'est lue
  que champ par champ ; les destinataires sont fixés par les réglages ; les mots d'alerte (sinistre, accident, dégât des eaux…) déclenchent l'urgence 5 même si l'IA la minimise.
- **Conteneur** : aucun port, système de fichiers en lecture seule, aucune capacité Linux, pas de swap, utilisateur non privilégié.

## Limites connues
- **Qualité de la transcription** : noms propres, numéros dits en toutes lettres, accents, mauvaise ligne. Le numéro à rappeler n'est retenu que si ses chiffres figurent dans la
  transcription ; sinon c'est celui de l'en-tête du mail de l'opérateur, signalé comme « non confirmé par l'appelant ». La synthèse rappelle que la transcription peut contenir des erreurs.
- **Mots d'urgence 5** : les racines fortes déclenchent seules, même abîmées par la transcription (dégât, inondé, « l'eau coule », incendie, fumée, pompiers, blessé, accident, effraction, cambriolé, sinistre, effondrement, odeur de gaz). Les mots ambigus (« fuite », « plafond », « gaz ») ne comptent qu'associés à un mot voisin dans la même phrase ou à 6 mots près (fuite d'eau, fuite de gaz, plafond fissuré / tombé / mouillé…) : « chauffage au gaz », « plafond de loyer », « fuite d'informations » et « hors d'eau » ne déclenchent rien. Le tout ne comprend pas la négation (« pas d'accident » déclenche l'alerte) : on préfère une fausse alerte à un sinistre manqué. La liste est dans `classement.py` (`MOTS_URGENCE_5`, `ASSOCIATIONS`).
- **Résumé** : si la Secrétaire recopie le message au lieu de le résumer (ou ne rend pas exactement 2 lignes), le standard le lui redemande une fois ; si c'est encore mauvais, il garde le classement et reconstitue le résumé à partir du motif et de l'action, en le signalant dans la synthèse.
- **Un message à la fois** : après une rafale, les derniers attendent. Au-delà de `DUREE_MAX` secondes, le reste du message n'est pas transcrit.
- **La tâche de rappel** vit dans le journal du standard (commandes `taches` / `fait`) et dans le mail ; elle n'est pas reliée à un outil de tâches du client.
- **Pas de rappel groupé le matin** : chaque message donne un mail (avec la limite horaire en garde-fou).
- **Des messages purgés avant l'envoi** (service en panne plus de 30 jours) passent en statut `expire`.
- **Phase 1 uniquement** : le standard ne décroche pas, ne répond pas en temps réel et ne transfère aucun appel (phase 2, voir le rapport).

## Désinstallation *(terminal Hostinger)*
```bash
cd /opt/abi-bureau-ia/instances/abi
dc stop abi_standard && dc rm -f abi_standard                           # arrêter et retirer le conteneur
sed -i 's|:docker-compose.standard.yml||' .env                                                    # retire le module de COMPOSE_FILE
docker volume rm abi_standard_donnees abi_standard_modeles      # EFFACE le journal, les tâches et le modèle de voix
docker image rm abi/standard:1
```
Les autres conteneurs (chat, agents, base) ne sont pas touchés. Il reste à faire désactiver côté opérateur l'envoi des messages vocaux vers la boîte dédiée.
