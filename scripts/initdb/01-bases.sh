#!/bin/sh
# Premier démarrage uniquement : crée les bases du Bureau IA.
set -e
for base in n8n pk_bureau; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tc "SELECT 1 FROM pg_database WHERE datname='$base'" | grep -q 1 \
    || psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE $base"
done
