#!/usr/bin/env bash
# =====================================================================
#  PK Atelier — installation / mise à jour sur le VPS Hostinger
#  Rejouable : conserve /opt/pk-atelier/.env et les données (factures,
#  numérotation, mails, sites démo).
#  Usage :  bash install-atelier.sh
# =====================================================================
set -euo pipefail
DOMAINE="${DOMAINE:-mebeeltech.online}"
HOTE_API="atelier.$DOMAINE"
HOTE_DEMO="demo.$DOMAINE"
DIR=/opt/pk-atelier
SRC="$DIR/src"
PORT=20300
DEPOT="${DEPOT:-https://github.com/mobeeltech-lab/pixelkreol-bureau-ia.git}"
IP_VPS="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

vert(){ printf '\033[32m%s\033[0m\n' "$*"; }
# écrit ou remplace une variable dans le .env
setenv(){ if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi; }
jaune(){ printf '\033[33m%s\033[0m\n' "$*"; }

echo "== 1/7 Code source"
mkdir -p "$DIR/data"
if [ -d "$SRC/.git" ]; then git -C "$SRC" pull --ff-only -q; else git clone -q --depth 1 "$DEPOT" "$SRC"; fi
APP="$SRC/atelier"
chmod -R a+rX "$SRC"   # serveur à umask strict : le conteneur (non-root) doit pouvoir lire le code
[ -f "$APP/Dockerfile" ] || { echo "Dossier atelier/ introuvable dans le dépôt"; exit 1; }

echo "== 2/7 Configuration (.env)"
if [ ! -f "$DIR/.env" ]; then
  cp "$APP/env.exemple" "$DIR/.env"
  sed -i "s|^ATELIER_KEY=.*|ATELIER_KEY=$(openssl rand -hex 24)|; s|^ATELIER_SECRET=.*|ATELIER_SECRET=$(openssl rand -hex 32)|" "$DIR/.env"
  sed -i "s|^ATELIER_URL_PUBLIQUE=.*|ATELIER_URL_PUBLIQUE=https://$HOTE_API|; s|^ATELIER_URL_DEMO=.*|ATELIER_URL_DEMO=https://$HOTE_DEMO|; s|^ATELIER_HOTE_DEMO=.*|ATELIER_HOTE_DEMO=$HOTE_DEMO|" "$DIR/.env"
  vert "   .env créé avec des clés neuves"
else
  vert "   .env existant conservé"
fi
chmod 600 "$DIR/.env"
mkdir -p "$DIR/data/hebergement/demandes" "$DIR/data/hebergement/attente" "$DIR/data/hebergement/etat"
chown -R 10001:10001 "$DIR/data"
setenv ATELIER_IP_VPS "$IP_VPS"

echo "== 2b Hébergement autonome (sites sur sous-domaines et domaines clients)"
install -m 755 "$APP/hote/pk-hebergeur.py" /usr/local/bin/pk-hebergeur
install -m 644 "$APP/hote/pk-hebergeur.service" "$APP/hote/pk-hebergeur-file.service" \
               "$APP/hote/pk-hebergeur.path" "$APP/hote/pk-hebergeur-file.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pk-hebergeur.path pk-hebergeur-file.timer >/dev/null 2>&1
setenv HEBERGEMENT_ACTIF true
# sous-domaines démo : il faut un enregistrement DNS « *.demo » → IP du VPS (une seule fois)
if getent hosts "pk-test-$RANDOM.$HOTE_DEMO" | grep -q "$IP_VPS"; then
  setenv HEBERGEMENT_SOUS_DOMAINES true
  vert "   sous-domaines actifs : chaque démo aura son adresse https://<nom>.$HOTE_DEMO"
else
  setenv HEBERGEMENT_SOUS_DOMAINES false
  jaune "   ⚠ Pour une adresse propre par démo, ajoute dans hPanel un enregistrement A « *.demo » → $IP_VPS, puis relance ce script."
fi
vert "   pk-hebergeur installé (nginx + certificats HTTPS automatiques, sous validation de Will pour les domaines clients)"
install -m 755 "$APP/hote/pk-sauvegarde.sh" /usr/local/bin/pk-sauvegarde
install -m 755 "$APP/hote/pk-gardien.sh" /usr/local/bin/pk-gardien
install -m 644 "$APP/hote/pk-sauvegarde.service" "$APP/hote/pk-sauvegarde.timer" \
               "$APP/hote/pk-gardien.service" "$APP/hote/pk-gardien.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pk-sauvegarde.timer pk-gardien.timer >/dev/null 2>&1
vert "   sauvegarde chaque nuit (/opt/pk-sauvegardes, 14 jours) et gardien toutes les 5 minutes"

echo "== 3/7 Image Docker"
docker build -q -t pk-atelier:latest "$APP" >/dev/null
vert "   image pk-atelier:latest prête"

echo "== 4/7 Conteneur"
NET=$(docker network ls --format '{{.Name}}' | grep -m1 -E '^pixelkreol_net$|pixelkreol' || true)
docker rm -f pk_atelier >/dev/null 2>&1 || true
docker run -d --name pk_atelier --restart unless-stopped ${NET:+--network "$NET"} --memory 512m \
  -p 127.0.0.1:$PORT:8000 --env-file "$DIR/.env" -v "$DIR/data":/data pk-atelier:latest >/dev/null
for i in $(seq 1 20); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/health" && echo
# brancher l'atelier sur le(s) réseau(x) d'Open WebUI pour que l'outil l'appelle via http://pk_atelier:8000
OWUI=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($0) ~ /open-webui|openwebui/ {print $1; exit}')
if [ -n "$OWUI" ]; then
  for N in $(docker inspect "$OWUI" -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'); do
    docker network connect "$N" pk_atelier 2>/dev/null || true
  done
  vert "   relié à Open WebUI ($OWUI) : l'outil appelle http://pk_atelier:8000"
else
  jaune "   ⚠ conteneur Open WebUI introuvable : relie-le à la main (docker network connect <réseau> pk_atelier)"
fi

echo "== 4b Moteur de recherche privé (SearXNG)"
docker network inspect pk_outils >/dev/null 2>&1 || docker network create pk_outils >/dev/null
docker network connect pk_outils pk_atelier 2>/dev/null || true
mkdir -p /opt/pk-searxng
if [ ! -f /opt/pk-searxng/settings.yml ]; then
  cat > /opt/pk-searxng/settings.yml <<SX
use_default_settings: true
general:
  instance_name: "PixelKreol recherche"
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
  image_proxy: false
  public_instance: false
search:
  safe_search: 1
  default_lang: "fr"
  formats: [html, json]
ui:
  default_locale: fr
SX
fi
chmod -R a+rX /opt/pk-searxng
docker pull -q searxng/searxng:latest >/dev/null 2>&1 || true
docker rm -f pk_searxng >/dev/null 2>&1 || true
docker run -d --name pk_searxng --restart unless-stopped --network pk_outils -e GRANIAN_HOST=0.0.0.0 \
  --memory 384m -v /opt/pk-searxng:/etc/searxng searxng/searxng:latest >/dev/null
if [ -n "${OWUI:-}" ]; then
  for N in $(docker inspect "$OWUI" -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'); do
    docker network connect "$N" pk_searxng 2>/dev/null || true
  done
fi
for i in $(seq 1 20); do docker exec pk_atelier python -c "import urllib.request;urllib.request.urlopen('http://pk_searxng:8080/healthz',timeout=3)" >/dev/null 2>&1 && break; sleep 2; done
if docker exec pk_atelier python -c "import urllib.request;urllib.request.urlopen('http://pk_searxng:8080/healthz',timeout=3)" >/dev/null 2>&1; then
  vert "   recherche web privée prête (agents : outil PK Atelier · Open WebUI : http://pk_searxng:8080/search?q=<query>)"
else
  jaune "   ⚠ SearXNG ne répond pas encore : docker logs pk_searxng"
fi

echo "== 5/7 nginx"
for H in "$HOTE_API" "$HOTE_DEMO"; do
  F=/etc/nginx/sites-available/$H
  if [ ! -f "$F" ]; then
    cat > "$F" <<NGX
server {
    listen 80;
    listen [::]:80;
    server_name $H;
    client_max_body_size 20m;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location /api/ { return 404; }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120;
    }
}
NGX
    ln -sf "$F" /etc/nginx/sites-enabled/$H
  fi
done
nginx -t -q && systemctl reload nginx
vert "   nginx rechargé (l'API /api/ n'est jamais exposée sur Internet)"

# démos en sous-domaine : réponse HTTP immédiate, le certificat de chaque démo est créé par pk-hebergeur
F=/etc/nginx/sites-available/pk-demo-sous-domaines
cat > "$F" <<NGX
# Géré par install-atelier.sh (PK Atelier)
server {
    listen 80;
    listen [::]:80;
    server_name *.$HOTE_DEMO;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location /api/ { return 404; }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGX
ln -sf "$F" /etc/nginx/sites-enabled/pk-demo-sous-domaines
nginx -t -q && systemctl reload nginx

echo "== 6/7 Certificats HTTPS"
for H in "$HOTE_API" "$HOTE_DEMO"; do
  if getent hosts "$H" | grep -q "$IP_VPS"; then
    if ! grep -q "ssl_certificate" /etc/nginx/sites-available/$H; then
      certbot --nginx -d "$H" --non-interactive --agree-tos --redirect -q && vert "   HTTPS actif pour $H"
    else
      vert "   HTTPS déjà actif pour $H"
    fi
  else
    jaune "   ⚠ $H ne pointe pas encore vers $IP_VPS : ajoute l'enregistrement DNS A « ${H%%.*} » dans hPanel, puis relance ce script."
  fi
done

echo "== 7/7 Récapitulatif"
source <(grep -E '^(ATELIER_KEY|SMTP_HOTE|VENDEUR_SIREN|VENDEUR_SIRET)=' "$DIR/.env")
echo
vert "PK Atelier est en place."
echo "  Liens publics : https://$HOTE_API (fichiers, validation des mails) · https://$HOTE_DEMO (sites démo)"
echo "  Clé à coller dans Open WebUI (outil « PK Atelier », réglage ATELIER_KEY) :"
echo "      $ATELIER_KEY"
[ -z "${SMTP_HOTE:-}" ] && jaune "  ⚠ Envoi de mails pas encore configuré : renseigne SMTP_* dans $DIR/.env puis relance le script."
[ -z "${VENDEUR_SIREN:-}${VENDEUR_SIRET:-}" ] && jaune "  ⚠ SIREN/SIRET vide : les factures portent un bandeau « mentions à compléter » et pas de Factur-X."
echo "  Sauvegardes : /opt/pk-sauvegardes (chaque nuit, 14 jours) · lancer tout de suite : pk-sauvegarde"
echo "  Configuration : nano $DIR/.env   ·   Journaux : docker logs -f pk_atelier"
