#!/usr/bin/env sh
set -eu

COMPOSE_FILE="${E2E_COMPOSE_FILE:-compose.e2e.yaml}"

docker compose -f "$COMPOSE_FILE" up -d --build --wait --wait-timeout 180
python3 e2e/run_e2e.py
docker compose -f "$COMPOSE_FILE" exec -T postgres \
  psql -U review_agent_e2e -d review_agent_e2e -c \
  "SELECT (SELECT count(*) FROM incidents) AS incidents, (SELECT count(*) FROM evidence_documents) AS evidence, (SELECT count(*) FROM retrospective_versions) AS versions, (SELECT count(*) FROM review_actions) AS reviews, (SELECT count(*) FROM publications) AS publications, (SELECT count(*) FROM knowledge_entries) AS knowledge_entries, (SELECT count(*) FROM knowledge_chunks) AS knowledge_chunks;"
