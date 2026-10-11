# Déploiement du lot 1 (agents + banc) sur le serveur

Le dossier du Bureau sur le serveur, `/opt/pixelkreol-bureau-ia`, **n'est pas une copie git** : tout se fait par **copie de fichiers**.
Toutes les commandes se tapent dans le **terminal Hostinger** (hPanel → VPS → *Terminal*), en administrateur (`root`), une par une.

## Ce qu'il faut copier (14 fichiers, chemins relatifs au dossier du Bureau)

| Fichier | Rôle | Effet |
|---|---|---|
| `agents/registre.yaml` | les 25 fiches améliorées | **seul fichier qui change le comportement des agents** |
| `scripts/banc_agents.py` | la commande du banc d'essai | aucun effet sur les agents |
| `scripts/banc/__init__.py`, `regles.py`, `cas.py`, `cas_direction_vente.py`, `cas_production.py`, `cas_gestion_relation.py`, `cas_technique_produit.py`, `runner.py`, `audit.py` | le banc : règles, 200 questions, exécution | aucun effet sur les agents |
| `scripts/banc/LISEZMOI.md`, `DEPLOIEMENT.md`, `SHA256SUMS` | notice, cette procédure, empreintes de contrôle | aucun |

À **ne pas** copier : `tests/` (développement), `instances/` (l'instance ABI n'est pas concernée), `agents/socle.txt`, `agents/cerveaux.yaml`, `agents/perso.yaml`
(inchangés ; `perso.yaml` contient les agents ajoutés sur le serveur et ne doit jamais être écrasé), le code de l'orchestrateur (inchangé : pas de reconstruction).

## L'orchestrateur relit les fiches à chaud, sans redémarrage
Le dossier `agents/` est monté dans le conteneur (`./agents:/app/agents`). À **chaque requête**, l'orchestrateur compare la taille et la date de modification de
`registre.yaml`, `perso.yaml`, `socle.txt` et `cerveaux.yaml` ; s'ils ont changé, il les relit (un fichier illisible ou tronqué ne remplace jamais la dernière version valide).
Il n'y a donc **ni `docker compose restart`, ni `bash install.sh`, ni reconstruction** : la nouvelle fiche sert dès la question suivante (vérifié par un test automatique,
y compris pour un fichier remplacé par une copie). Éviter seulement les copies qui conservent l'ancienne date *et* la même taille (`cp -p`, `rsync -t`, `touch -r`) : sans changement
de date ni de taille, la relecture ne voit rien. Le nouveau `registre.yaml` a une taille différente de l'ancien, ce qui ne pose pas de problème.

## Procédure (avec mesure avant / après)

### 1. Récupérer les fichiers et vérifier la copie *(terminal Hostinger)*
Le dossier de dépôt GitHub est public ; sinon copier les fichiers autrement (WinSCP, gestionnaire de fichiers…) vers `/root/lot1` en gardant les chemins, puis passer à la vérification.
```bash
REF=bureau-agents        # après la fusion de la pull request : REF=serveur-propre
rm -rf /root/lot1 && mkdir -p /root/lot1 && cd /root/lot1
for f in agents/registre.yaml scripts/banc_agents.py scripts/banc/{__init__,regles,cas,cas_direction_vente,cas_production,cas_gestion_relation,cas_technique_produit,runner,audit}.py scripts/banc/{LISEZMOI,DEPLOIEMENT}.md scripts/banc/SHA256SUMS; do
  mkdir -p "$(dirname "$f")"; curl -fsSL "https://raw.githubusercontent.com/mobeeltech-lab/pixelkreol-bureau-ia/$REF/$f" -o "$f" || echo "ÉCHEC : $f"
done
sha256sum -c scripts/banc/SHA256SUMS      # doit afficher « OK » pour les 13 fichiers
```

### 2. Installer le banc seul (sans toucher aux agents) *(terminal Hostinger)*
```bash
cp -r /root/lot1/scripts/banc_agents.py /root/lot1/scripts/banc /opt/pixelkreol-bureau-ia/scripts/
cd /opt/pixelkreol-bureau-ia && python3 scripts/banc_agents.py --liste | tail -1      # « 200 cas »
```

### 3. Mesurer l'état actuel (avant) *(terminal Hostinger, 20 à 40 min)*
```bash
cd /opt/pixelkreol-bureau-ia && python3 scripts/banc_agents.py --sauver /root/banc-avant.json
```

### 4. Activer les nouvelles fiches *(terminal Hostinger)*
```bash
cd /opt/pixelkreol-bureau-ia
cp agents/registre.yaml /root/registre.yaml.avant-lot1          # sauvegarde pour le retour arrière
cp /root/lot1/agents/registre.yaml agents/registre.yaml && chmod 644 agents/registre.yaml
docker exec pk_orchestrateur python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:4001/health').read().decode())"
```
La réponse doit contenir `"agents":25` ; aucun redémarrage n'est nécessaire. (Les agents ajoutés dans `agents/perso.yaml` restent en place.)

### 5. Mesurer après et comparer *(terminal Hostinger)*
```bash
cd /opt/pixelkreol-bureau-ia && python3 scripts/banc_agents.py --comparer /root/banc-avant.json --sauver /root/banc-apres.json
```

### Retour arrière *(terminal Hostinger)*
```bash
cp /root/registre.yaml.avant-lot1 /opt/pixelkreol-bureau-ia/agents/registre.yaml      # relu à chaud, sans redémarrage
```

## Ce que ce lot ne touche pas
L'instance ABI (`instances/abi`, son registre, son compose), l'orchestrateur (code, image, conteneurs), `docker-compose.yml`, `.env`, LiteLLM et les cerveaux.
