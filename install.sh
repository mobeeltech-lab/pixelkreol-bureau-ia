#!/usr/bin/env bash
# =====================================================================
#  Bureau IA PixelKréol — installation / mise à jour v2
#
#  Usage (en root, depuis le dossier extrait) :
#      bash install.sh
#  Options (variables d'environnement) :
#      PK_DEST=/opt/pixelkreol-bureau-ia   dossier d'installation
#      PK_PROXY=auto|caddy|nginx|aucun     reverse proxy (auto = détection)
#      PK_SANS_TEST_REEL=1                 ne pas appeler les vrais LLM à la fin
#
#  Ce script est rejouable sans risque : il sauvegarde d'abord, conserve les
#  clés existantes, réutilise les volumes (n8n, Open WebUI, Postgres/CRM) et
#  ne supprime jamais de données.
# =====================================================================
set -Eeuo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${PK_DEST:-/opt/pixelkreol-bureau-ia}"
PROXY="${PK_PROXY:-auto}"
PROJET="pixelkreol-bureau-ia"
HORO="$(date +%Y%m%d-%H%M%S)"
SAUV="${PK_SAUVEGARDES:-/opt/pk-sauvegardes}"
JOURNAL="$SAUV/install-$HORO.log"

V='\033[0;32m'; J='\033[1;33m'; R='\033[0;31m'; B='\033[1;36m'; N='\033[0m'
etape() { echo -e "\n${B}━━ $* ${N}"; }
ok()    { echo -e "  ${V}✔${N} $*"; }
info()  { echo -e "  • $*"; }
alerte(){ echo -e "  ${J}⚠${N} $*"; }
stop()  { echo -e "\n${R}✘ $*${N}" >&2; exit 1; }
trap 'echo -e "\n${R}✘ Erreur ligne $LINENO (commande : $BASH_COMMAND). Journal : $JOURNAL${N}" >&2' ERR

[[ $EUID -eq 0 ]] || stop "Lancer en root : sudo bash install.sh"
mkdir -p "$SAUV" && chmod 700 "$SAUV"
exec > >(tee -a "$JOURNAL") 2>&1

echo -e "${B}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   Bureau IA PixelKréol — installation v2     ║"
echo -e "  ╚══════════════════════════════════════════════╝${N}"

# ---------------------------------------------------------------------
etape "1/9 Vérifications"
command -v docker >/dev/null || stop "Docker absent. Installer Docker puis relancer."
docker info >/dev/null 2>&1 || stop "Le démon Docker ne répond pas (systemctl start docker)."
if docker compose version >/dev/null 2>&1; then DC=(docker compose)
else stop "Plugin « docker compose » absent : apt-get install -y docker-compose-plugin"; fi
ok "Docker $(docker version -f '{{.Server.Version}}') + $("${DC[@]}" version --short 2>/dev/null || echo compose)"
for f in docker-compose.yml agents/registre.yaml agents/cerveaux.yaml orchestrateur/app.py outils/generer_litellm.py; do
  [[ -f "$SRC/$f" ]] || stop "Fichier manquant dans le paquet : $f"
done
RAM_LIBRE=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
DISQUE_LIBRE=$(df -Pm "$(dirname "$DEST")" | awk 'NR==2{print $4}')
info "RAM disponible : ${RAM_LIBRE} Mo — disque libre : ${DISQUE_LIBRE} Mo"
(( DISQUE_LIBRE > 4000 )) || stop "Moins de 4 Go libres sur le disque."
(( RAM_LIBRE > 1500 )) || alerte "RAM disponible faible (${RAM_LIBRE} Mo) : la pile demande ~2,5 Go."

# ---------------------------------------------------------------------
etape "2/9 Sauvegarde de l'existant"
if [[ -d "$DEST" ]]; then
  tar -czf "$SAUV/bureau-ia-fichiers-$HORO.tgz" -C "$(dirname "$DEST")" "$(basename "$DEST")" 2>/dev/null || true
  chmod 600 "$SAUV/bureau-ia-fichiers-$HORO.tgz"
  ok "Fichiers sauvegardés : $SAUV/bureau-ia-fichiers-$HORO.tgz"
else
  info "Première installation (pas de $DEST)"
fi
if docker ps --format '{{.Names}}' | grep -qx pk_postgres; then
  PGU=$(docker exec pk_postgres printenv POSTGRES_USER 2>/dev/null || echo postgres)
  if docker exec pk_postgres pg_dumpall -U "$PGU" 2>/dev/null | gzip > "$SAUV/bureau-ia-postgres-$HORO.sql.gz"; then
    chmod 600 "$SAUV/bureau-ia-postgres-$HORO.sql.gz"; ok "Base Postgres exportée"
  else alerte "Export Postgres impossible (on continue, les volumes sont conservés)"; fi
fi

# ---------------------------------------------------------------------
etape "3/9 Récupération des réglages et des clés existants"
declare -A ENVV=()
est_bidon() {  # valeurs d'exemple à ne jamais reprendre
  [[ -z "$1" || "$1" =~ (changeme|change_me|votre|your|xxx|\.\.\.|<|exemple|example|REMPLACER|a_definir) ]]
}
charger_env() {  # $1 fichier ; $2 filtre optionnel (regex de clés) ; n'écrase pas ce qui est déjà connu
  local f="$1" filtre="${2:-.*}" k v
  [[ -f "$f" ]] || return 0
  while IFS= read -r ligne || [[ -n "$ligne" ]]; do
    [[ "$ligne" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [[ "$k" =~ ^($filtre)$ ]] || continue
    est_bidon "$v" && continue
    [[ -z "${ENVV[$k]:-}" ]] && ENVV[$k]="$v"
  done < "$f"
  return 0
}
charger_conteneur() {  # récupère les variables d'un conteneur existant (même arrêté)
  local c="$1" ligne k v
  docker inspect "$c" >/dev/null 2>&1 || return 0
  while IFS= read -r ligne; do
    [[ "$ligne" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"   # à copier AVANT tout autre test =~
    est_bidon "$v" && continue
    case "$k" in
      GROQ_API_KEY|GEMINI_API_KEY|MISTRAL_API_KEY|CEREBRAS_API_KEY|OPENROUTER_API_KEY|DEEPSEEK_API_KEY|\
      LITELLM_MASTER_KEY|WEBUI_SECRET_KEY|N8N_ENCRYPTION_KEY|POSTGRES_USER|POSTGRES_PASSWORD)
        [[ -z "${ENVV[$k]:-}" ]] && ENVV[$k]="$v" ;;
      DB_POSTGRESDB_PASSWORD) [[ -z "${ENVV[POSTGRES_PASSWORD]:-}" ]] && ENVV[POSTGRES_PASSWORD]="$v" ;;
      DB_POSTGRESDB_USER)     [[ -z "${ENVV[POSTGRES_USER]:-}" ]] && ENVV[POSTGRES_USER]="$v" ;;
    esac
  done < <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null)
  return 0
}
# Fichiers d'environnement existants, du plus récent au plus ancien (hors exemples et fichiers d'éditeur)
# shellcheck disable=SC2010  # noms de fichiers maîtrisés
mapfile -t FICHIERS_ENV < <(ls -1t "$DEST"/.env "$DEST"/.env.* "$DEST"/*.env 2>/dev/null \
  | grep -vE '\.(example|exemple|sample|swp|nouveau)$' || true)
# Secrets d'infrastructure (base, Open WebUI, clé maître utilisée par n8n) : on les reprend du fichier
# le plus récent qui contient POSTGRES_PASSWORD — c'est celui de l'installation qui a créé les données.
INFRA='LITELLM_MASTER_KEY|WEBUI_SECRET_KEY|POSTGRES_USER|POSTGRES_PASSWORD|N8N_ENCRYPTION_KEY'
for f in "${FICHIERS_ENV[@]}"; do
  if grep -qE '^POSTGRES_PASSWORD=.+' "$f"; then charger_env "$f" "$INFRA"; info "Secrets d'infrastructure repris de $(basename "$f")"; break; fi
done
# Puis tout le reste (clés des fournisseurs : la plus récente gagne), fichier par fichier
for f in "${FICHIERS_ENV[@]}" "$SRC/.env"; do [[ -f "$f" ]] && charger_env "$f"; done
for c in pk_n8n pk_litellm pk_openwebui pk_postgres; do charger_conteneur "$c"; done

vol_existe() { docker volume inspect "$1" >/dev/null 2>&1; }
lire_dans_volume() { docker run --rm -v "$1":/v:ro alpine:3 sh -c "cat /v/$2 2>/dev/null" 2>/dev/null || true; }

# Volumes : on réutilise ceux qui existent déjà
VOL_N8N="${ENVV[PK_VOL_N8N]:-${PROJET}_pk_n8n_data}"
VOL_OWUI="${ENVV[PK_VOL_OPENWEBUI]:-${PROJET}_pk_openwebui_data}"
VOL_PG="${ENVV[PK_VOL_POSTGRES]:-}"
if [[ -z "$VOL_PG" ]]; then
  for cand in "${PROJET}_pk_postgres_data" "${PROJET}_postgres_data"; do
    if vol_existe "$cand" && [[ -n "$(lire_dans_volume "$cand" PG_VERSION)" ]]; then VOL_PG="$cand"; break; fi
  done
fi
VOL_PG="${VOL_PG:-${PROJET}_pk_postgres_data}"
PG_VERSION_VOL=""
vol_existe "$VOL_PG" && PG_VERSION_VOL="$(lire_dans_volume "$VOL_PG" PG_VERSION | tr -d '[:space:]')"
if [[ -n "$PG_VERSION_VOL" ]]; then
  ENVV[PK_PG_IMAGE]="postgres:${PG_VERSION_VOL}-alpine"
  ok "Postgres existant réutilisé : volume $VOL_PG (version $PG_VERSION_VOL)"
else
  ENVV[PK_PG_IMAGE]="${ENVV[PK_PG_IMAGE]:-postgres:17-alpine}"
  info "Nouveau volume Postgres : $VOL_PG"
fi
vol_existe "$VOL_N8N"  && ok "Données n8n conservées : $VOL_N8N"        || info "Nouveau volume n8n : $VOL_N8N"
vol_existe "$VOL_OWUI" && ok "Données Open WebUI conservées : $VOL_OWUI" || info "Nouveau volume Open WebUI : $VOL_OWUI"

# Clé de chiffrement n8n : celle enregistrée dans ses données fait foi (sinon n8n refuse de démarrer)
if vol_existe "$VOL_N8N"; then
  CLE_N8N="$(lire_dans_volume "$VOL_N8N" config | sed -n 's/.*"encryptionKey"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  if [[ -n "$CLE_N8N" ]]; then
    [[ -n "${ENVV[N8N_ENCRYPTION_KEY]:-}" && "${ENVV[N8N_ENCRYPTION_KEY]}" != "$CLE_N8N" ]] && alerte "Clé n8n de .env différente de celle des données : on garde celle des données"
    ENVV[N8N_ENCRYPTION_KEY]="$CLE_N8N"; ok "Clé de chiffrement n8n reprise de ses données"
  fi
fi

secret() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-40}"; }
NOUVEAUX=()
defaut() { local k="$1" v="$2"; if [[ -z "${ENVV[$k]:-}" ]]; then ENVV[$k]="$v"; NOUVEAUX+=("$k"); fi; }
defaut LITELLM_MASTER_KEY "sk-pk-$(secret 40)"
defaut POSTGRES_USER pixelkreol
defaut POSTGRES_PASSWORD "$(secret 32)"
defaut WEBUI_SECRET_KEY "$(secret 48)"
if [[ -z "${ENVV[N8N_ENCRYPTION_KEY]:-}" ]]; then
  vol_existe "$VOL_N8N" && alerte "Clé n8n introuvable : une nouvelle clé est créée — les identifiants déjà enregistrés dans n8n devront être ressaisis."
  defaut N8N_ENCRYPTION_KEY "$(secret 40)"
fi
defaut PK_DOMAINE_AGENTS agents.mebeeltech.online
defaut PK_DOMAINE_N8N agents-n8n.mebeeltech.online
defaut PK_DOMAINE_API agents-api.mebeeltech.online
defaut PK_EMAIL_ACME mobeeltech@gmail.com
ENVV[PK_VOL_POSTGRES]="$VOL_PG"; ENVV[PK_VOL_N8N]="$VOL_N8N"; ENVV[PK_VOL_OPENWEBUI]="$VOL_OWUI"
(( ${#NOUVEAUX[@]} )) && info "Valeurs créées : ${NOUVEAUX[*]}"

NB_FOURN=0
for k in GROQ_API_KEY GEMINI_API_KEY MISTRAL_API_KEY CEREBRAS_API_KEY OPENROUTER_API_KEY DEEPSEEK_API_KEY MOCK_API_KEY; do
  if [[ -n "${ENVV[$k]:-}" ]]; then ok "Clé ${k%_API_KEY} présente"; NB_FOURN=$((NB_FOURN+1)); else info "Clé ${k%_API_KEY} absente (fournisseur ignoré)"; fi
done
(( NB_FOURN > 0 )) || stop "Aucune clé de fournisseur LLM. Ajoute au moins GROQ_API_KEY ou OPENROUTER_API_KEY dans $DEST/.env puis relance."

# ---------------------------------------------------------------------
etape "4/9 Installation des fichiers dans $DEST"
mkdir -p "$DEST"
# anciens fichiers de la tentative du 24/09 : rangés (ils contenaient des clés en clair)
mkdir -p "$DEST/ancien"
for f in README.md DEPLOYMENT_GUIDE.md QUICK_START.sh VERIFY_DEPLOYMENT.sh deploy_9_agents.sh test_9_agents.sh \
         litellm_custom_server.py litellm_init.sh routing; do
  [[ -e "$DEST/$f" ]] && mv "$DEST/$f" "$DEST/ancien/$f.$HORO"
done
chmod 700 "$DEST/ancien"
if [[ "$SRC" != "$DEST" ]]; then
  for d in agents orchestrateur outils config scripts; do mkdir -p "$DEST/$d"; done
  cp -f "$SRC/docker-compose.yml" "$SRC/docker-compose.caddy.yml" "$SRC/install.sh" "$SRC/verifier.sh" "$DEST/"
  [[ -f "$SRC/LISEZMOI.txt" ]] && cp -f "$SRC/LISEZMOI.txt" "$DEST/"
  # registre / cerveaux / socle : si modifiés sur le serveur, on garde une copie avant de remplacer
  for f in registre.yaml cerveaux.yaml socle.txt; do
    if [[ -f "$DEST/agents/$f" ]] && ! cmp -s "$SRC/agents/$f" "$DEST/agents/$f"; then
      cp "$DEST/agents/$f" "$DEST/agents/$f.avant-$HORO"
    fi
    cp -f "$SRC/agents/$f" "$DEST/agents/$f"
  done
  [[ -f "$DEST/agents/perso.yaml" ]] || printf '# Agents personnalisés (même format que registre.yaml). Rechargé à chaud.\nagents: []\n' > "$DEST/agents/perso.yaml"
  cp -rf "$SRC/orchestrateur/." "$DEST/orchestrateur/"
  rm -rf "$DEST/orchestrateur/tests" "$DEST/orchestrateur/__pycache__"
  cp -rf "$SRC/outils/." "$DEST/outils/"
  cp -rf "$SRC/scripts/." "$DEST/scripts/"
  cp -f "$SRC/config/Caddyfile.modele" "$SRC/config/Caddyfile.api" "$DEST/config/"
  [[ -d "$SRC/tests" && -n "${PK_AVEC_TESTS:-}" ]] && cp -rf "$SRC/tests" "$DEST/"
fi
chown -R 1000:1000 "$DEST/agents"
chmod +x "$DEST"/*.sh "$DEST"/scripts/initdb/*.sh 2>/dev/null || true
ok "Fichiers en place"

# ---------------------------------------------------------------------
etape "5/9 Reverse proxy (HTTPS)"
occupant_port() {  # nom du programme qui écoute sur le port $1 (vide si libre)
  local nom=""
  if command -v ss >/dev/null 2>&1; then
    nom="$(ss -Hltnp "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1 || true)"
    [[ -z "$nom" && -n "$(ss -Hltn "sport = :$1" 2>/dev/null || true)" ]] && nom="inconnu"
  elif (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
    nom="$(pgrep -a nginx >/dev/null 2>&1 && echo nginx || echo inconnu)"
  fi
  echo "$nom"
}
# nos anciens conteneurs Caddy ne comptent pas comme « occupants »
OCC80="$(occupant_port 80)"; OCC443="$(occupant_port 443)"
if docker ps --format '{{.Names}} {{.Ports}}' | grep -E '^pk_caddy .*(:80->|:443->)' >/dev/null; then OCC80=""; OCC443=""; fi
if [[ "$PROXY" == auto ]]; then
  if [[ -z "$OCC80$OCC443" ]]; then PROXY=caddy
  elif [[ "$OCC80$OCC443" == *nginx* ]]; then PROXY=nginx
  else PROXY=aucun; alerte "Ports 80/443 tenus par « ${OCC80:-$OCC443} » : reverse proxy non configuré automatiquement."
  fi
fi
IP_VPS="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
dns_ok() { local ip; ip="$(getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}')"; [[ -n "$ip" && "$ip" == "$IP_VPS" ]]; }
DOMAINES=("${ENVV[PK_DOMAINE_AGENTS]}" "${ENVV[PK_DOMAINE_N8N]}")
AVEC_API=0
if dns_ok "${ENVV[PK_DOMAINE_API]}"; then AVEC_API=1; DOMAINES+=("${ENVV[PK_DOMAINE_API]}")
else info "${ENVV[PK_DOMAINE_API]} ne pointe pas (encore) vers ce serveur : API publique non exposée (optionnel)."; fi
for d in "${ENVV[PK_DOMAINE_AGENTS]}" "${ENVV[PK_DOMAINE_N8N]}"; do
  dns_ok "$d" && ok "DNS $d → $IP_VPS" || alerte "DNS $d ne pointe pas vers $IP_VPS : le certificat HTTPS échouera tant que ce n'est pas corrigé."
done

case "$PROXY" in
  caddy)
    cp "$DEST/config/Caddyfile.modele" "$DEST/config/Caddyfile"
    if (( AVEC_API )); then { echo; cat "$DEST/config/Caddyfile.api"; } >> "$DEST/config/Caddyfile"; fi
    ENVV[COMPOSE_FILE]="docker-compose.yml:docker-compose.caddy.yml"
    ok "Caddy (conteneur) gérera HTTPS pour : ${DOMAINES[*]}" ;;
  nginx)
    unset 'ENVV[COMPOSE_FILE]'
    CONF=/etc/nginx/sites-available/pixelkreol-bureau-ia.conf
    [[ -d /etc/nginx/sites-available ]] || CONF=/etc/nginx/conf.d/pixelkreol-bureau-ia.conf
    [[ -f "$CONF" ]] && cp "$CONF" "$SAUV/nginx-pixelkreol-bureau-ia.conf.$HORO"
    bloc_nginx() {  # $1 domaine  $2 port local
      local cert="/etc/letsencrypt/live/$1"
      cat <<NGX
server {
    listen 80;
    server_name $1;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
NGX
      if [[ -f "$cert/fullchain.pem" ]]; then cat <<NGX
server {
    listen 443 ssl;
    server_name $1;
    ssl_certificate     $cert/fullchain.pem;
    ssl_certificate_key $cert/privkey.pem;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:$2;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
NGX
      fi
    }
    # Domaine déjà servi par un site nginx existant (ex. installé par certbot) ? On le garde
    # tel quel et on aligne nos ports sur son proxy_pass, au lieu de créer un doublon.
    A_GENERER=()
    site_existant() {  # $1 domaine -> chemin du fichier nginx qui le sert déjà (hors le nôtre)
      grep -lsE "server_name[^;]*[[:space:]]$1[[:space:];]" /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null \
        | grep -v 'pixelkreol-bureau-ia.conf' | head -1 || true
    }
    for paire in "PK_DOMAINE_AGENTS:PK_PORT_OPENWEBUI:3010" "PK_DOMAINE_N8N:PK_PORT_N8N:5678" "PK_DOMAINE_API:PK_PORT_ORCH:4001"; do
      IFS=: read -r vdom vport pdef <<< "$paire"
      dom="${ENVV[$vdom]}"
      [[ "$vdom" == PK_DOMAINE_API ]] && (( ! AVEC_API )) && continue
      fichier="$(site_existant "$dom")"
      port_ex=""
      [[ -n "$fichier" ]] && port_ex="$(grep -oE 'proxy_pass[[:space:]]+http://(127\.0\.0\.1|localhost):[0-9]+' "$fichier" | head -1 | grep -oE '[0-9]+$' || true)"
      if [[ -n "$port_ex" ]]; then
        ENVV[$vport]="$port_ex"
        ok "$dom déjà servi par nginx ($(basename "$fichier")) → service publié sur 127.0.0.1:$port_ex"
      else
        A_GENERER+=("$dom:${ENVV[$vport]:-$pdef}")
      fi
    done
    ecrire_nginx() {
      if (( ${#A_GENERER[@]} == 0 )); then rm -f "$CONF" /etc/nginx/sites-enabled/pixelkreol-bureau-ia.conf; return 0; fi
      {
        echo "# Bureau IA PixelKréol — généré par install.sh ($HORO)"
        for dp in "${A_GENERER[@]}"; do bloc_nginx "${dp%%:*}" "${dp##*:}"; done
      } > "$CONF"
      if [[ "$CONF" == */sites-available/* ]]; then ln -sf "$CONF" /etc/nginx/sites-enabled/pixelkreol-bureau-ia.conf; fi
      return 0
    }
    mkdir -p /var/www/html
    ecrire_nginx
    nginx -t && systemctl reload nginx
    if (( ${#A_GENERER[@]} )) && command -v certbot >/dev/null; then
      for dp in "${A_GENERER[@]}"; do
        d="${dp%%:*}"
        if [[ ! -f "/etc/letsencrypt/live/$d/fullchain.pem" ]] && dns_ok "$d"; then
          if certbot certonly --webroot -w /var/www/html -d "$d" --non-interactive --agree-tos -m "${ENVV[PK_EMAIL_ACME]}"; then
            ok "Certificat obtenu pour $d"; else alerte "Certificat impossible pour $d"; fi
        fi
      done
      ecrire_nginx; nginx -t && systemctl reload nginx
    elif (( ${#A_GENERER[@]} )); then
      alerte "certbot absent : apt-get install -y certbot puis relancer install.sh pour activer HTTPS."
    fi
    if (( ${#A_GENERER[@]} )); then ok "nginx : nouveaux sites dans $CONF"; else ok "nginx : sites existants réutilisés, aucun fichier ajouté"; fi ;;
  aucun)
    unset 'ENVV[COMPOSE_FILE]'
    info "Pas de reverse proxy : services joignables en local (127.0.0.1:3010 / :5678 / :4001)." ;;
  *) stop "PK_PROXY inconnu : $PROXY" ;;
esac

# ---------------------------------------------------------------------
etape "6/9 Écriture de .env"
{
  echo "# Bureau IA PixelKréol — généré par install.sh le $HORO"
  echo "# Les valeurs existantes ont été conservées. NE PAS partager ce fichier."
  for k in $(printf '%s\n' "${!ENVV[@]}" | sort); do
    [[ "$k" =~ ^(PK_|POSTGRES_|LITELLM_|WEBUI_|N8N_|OPENWEBUI_|COMPOSE_|[A-Z]+_API_KEY$) ]] || continue
    printf '%s=%s\n' "$k" "${ENVV[$k]}"
  done
} > "$DEST/.env.nouveau"
chmod 600 "$DEST/.env.nouveau"
[[ -f "$DEST/.env" ]] && cp "$DEST/.env" "$SAUV/env-$HORO" && chmod 600 "$SAUV/env-$HORO"
mv "$DEST/.env.nouveau" "$DEST/.env"
ok ".env écrit (droits 600) — ancienne version dans $SAUV/env-$HORO"

# ---------------------------------------------------------------------
etape "7/9 Construction de l'orchestrateur et des cerveaux"
cd "$DEST"
if [[ -z "${PK_SANS_BUILD:-}" ]]; then "${DC[@]}" build pk_orchestrateur; else info "Construction ignorée (PK_SANS_BUILD)"; fi
docker run --rm --user 0 -v "$DEST":/w --entrypoint python pixelkreol/orchestrateur:2 \
  /w/outils/generer_litellm.py --cerveaux /w/agents/cerveaux.yaml --env /w/.env --sortie /w/config/litellm_config.yaml \
  || stop "Génération de la configuration LiteLLM impossible"
chmod 644 "$DEST/config/litellm_config.yaml"
ok "config/litellm_config.yaml généré"

# Anciens conteneurs portant nos noms (tentatives précédentes) : on les retire avant de
# recréer la pile. Les DONNÉES sont dans les volumes et ne sont pas touchées.
for c in pk_postgres pk_litellm pk_orchestrateur pk_openwebui pk_n8n pk_caddy pk_routing_proxy; do
  if docker inspect "$c" >/dev/null 2>&1; then
    cfg_vol="$(docker inspect -f '{{range .Mounts}}{{if .Name}}{{.Name}} {{end}}{{end}}' "$c" 2>/dev/null || true)"
    docker rm -f "$c" >/dev/null && info "Ancien conteneur $c retiré${cfg_vol:+ (volumes conservés : ${cfg_vol% })}"
  fi
done
# Postgres existant : on aligne le mot de passe du rôle sur .env (connexion locale de confiance)
"${DC[@]}" up -d pk_postgres
for _ in $(seq 1 30); do docker exec pk_postgres pg_isready -q 2>/dev/null && break; sleep 2; done
PGU="${ENVV[POSTGRES_USER]}"
PGPW_SQL="${ENVV[POSTGRES_PASSWORD]//\'/\'\'}"
ROLE_OK=0
for r in "$PGU" pixelkreol litellm postgres; do
  if docker exec pk_postgres psql -U "$r" -d postgres -tAc "select 1" >/dev/null 2>&1; then
    if [[ "$r" != "$PGU" ]]; then
      docker exec pk_postgres psql -U "$r" -d postgres -tAc "select 1 from pg_roles where rolname='$PGU'" | grep -q 1 \
        || docker exec pk_postgres psql -U "$r" -d postgres -c "CREATE ROLE \"$PGU\" LOGIN SUPERUSER" >/dev/null
    fi
    docker exec -i pk_postgres psql -U "$r" -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE "$PGU" WITH LOGIN PASSWORD '${PGPW_SQL}';
SQL
    ROLE_OK=1; break
  fi
done
(( ROLE_OK )) || stop "Impossible d'accéder à Postgres pour aligner le mot de passe."
for base in n8n pk_bureau; do
  docker exec pk_postgres psql -U "$PGU" -d postgres -tAc "select 1 from pg_database where datname='$base'" | grep -q 1 \
    || docker exec pk_postgres psql -U "$PGU" -d postgres -c "CREATE DATABASE $base OWNER \"$PGU\"" >/dev/null
done
ok "Postgres prêt (rôle $PGU, bases n8n et pk_bureau)"

# Open WebUI existant : on branche l'orchestrateur sans toucher au reste
if vol_existe "$VOL_OWUI"; then
  docker rm -f pk_openwebui >/dev/null 2>&1 || true
  CLE_API="${ENVV[PK_API_KEY]:-${ENVV[LITELLM_MASTER_KEY]}}"
  rc=0
  docker run --rm --user 0 -v "$VOL_OWUI":/data -v "$DEST/outils":/o:ro --entrypoint python pixelkreol/orchestrateur:2 \
    /o/patch_openwebui.py /data/webui.db "http://pk_orchestrateur:4001/v1" "$CLE_API" \
    "http://pk_litellm:4000/v1" "${ENVV[LITELLM_MASTER_KEY]}" || rc=$?
  case $rc in 0) ok "Connexions Open WebUI mises à jour" ;; 3) info "Open WebUI : première configuration via les variables" ;;
    *) alerte "Mise à jour des connexions Open WebUI impossible (code $rc) : à refaire dans Réglages > Connexions" ;; esac
fi

# ---------------------------------------------------------------------
etape "8/9 Démarrage de la pile"
"${DC[@]}" up -d --remove-orphans
attendre() {  # $1 nom  $2 secondes
  local t=0 etat
  while (( t < $2 )); do
    etat="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null | tr -d '[:space:]')"; etat="${etat:-absent}"
    [[ "$etat" == healthy || ( "$etat" == running && "$1" != pk_litellm && "$1" != pk_orchestrateur ) ]] && { ok "$1 : $etat"; return 0; }
    sleep 3; t=$((t+3))
  done
  alerte "$1 : $etat après $2 s"; docker logs --tail 25 "$1" 2>&1 | sed 's/^/      /'; return 1
}
ECHECS=0
for c in pk_postgres pk_litellm pk_orchestrateur pk_openwebui pk_n8n; do attendre "$c" 240 || ECHECS=$((ECHECS+1)); done
[[ "$PROXY" == caddy ]] && { attendre pk_caddy 60 || ECHECS=$((ECHECS+1)); }

# ---------------------------------------------------------------------
etape "9/9 Vérification"
if [[ -n "${PK_SANS_TEST_REEL:-}" ]]; then bash "$DEST/verifier.sh" --rapide || ECHECS=$((ECHECS+1))
else bash "$DEST/verifier.sh" || ECHECS=$((ECHECS+1)); fi

echo
if (( ECHECS == 0 )); then
  echo -e "${V}🎉 Bureau IA PixelKréol opérationnel.${N}"
else
  echo -e "${J}Installation terminée avec $ECHECS point(s) à regarder (voir ci-dessus).${N}"
fi
echo "   Chat des agents : https://${ENVV[PK_DOMAINE_AGENTS]}"
echo "   n8n             : https://${ENVV[PK_DOMAINE_N8N]}"
(( AVEC_API )) && echo "   API des agents  : https://${ENVV[PK_DOMAINE_API]}/v1  (clé : PK_API_KEY ou LITELLM_MASTER_KEY dans .env)"
echo "   Journal         : $JOURNAL"
echo "   Retour arrière  : bash $DEST/scripts/restaurer.sh $HORO"
exit $(( ECHECS > 0 ? 1 : 0 ))
