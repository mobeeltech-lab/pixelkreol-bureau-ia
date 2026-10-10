# Audit du Bureau IA PixelKréol — 10/10/2026

Base de l'audit : `serveur-20261010` (code en service), puis rebasé sur la branche propre `serveur-propre` (même code, sans fichiers sensibles). La branche `main` ne contient que le site vitrine
(pages HTML, `vercel.json`, dossier `atelier`) : elle n'a **aucun** fichier du Bureau IA (ni orchestrateur, ni
`install.sh`, ni registre des agents). Les deux branches sont donc deux projets différents ; ne jamais déployer `main` sur le VPS.

Résultat des tests : **238 réussis, 0 échec** après correction (**98 échecs** sur le code d'origine, avec les mêmes tests).
Aucun test n'appelle un vrai fournisseur ni n'utilise de vraie clé.

## 1. Défauts corrigés (du plus grave au moins grave)

1. **Un fichier d'agents mal écrit pouvait faire tomber tous les agents.** Un `perso.yaml` qui commence par une liste au lieu de
   `agents:`, qui contient des caractères illisibles, un cerveau mal tapé, ou un champ au mauvais format, faisait planter la lecture du
   registre : plus aucune réponse (ou conteneur qui redémarre en boucle au démarrage). Autre cas : en ajoutant un agent par `/admin/agents`
   avec un « nom » qui n'est pas du texte, `pk-auto` cessait de fonctionner pour tout le monde. Et si `perso.yaml` devenait illisible,
   les agents de vos clients disparaissaient. Maintenant : on garde la dernière version valide de chaque fichier, le problème est signalé
   dans `/health` et les journaux, et aucun des 25 agents ne bouge.
2. **L'API s'ouvrait à tout le monde si aucune clé n'était configurée** (y compris l'administration). Maintenant elle reste fermée.
   Les clés sont comparées à temps constant (une attaque par chronométrage n'est plus possible).
3. **Réponse coupée en plein milieu sans explication.** Quand la connexion avec le cerveau se coupait pendant la réponse, le texte
   s'arrêtait net. Maintenant le chat affiche « Réponse interrompue… relance ta demande », la ligne est écrite dans les journaux, et on ne relance
   pas le calcul tout seul (pas de double consommation).
4. **`pk-equipe` cachait les pannes.** Si un agent était en panne ou trop lent, la réponse finale ne le disait pas (c'était seulement dans un
   volet « détails »). Si tous étaient en panne, le directeur pouvait « synthétiser » du vide. Une réponse bizarre d'un seul agent, ou un plan mal
   formé, faisait échouer toute la demande (erreur 500). Une valeur illisible dans `PK_EQUIPE_MAX` empêchait même le démarrage.
   Maintenant : avertissement visible « sans réponse de : … », le directeur est prévenu pour ne pas inventer, erreur claire si tout est en panne,
   délai maximum par agent (150 s, réglable), pas d'agent en double, usage (jetons) additionné.
5. **Bascule entre cerveaux incomplète.** Une réponse vide (code 200 mais sans texte) donnait une bulle vide sans passer au cerveau suivant ;
   une réponse non lisible donnait une erreur 500 ; en flux, une erreur ou un flux vide au démarrage ne déclenchait pas la bascule. Le message
   « tout est en panne » ne disait pas ce qui avait été essayé. Maintenant tout cela bascule (une seule tentative par cerveau, jamais de boucle), et le message
   liste les cerveaux essayés.
6. **Fuite possible de clés dans les messages d'erreur.** Les textes d'erreur des fournisseurs (qui peuvent citer un morceau de clé) étaient renvoyés au chat
   et écrits dans le journal. Ils sont maintenant masqués.
7. **Un client de l'API pouvait imposer des options dangereuses à LiteLLM** (autre serveur, autre clé, désactivation des secours…). Ces options
   sont maintenant retirées ; les réglages normaux (température, longueur, outils…) passent toujours.
8. **Retour arrière qui ne revenait pas vraiment en arrière.** `restaurer.sh` remettait les anciens fichiers mais ne reconstruisait pas
   l'image : le conteneur gardait le nouveau code. Il reconstruit maintenant, et il vérifie que la sauvegarde est lisible **avant** de déplacer le dossier en service.
9. **Modification d'Open WebUI sans copie.** `install.sh` change les connexions dans la base d'Open WebUI sans en garder de copie ; une copie
   `webui.db.avant-DATE` est maintenant créée (les 3 dernières sont gardées), et un réglage illisible n'est plus remplacé en silence.
10. **Routage `pk-auto`** : « Kap Numérik » menait à deux agents (maintenant uniquement Subventions) ; « répondre à » envoyait au
    Secrétariat les objections de clients et les avis Google ; « marche » (le verbe) menait à la Veille ; « Aide-moi à… » déclenchait les Subventions ;
    « impayée » (féminin) n'était pas reconnu ; l'IA de routage qui répondait une phrase prenait le premier mot ressemblant à un agent (maintenant :
    réponse exacte, ou un seul agent cité, sinon on retombe sur les mots-clés). Plusieurs mots-clés ajoutés (voir le tableau). Les pannes de l'IA de routage laissent désormais une trace.
11. **Erreurs 500 muettes** : messages mal formés, JSON illisible, corps non UTF-8 → réponses d'erreur propres au format OpenAI ; toute erreur interne est
    écrite dans les journaux avec le détail, sans montrer de détail technique au client. Pages `/docs` et `/openapi.json` fermées au public.
12. **`/admin/agents`** : types vérifiés (id, num, nom, cerveau, température…), numéro 0 ou décimal refusé, taille limitée (64 Ko), plusieurs agents d'un coup
    signalés (avant : seul le premier était gardé en silence), `perso.yaml` illisible → refus clair sans l'écraser, écriture atomique avec nettoyage du fichier temporaire.
13. **Détails** : `verifier.sh` exécutait le contenu de `.env` comme des commandes (il le lit maintenant) ; le jour de la semaine donné aux agents était en anglais ;
    un test vérifie que Git ignore bien `.env`, les `.swp`, `.restic-password`, `backups/` et les copies de configuration.
14. **Pannes silencieuses (point H)** : traces ajoutées pour le routage par IA, l'état de LiteLLM (une ligne par panne, pas toutes les 15 s),
    le journal Postgres (une alerte, pas une par message), la lecture des fichiers de configuration, le nettoyage des fichiers temporaires, la lecture des réglages d'Open WebUI.

## 2. Tableau de routage `pk-auto`

Mesure faite **avec l'IA de routage en panne** (cas le plus défavorable : seuls les mots-clés décident). Quand l'IA fonctionne, elle tranche les cas
où les mots-clés hésitent ; en supposant une IA parfaite, on passe de 85/87 à 86/87.
« Attendu » = mon jugement ; corrigez-moi si vous n'êtes pas d'accord (fichier `tests/cas_routage.py`).
Le jeu de contrôle a été écrit **avant** de régler les mots-clés et n'a pas servi à les régler : il reste à 30/32 (pas d'amélioration,
pas de dégradation). Le gain du jeu principal (73 → 83 sur 87) est en partie dû à un réglage sur ces mêmes phrases : à prendre avec prudence.


### Jeu principal (87 demandes)

| # | Demande | Agent attendu | Avant | Après |
|---|---|---|---|---|
| 1 | Fais-moi un devis pour un site vitrine de 5 pages pour une boulangerie à Saint-Pierre | devis | ✅ devis | ✅ devis |
| 2 | Peux-tu me préparer un devis ? | devis | ✅ devis | ✅ devis |
| 3 | Combien coûte un site pour un artisan ? | devis | ✅ devis | ✅ devis |
| 4 | Je veux une estimation du budget pour une application mobile | devis | ✅ devis | ✅ devis |
| 5 | Trouve-moi un tarif pour la création d'une boutique en ligne | devis | ✅ devis | ✅ devis |
| 6 | Mon client veut un devis pour un logo et une charte graphique | devis | ❌ designer | ❌ designer |
| 7 | Je dois relancer un client qui n'a pas répondu à mon offre | commercial | ✅ commercial | ✅ commercial |
| 8 | Je veux relancer mes prospects de la semaine dernière | commercial | ✅ commercial | ✅ commercial |
| 9 | RELANCE CLIENT URGENTE | commercial | ✅ commercial | ✅ commercial |
| 10 | Prépare un argumentaire de vente pour un cabinet dentaire | commercial | ✅ commercial | ✅ commercial |
| 11 | Comment répondre à l'objection « c'est trop cher » d'un client ? | commercial | ❌ secretariat | ✅ commercial |
| 12 | Trouve-moi 20 prospects : plombiers dans l'Ouest | prospecteur | ❌ commercial | ❌ commercial |
| 13 | Liste des entreprises de BTP à Saint-Denis avec leurs coordonnées | prospecteur | ✅ prospecteur | ✅ prospecteur |
| 14 | Trouve des clients pour mon offre de création de site internet | prospecteur | ❌ commercial | ❌ commercial |
| 15 | Cible les restaurateurs de Saint-Gilles et fais une fiche prospect | prospecteur | ✅ prospecteur | ✅ prospecteur |
| 16 | Fais le planning du projet site e-commerce sur 6 semaines | chef-de-projet | ✅ chef-de-projet | ✅ chef-de-projet |
| 17 | Planifie la livraison du site de la boutique pour le 15 novembre | chef-de-projet | ✅ chef-de-projet | ✅ chef-de-projet |
| 18 | Il y a un retard sur le projet client du garage, que faire ? | chef-de-projet | ✅ chef-de-projet | ✅ chef-de-projet |
| 19 | Le site de mon client affiche une erreur 500 depuis ce matin | developpeur | ✅ developpeur | ✅ developpeur |
| 20 | Il y a un bug sur le site : le panier ne s'affiche plus | developpeur | ✅ developpeur | ✅ developpeur |
| 21 | Écris un script Python pour renommer des fichiers | developpeur | ✅ developpeur | ✅ developpeur |
| 22 | Mon site est lent, peux-tu optimiser le code CSS ? | developpeur | ✅ developpeur | ✅ developpeur |
| 23 | Rédige les textes de la page d'accueil du site d'un traiteur | redacteur-seo | ✅ redacteur-seo | ✅ redacteur-seo |
| 24 | Écris un article de blog sur le tourisme à La Réunion | redacteur-seo | ✅ redacteur-seo | ✅ redacteur-seo |
| 25 | Améliore le référencement Google de mon site | redacteur-seo | ✅ redacteur-seo | ✅ redacteur-seo |
| 26 | Prépare un shooting photo pour un restaurant à Saint-Leu | photo-video | ✅ photo-video | ✅ photo-video |
| 27 | Fais un script vidéo de 30 secondes pour un reel | photo-video | ✅ photo-video | ✅ photo-video |
| 28 | Aide-moi à faire le storyboard du tournage de la pub | photo-video | ✅ photo-video | ✅ photo-video |
| 29 | Crée un logo pour une entreprise de BTP | designer | ✅ designer | ✅ designer |
| 30 | Fais une affiche pour le salon de l'artisanat | designer | ✅ designer | ✅ designer |
| 31 | Réalise la maquette de l'interface de l'application | designer | ✅ designer | ✅ designer |
| 32 | Écris un post Instagram pour la promo de la semaine chez le coiffeur | community-manager | ✅ community-manager | ✅ community-manager |
| 33 | Cree un post instagram pour le cafe | community-manager | ✅ community-manager | ✅ community-manager |
| 34 | Propose des hashtags pour une vidéo TikTok | community-manager | ✅ community-manager | ✅ community-manager |
| 35 | Calendrier éditorial pour le mois de novembre | community-manager | ✅ community-manager | ✅ community-manager |
| 36 | Rédige le post LinkedIn pour annoncer notre nouveau site | community-manager | ✅ community-manager | ✅ community-manager |
| 37 | Écris la légende d'une photo Facebook pour un salon de beauté | community-manager | ✅ community-manager | ✅ community-manager |
| 38 | Rédige une relance pour la facture 2026-014 impayée depuis 45 jours | comptable | ❌ commercial | ✅ comptable |
| 39 | Où en est la trésorerie ce mois-ci ? | comptable | ✅ comptable | ✅ comptable |
| 40 | Calcule la TVA à reverser pour le trimestre | comptable | ✅ comptable | ✅ comptable |
| 41 | Je dois déclarer mes charges à l'URSSAF ce mois-ci | comptable | ✅ comptable | ✅ comptable |
| 42 | Facture à envoyer à la mairie de Saint-Paul | comptable | ✅ comptable | ✅ comptable |
| 43 | Fais une facture pour la prestation de design du restaurant | comptable | ❌ designer | ❌ designer |
| 44 | Prépare le compte rendu de la réunion d'hier avec le restaurateur | secretariat | ✅ secretariat | ✅ secretariat |
| 45 | Réponds à ce mail du fournisseur de papier | secretariat | ✅ secretariat | ✅ secretariat |
| 46 | Quel est mon agenda demain ? | secretariat | ✅ secretariat | ✅ secretariat |
| 47 | Envoie un rappel pour la réunion de jeudi | secretariat | ✅ secretariat | ✅ secretariat |
| 48 | Rédige un email de remerciement à un client après la livraison | secretariat | ❌ chef-de-projet | ✅ secretariat |
| 49 | Peux-tu relire le contrat de prestation avant signature ? | juriste | ✅ juriste | ✅ juriste |
| 50 | Quelles sont les mentions légales obligatoires pour le site ? | juriste | ✅ juriste | ✅ juriste |
| 51 | Prépare les CGV pour la boutique en ligne | juriste | ✅ juriste | ✅ juriste |
| 52 | Écris un accord de confidentialité NDA pour un freelance | juriste | ✅ juriste | ✅ juriste |
| 53 | Nous avons un litige avec un fournisseur | juriste | ✅ juriste | ✅ juriste |
| 54 | Prépare une demande de subvention Kap Numérik | subventions | ✅ subventions | ✅ subventions |
| 55 | Quelles aides existent pour numériser une TPE à La Réunion ? | subventions | ✅ subventions | ✅ subventions |
| 56 | Aide-moi à monter un dossier de financement auprès de la Région Réunion | subventions | ✅ subventions | ✅ subventions |
| 57 | quelles subventions pour une startup a la reunion | subventions | ✅ subventions | ✅ subventions |
| 58 | Classe les documents scannés du dossier client Hoarau | archiviste | ✅ archiviste | ✅ archiviste |
| 59 | Résume ce PDF de 80 pages | archiviste | ✅ archiviste | ✅ archiviste |
| 60 | Le client est mécontent, il veut être remboursé | support | ❌ orchestrateur | ✅ support |
| 61 | J'ai reçu une réclamation d'un client | support | ✅ support | ✅ support |
| 62 | Le client dit que ça ne marche plus du tout, il ne peut plus se connecter | support | ❌ veille | ✅ support |
| 63 | Comment répondre à un avis Google négatif sur le salon de coiffure ? | e-reputation | ❌ secretariat | ✅ e-reputation |
| 64 | Écris une réponse aux avis clients du restaurant | e-reputation | ✅ e-reputation | ✅ e-reputation |
| 65 | Crée un tutoriel pour que mon client apprenne à modifier son site | formateur | ✅ formateur | ✅ formateur |
| 66 | Prépare un programme de formation de 2 jours sur Canva | formateur | ✅ formateur | ✅ formateur |
| 67 | Traduis ce texte en créole réunionnais | traducteur | ✅ traducteur | ✅ traducteur |
| 68 | Traduis ma plaquette en anglais pour les touristes | traducteur | ✅ traducteur | ✅ traducteur |
| 69 | Mets en place la sauvegarde automatique du VPS | devops | ✅ devops | ✅ devops |
| 70 | Le certificat SSL du site expire demain | devops | ✅ devops | ✅ devops |
| 71 | Docker ne démarre plus sur le serveur | devops | ✅ devops | ✅ devops |
| 72 | Le nom de domaine du client n'est plus joignable, problème de DNS | devops | ❌ support | ✅ devops |
| 73 | Fais la relecture complète du site avant la livraison au client | qualite | ❌ chef-de-projet | ✅ qualite |
| 74 | Écris les tests pour le formulaire de commande | qualite | ✅ qualite | ✅ qualite |
| 75 | Quelles sont les nouveautés des outils IA cette semaine ? | veille | ✅ veille | ✅ veille |
| 76 | Fais un benchmark de mes concurrents à Saint-Denis | veille | ✅ veille | ✅ veille |
| 77 | Compare Notion et Airtable pour mon équipe | veille | ❌ orchestrateur | ✅ veille |
| 78 | Étude du marché réunionnais pour les agences web | veille | ✅ veille | ✅ veille |
| 79 | Fais-moi un tableau de bord des ventes du mois | analyste | ✅ analyste | ✅ analyste |
| 80 | Analyse les statistiques de mon site ce mois-ci | analyste | ✅ analyste | ✅ analyste |
| 81 | Quel est le taux de conversion de ma campagne ? | analyste | ✅ analyste | ✅ analyste |
| 82 | Fais-moi le point du jour et les priorités de la semaine | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |
| 83 | Qui doit faire quoi cette semaine ? | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |
| 84 | Organise le travail entre les agents pour le lancement du site client | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |
| 85 | Bonjour ! | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |
| 86 | Prépare un pack d'agents IA pour un cabinet d'infirmières | architecte-bureau-ia | ❌ orchestrateur | ✅ architecte-bureau-ia |
| 87 | Je veux créer un nouvel agent pour un garage automobile | fabrique-agents | ✅ fabrique-agents | ✅ fabrique-agents |

**Avant : 73/87 — Après : 83/87**

### Jeu de contrôle (32 demandes)

| # | Demande | Agent attendu | Avant | Après |
|---|---|---|---|---|
| 1 | Prépare un devis pour un site de réservation pour un gîte à Cilaos | devis | ✅ devis | ✅ devis |
| 2 | Relance Mme Payet, elle n'a pas signé la proposition | commercial | ✅ commercial | ✅ commercial |
| 3 | Écris une accroche pour LinkedIn sur notre nouveau service | community-manager | ✅ community-manager | ✅ community-manager |
| 4 | Mon formulaire d'inscription renvoie une erreur PHP | developpeur | ✅ developpeur | ✅ developpeur |
| 5 | Quand est la prochaine échéance de la TVA ? | comptable | ✅ comptable | ✅ comptable |
| 6 | Rédige les mentions légales et la politique RGPD du site | juriste | ✅ juriste | ✅ juriste |
| 7 | Fais une affiche et un flyer pour l'inauguration | designer | ✅ designer | ✅ designer |
| 8 | Prépare l'argumentaire pour convaincre la mairie de Saint-Louis | commercial | ✅ commercial | ✅ commercial |
| 9 | Classe les factures scannées de 2025 dans le bon dossier | archiviste | ❌ comptable | ❌ comptable |
| 10 | Mon client veut apprendre à utiliser son nouveau site, prépare un guide | formateur | ✅ formateur | ✅ formateur |
| 11 | Traduis cette réponse en créole pour mon client | traducteur | ✅ traducteur | ✅ traducteur |
| 12 | Combien de visiteurs sur le site ce mois-ci et quel taux de rebond ? Fais des statistiques | analyste | ✅ analyste | ✅ analyste |
| 13 | Quels sont les concurrents de PixelKréol à Saint-Pierre ? | veille | ✅ veille | ✅ veille |
| 14 | Vérifie la qualité du site avant de le livrer | qualite | ✅ qualite | ✅ qualite |
| 15 | Comment installer Caddy sur le VPS ? | devops | ✅ devops | ✅ devops |
| 16 | Mets à jour l'agenda de la semaine et envoie les rappels | secretariat | ✅ secretariat | ✅ secretariat |
| 17 | Planifie les étapes du projet client de la pharmacie | chef-de-projet | ✅ chef-de-projet | ✅ chef-de-projet |
| 18 | Que devient la subvention Région pour mon projet numérique ? | subventions | ✅ subventions | ✅ subventions |
| 19 | Un client a laissé un avis une étoile sur Google | e-reputation | ❌ redacteur-seo | ❌ redacteur-seo |
| 20 | Fais une vidéo de présentation de l'équipe pour le site | photo-video | ✅ photo-video | ✅ photo-video |
| 21 | Cherche des leads dans le secteur du tourisme | prospecteur | ✅ prospecteur | ✅ prospecteur |
| 22 | Le serveur répond lentement, regarde les logs Docker | devops | ✅ devops | ✅ devops |
| 23 | Nouveau client à accueillir : prépare un tutoriel pas à pas | formateur | ✅ formateur | ✅ formateur |
| 24 | Fais le bilan de la semaine | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |
| 25 | Écris une légende et des hashtags pour le reel de samedi | community-manager | ✅ community-manager | ✅ community-manager |
| 26 | Mon client demande un remboursement, la facture est déjà payée | comptable | ✅ comptable | ✅ comptable |
| 27 | Ajoute une page contact au site React | developpeur | ✅ developpeur | ✅ developpeur |
| 28 | La charte graphique doit-elle être livrée en PDF ? | designer | ✅ designer | ✅ designer |
| 29 | Je veux monter un Bureau IA chez un client avocat | architecte-bureau-ia | ✅ architecte-bureau-ia | ✅ architecte-bureau-ia |
| 30 | Crée un agent pour gérer les réservations du restaurant | fabrique-agents | ✅ fabrique-agents | ✅ fabrique-agents |
| 31 | Aide-moi à écrire un mail au fournisseur | secretariat | ✅ secretariat | ✅ secretariat |
| 32 | Bonsoir, tu peux m'aider ? | orchestrateur | ✅ orchestrateur | ✅ orchestrateur |

**Avant : 30/32 — Après : 30/32**


Cas qui restent faux : « devis » + sujet d'un autre métier (logo / design), « prospects » (Commercial plutôt que Prospecteur), « avis » sans le mot
« Google », « classe les factures » (Comptable plutôt qu'Archiviste). Quand l'IA de routage répond, la plupart sont corrigés ; pas le cas devis + logo, où les mots-clés sont trop sûrs d'eux.

## 3. Défauts repérés mais non corrigés (à décider)

- **Fichiers sensibles / `fix_docker.sh`** : trouvés dans l'ancienne branche `serveur-20261010` (fichier de mot de passe et dépôt de sauvegarde restic, fichiers temporaires d'éditeur, copies de configuration, script obsolète). Réglé par la branche propre `serveur-propre` ; il reste à changer le mot de passe restic et à considérer comme exposé tout ce que l'ancienne branche a pu contenir.
- **Une seule clé pour tout** : par défaut la clé d'API, la clé d'administration et la clé maître de LiteLLM sont identiques (celle que connaissent n8n et Open WebUI).
  Une clé `PK_ADMIN_KEY` distincte est possible dans `.env` ; je n'ai pas imposé le changement car `LISEZMOI.txt` et vos flux n8n s'appuient sur la clé actuelle.
- **`pk-auto` n'a pas de mémoire de la conversation** : un message court (« oui, vas-y ») est routé tout seul et peut changer d'agent. Une « stickiness » est possible mais change le comportement.
- **Le préfixe « ➜ Agent » ajouté par `pk-auto`** se retrouve dans les textes récupérés par n8n. Mettre `PK_ANNONCE_AGENT=false` ou appeler directement `pk-NN-agent` dans n8n.
- **Conjugaisons** : « organise » ne correspond pas au mot-clé « organiser » (pas de racine des mots) ; corriger demanderait un traitement de la langue plus lourd, donc discutable.
- **Délais en cascade** : jusqu'à 120 s par cerveau (3 cerveaux), et LiteLLM attend jusqu'à 90 s par fournisseur : un échec total peut prendre plusieurs minutes. Si le délai est dépassé, l'appel peut
  continuer côté fournisseur pendant qu'on essaie le suivant (consommation de quota en double, mais sans coût : fournisseurs gratuits, sauf DeepSeek en fin de chaîne de `pk-costaud`).
- **`install.sh`** : (a) `tar … || true` : une sauvegarde partielle ne bloque pas l'installation ; (b) `.env` est réécrit avec les seules variables connues (les autres sont dans `/opt/pk-sauvegardes/env-DATE`) ;
  (c) les volumes n8n/Open WebUI ne sont pas sauvegardés par le script (seulement la base Postgres et les fichiers), d'où l'étape de sauvegarde manuelle plus bas ;
  (d) crée un rôle Postgres SUPERUSER s'il manque ; (e) domaines et e-mail par défaut écrits en dur (modifiables dans `.env`) ; (f) images `:latest` / `:main` non épinglées pour n8n, Open WebUI et LiteLLM.
  Aucune commande destructrice (`docker system prune`, suppression de volumes, `down -v`) n'a été trouvée ; `shellcheck` ne signale rien.
- **Pas de limitation du nombre d'essais de clé** : à régler dans Caddy si l'API est exposée. `/health` et la page `/` sont publiques (liste des agents visible).
- **Pas de `/v1/embeddings` ni `/v1/completions`** : les nœuds n8n qui en ont besoin ne marcheront pas via l'orchestrateur.
- **L'IA de routage consomme un appel** sur environ la moitié des messages (ceux où les mots-clés hésitent).
- **`perso.yaml`** : les commentaires écrits à la main sont perdus quand l'API réécrit le fichier.

## 4. Mise à jour du serveur (terminal Hostinger)

Voir la procédure dans la réponse de l'audit ; en résumé : sauvegarde manuelle → récupération du code dans `/opt/pk-maj` → `bash install.sh` → `verifier.sh`
→ retour arrière possible avec `restaurer.sh`.
