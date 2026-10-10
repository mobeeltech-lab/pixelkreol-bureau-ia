#!/usr/bin/env bash
# Vérifie le Bureau IA : conteneurs, agents, fournisseurs LLM (appels réels), URLs publiques.
#   bash verifier.sh            vérification complète
#   bash verifier.sh --rapide   sans appel aux vrais modèles
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
V='\033[0;32m'; R='\033[0;31m'; J='\033[1;33m'; B='\033[1;36m'; N='\033[0m'
KO=0
echo -e "${B}━━ Conteneurs${N}"
for c in pk_postgres pk_litellm pk_orchestrateur pk_openwebui pk_n8n pk_caddy; do
  etat="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null | tr -d '[:space:]')"; etat="${etat:-absent}"
  if [[ "$c" == pk_caddy && "$etat" == absent ]]; then continue; fi
  if [[ "$etat" == healthy || "$etat" == running ]]; then echo -e "  ${V}✔${N} $c : $etat"
  else echo -e "  ${R}✘${N} $c : $etat"; KO=$((KO+1)); fi
done
echo -e "${B}━━ Agents et cerveaux${N}"
docker exec -i pk_orchestrateur python - "$@" < outils/verifier.py || KO=$((KO+1))
if [[ " $* " != *" --rapide "* ]]; then
  echo -e "${B}━━ Accès publics${N}"
  set -a; source .env 2>/dev/null; set +a
  for d in "${PK_DOMAINE_AGENTS:-}" "${PK_DOMAINE_N8N:-}"; do
    [[ -z "$d" ]] && continue
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$d/" 2>/dev/null)"
    if [[ "$code" =~ ^(200|301|302|307|308)$ ]]; then echo -e "  ${V}✔${N} https://$d → HTTP $code"
    else echo -e "  ${J}⚠${N} https://$d → HTTP ${code:-aucune réponse} (DNS, certificat ou proxy à vérifier)"; fi
  done
fi
exit $(( KO > 0 ? 1 : 0 ))
