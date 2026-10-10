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
# L'archive doit être lisible AVANT qu'on déplace quoi que ce soit (sinon on se retrouverait sans dossier).
tar -tzf "$ARCH" >/dev/null 2>&1 || { echo "Archive illisible ou abîmée : $ARCH — rien n'a été modifié."; exit 1; }
cd "$DEST" && docker compose down --remove-orphans || true
mv "$DEST" "$DEST.v2-$(date +%Y%m%d-%H%M%S)"
tar -xzf "$ARCH" -C "$(dirname "$DEST")"
# --build : l'orchestrateur est une image construite à partir du dossier ; sans cela, l'ancien code
# restauré ne serait pas celui qui tourne (le conteneur garderait la version récente).
cd "$DEST" && docker compose up -d --build
echo "Restauré depuis $ARCH (la version v2 est gardée à côté)."
