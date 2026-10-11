#!/usr/bin/env bash
# =====================================================================
#  COUPURE D'URGENCE de l'accès au NAS (instance de test ABI Holding)
#
#  Une seule commande, à taper dans le terminal Hostinger :
#      bash /opt/abi-bureau-ia/instances/abi/nas-coupure-urgence.sh
#  (ajouter --tailscale pour couper aussi le réseau privé Tailscale du serveur)
#
#  1) ABI_NAS_ACTIF=0 et retrait du montage du NAS dans le conteneur ;
#  2) redémarrage de abi_orchestrateur SEUL (le chat et la base ne bougent pas) ;
#  3) démontage du partage sur le serveur, et ligne du partage mise en commentaire dans /etc/fstab
#     (il ne revient pas tout seul au prochain redémarrage).
#  Ne supprime rien : ni l'index, ni le journal, ni les données du NAS.
# =====================================================================
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONTAGE="${ABI_NAS_MONTAGE:-/mnt/nas-abi}"
FSTAB="${ABI_FSTAB:-/etc/fstab}"
ECHECS=0
PROJET="${ABI_COMPOSE_PROJET:-abi}"      # projet Docker Compose de la pile ABI (le même que celui de `dc`, voir les LISEZMOI)
cd "$DIR" || { echo "Dossier introuvable : $DIR"; exit 1; }
unset COMPOSE_FILE

echo "1/3 Désactivation de la lecture du NAS dans .env..."
if [[ -f .env ]]; then
  if grep -q '^ABI_NAS_ACTIF=' .env; then sed -i 's|^ABI_NAS_ACTIF=.*|ABI_NAS_ACTIF=0|' .env; else echo 'ABI_NAS_ACTIF=0' >> .env; fi
  # retire SEULEMENT le NAS de COMPOSE_FILE (le standard téléphonique, s'il est activé, y reste) ; la ligne disparaît s'il ne reste que le fichier de base
  sed -i '/^COMPOSE_FILE=/s#:docker-compose\.nas\.yml##g' .env
  sed -i '/^COMPOSE_FILE=docker-compose\.yml$/d' .env
else
  echo "   (pas de fichier .env : rien à modifier)"
fi

echo "2/3 Redémarrage de abi_orchestrateur sans le NAS..."
docker compose -p "$PROJET" --project-directory . up -d --no-deps --force-recreate abi_orchestrateur || { echo "   ✘ redémarrage impossible"; ECHECS=$((ECHECS+1)); }

echo "3/3 Démontage du partage ${MONTAGE}..."
if mountpoint -q "$MONTAGE" 2>/dev/null || grep -q " ${MONTAGE} " /proc/mounts 2>/dev/null; then
  umount "$MONTAGE" 2>/dev/null || umount -l "$MONTAGE" || { echo "   ✘ démontage impossible"; ECHECS=$((ECHECS+1)); }
else
  echo "   (le partage n'était pas monté)"
fi

# le partage ne doit pas revenir tout seul au prochain redémarrage du serveur : on met sa ligne en commentaire
if [[ -f "$FSTAB" ]] && grep -qE "^[^#].*[[:space:]]${MONTAGE}[[:space:]]+cifs" "$FSTAB"; then
  sed -i -E "s|^([^#].*[[:space:]]${MONTAGE}[[:space:]]+cifs)|# [coupure d'urgence] \1|" "$FSTAB" && systemctl daemon-reload 2>/dev/null
  echo "   ligne du partage mise en commentaire dans ${FSTAB} (à décommenter pour réactiver)"
fi

if [[ "${1:-}" == "--tailscale" ]]; then
  echo "Coupure du réseau privé Tailscale..."
  tailscale down || ECHECS=$((ECHECS+1))
fi

if (( ECHECS == 0 )); then echo "✔ Accès au NAS coupé."; else echo "⚠ $ECHECS étape(s) en échec : voir ci-dessus."; fi
exit $(( ECHECS > 0 ? 1 : 0 ))
