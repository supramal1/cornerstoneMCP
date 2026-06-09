#!/usr/bin/env bash
# Deploy cornerstone-mcp to Cloud Run from this canonical source repo.
# Uses native env vars + (later) per-key Secret Manager refs — no
# /var/secrets/.env mount, no shell-source trick in Dockerfile.
#
# Rollback: capture the current revision name before running this and
# revert via:
#   gcloud run services update-traffic cornerstone-mcp \
#     --region us-central1 \
#     --to-revisions=<PREVIOUS_REVISION>=100
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-cornerstone-489916}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-cornerstone-mcp}"
GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/bin/gcloud}"
NO_TRAFFIC="${NO_TRAFFIC:-}"

if [ ! -x "$GCLOUD" ]; then
  echo "gcloud not found at $GCLOUD" >&2
  exit 1
fi

PROJECT_NUMBER="$("$GCLOUD" projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBLIC_URL="${PUBLIC_URL:-https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app}"

echo "== Project: $PROJECT_ID  Region: $REGION  Service: $SERVICE =="
echo "== Public URL: $PUBLIC_URL =="

TRAFFIC_FLAG=""
if [ -n "$NO_TRAFFIC" ]; then
  TRAFFIC_FLAG="--no-traffic"
  echo "== --no-traffic mode (revision will not receive traffic) =="
fi

"$GCLOUD" run deploy "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --source=. \
  --port=8080 \
  --min-instances=1 \
  --max-instances=5 \
  --memory=1Gi \
  --cpu=1 \
  --timeout=300s \
  --allow-unauthenticated \
  $TRAFFIC_FLAG \
  --set-env-vars="MCP_PUBLIC_URL=${PUBLIC_URL},DISABLE_OAUTH=true,CORNERSTONE_URL=https://cornerstone-api-${PROJECT_NUMBER}.${REGION}.run.app,CORNERSTONE_NAMESPACE=default,CORNERSTONE_AGENT_ID=obot-mcp" \
  --set-secrets="CORNERSTONE_API_KEY=cornerstone-mcp-api-key:latest,OAUTH_JWT_SECRET=cornerstone-mcp-oauth-jwt-secret:latest"

REV="$("$GCLOUD" run services describe "$SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.latestCreatedRevisionName)')"
echo "== Deployed revision: $REV =="
echo "== Done =="
