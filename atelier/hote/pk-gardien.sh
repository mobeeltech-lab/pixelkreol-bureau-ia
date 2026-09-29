#!/usr/bin/env bash
# PK — gardien : relance l'atelier ou le moteur de recherche s'ils ne répondent plus.
PORT=${PK_PORT:-20300}
if docker ps -a --format '{{.Names}}' | grep -qx pk_atelier; then
  if ! curl -fsS -m 10 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    sleep 20
    if ! curl -fsS -m 10 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      echo "[pk-gardien] atelier muet : redémarrage"; docker restart pk_atelier >/dev/null
    fi
  fi
fi
if docker ps -a --format '{{.Names}}' | grep -qx pk_searxng; then
  if [ "$(docker inspect -f '{{.State.Running}}' pk_searxng)" != "true" ]; then
    echo "[pk-gardien] moteur de recherche arrêté : redémarrage"; docker start pk_searxng >/dev/null
  fi
fi
# alerte disque
USE=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "${USE:-0}" -gt 90 ] && echo "[pk-gardien] ⚠ disque plein à $USE %"
exit 0
