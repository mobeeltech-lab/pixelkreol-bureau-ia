#!/usr/bin/env bash
# PK — sauvegarde quotidienne : atelier (registre, factures, sites, .env), données Open WebUI et n8n.
# Garde 14 jours dans /opt/pk-sauvegardes. Restauration : voir LISEZMOI.txt.
set -uo pipefail
DEST=/opt/pk-sauvegardes
JOURS=${PK_SAUVEGARDE_JOURS:-14}
D=$(date +%Y-%m-%d_%H%M)
TMP=$(mktemp -d)
mkdir -p "$DEST"; chmod 700 "$DEST"
# base de l'atelier : copie cohérente même en cours d'écriture
if [ -f /opt/pk-atelier/data/atelier.sqlite ]; then
  python3 -c "import sqlite3,sys;s=sqlite3.connect('/opt/pk-atelier/data/atelier.sqlite');d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close()" "$TMP/atelier.sqlite"
fi
LISTE=("$TMP")
[ -d /opt/pk-atelier ] && LISTE+=(/opt/pk-atelier/.env /opt/pk-atelier/data)
[ -d /opt/pk-searxng ] && LISTE+=(/opt/pk-searxng)
# volumes des conteneurs Open WebUI et n8n (dossiers de données montés)
for C in $(docker ps --format '{{.Names}}' | grep -Ei 'webui|n8n' || true); do
  for S in $(docker inspect "$C" -f '{{range .Mounts}}{{.Source}} {{end}}'); do
    [ -d "$S" ] && LISTE+=("$S")
  done
done
tar --warning=no-file-changed -czf "$DEST/pk-$D.tar.gz" --exclude='*/cache/*' "${LISTE[@]}" 2>/dev/null
RC=$?
rm -rf "$TMP"
chmod 600 "$DEST/pk-$D.tar.gz"
find "$DEST" -name 'pk-*.tar.gz' -mtime +"$JOURS" -delete
TAILLE=$(du -h "$DEST/pk-$D.tar.gz" | cut -f1)
echo "[pk-sauvegarde] $D : $TAILLE (code $RC), $(ls "$DEST" | wc -l) sauvegarde(s) conservée(s)"
# si le disque est plein à plus de 85 %, on ne garde que les 3 dernières
USE=$(df --output=pcent "$DEST" | tail -1 | tr -dc 0-9)
if [ "${USE:-0}" -gt 85 ]; then ls -1t "$DEST"/pk-*.tar.gz | tail -n +4 | xargs -r rm -f; echo "[pk-sauvegarde] disque à $USE % : rotation réduite à 3"; fi
exit 0
