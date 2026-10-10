#!/usr/bin/env bash
# Retour arrière : remet les fichiers sauvegardés par install.sh à la date donnée.
#   bash scripts/restaurer.sh 20260926-150000
# Les volumes (données n8n, Open WebUI, Postgres) ne sont jamais supprimés par install.sh ;
# un export SQL est aussi disponible dans /opt/pk-sauvegardes si besoin.
set -euo pipefail
SAUV="${PK_SAUVEGARDES:-/opt/pk-sauvegardes}"
DEST="${PK_DEST:-/opt/pixelkreol-bureau-ia}"
H="${1:-}"
if [[ -z "$H" ]]; then echo "Sauvegardes disponibles :"; ls -1 "$SAUV" | sed -n 's/^bureau-ia-fichiers-\(.*\)\.tgz$/  \1/p'; exit 0; fi
ARCH="$SAUV/bureau-ia-fichiers-$H.tgz"
[[ -f "$ARCH" ]] || { echo "Introuvable : $ARCH"; exit 1; }
cd "$DEST" && docker compose down --remove-orphans || true
mv "$DEST" "$DEST.v2-$(date +%Y%m%d-%H%M%S)"
tar -xzf "$ARCH" -C "$(dirname "$DEST")"
cd "$DEST" && docker compose up -d
echo "Restauré depuis $ARCH (la version v2 est gardée à côté)."
