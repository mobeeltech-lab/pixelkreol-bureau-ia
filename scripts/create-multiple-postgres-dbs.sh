#!/usr/bin/env bash
# Crée les bases "litellm" et "n8n" dans le même conteneur Postgres.
# Exécuté automatiquement par l'image postgres au premier démarrage
# (dossier /docker-entrypoint-initdb.d).
set -euo pipefail

if [ -n "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
	echo "Création des bases : $POSTGRES_MULTIPLE_DATABASES"
	IFS=',' read -ra DBS <<< "$POSTGRES_MULTIPLE_DATABASES"
	for db in "${DBS[@]}"; do
		psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
			SELECT 'CREATE DATABASE ${db}'
			WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')\gexec
		EOSQL
	done
fi
