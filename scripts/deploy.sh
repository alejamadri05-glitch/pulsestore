#!/usr/bin/env bash
# Deploys a commit to Azure Container Apps.
#
# GitHub Actions cannot do this for us: an OIDC deploy needs either an Entra app registration
# (refused: "Insufficient privileges to complete the operation" — directory permissions, which
# a subscription Owner does not have) or a user-assigned managed identity (denied by policy).
# So the deploy is one command a human runs, and CI verifies everything it can beforehand:
# lint, tests against a real PostgreSQL, and a smoke test of the published image.
#
#   ./scripts/deploy.sh              # deploys the current HEAD
#   ./scripts/deploy.sh <commit sha> # deploys a specific commit
#
# Idempotent: deploying the same commit twice is a no-op for the app.

set -euo pipefail

RG=${RG:-rg-pulsestore}
APP=${APP:-pulsestore-api}
IMAGE_REPO=${IMAGE_REPO:-ghcr.io/alejamadri05-glitch/pulsestore}
AZ=${AZ:-/opt/homebrew/bin/az}

SHA=${1:-$(git rev-parse HEAD)}
SHORT=${SHA:0:7}

echo "1/3 Checking that $IMAGE_REPO:$SHORT is published…"
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:${IMAGE_REPO#ghcr.io/}:pull&service=ghcr.io" |
  python3 -c "import sys, json; print(json.load(sys.stdin)['token'])")
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json" \
  "https://ghcr.io/v2/${IMAGE_REPO#ghcr.io/}/manifests/$SHA")
[ "$CODE" = "200" ] || { echo "No image for $SHORT (HTTP $CODE). Has CI finished?"; exit 1; }

echo "2/3 Pointing $APP at that image…"
"$AZ" containerapp update -g "$RG" -n "$APP" --image "$IMAGE_REPO:$SHA" -o none

echo "3/3 Verifying the deployment…"
URL=$("$AZ" containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
for _ in $(seq 1 10); do
  BODY=$(curl -s -m 90 "https://$URL/healthz" || true)
  case "$BODY" in *'"status":"ok"'*) break ;; esac
  sleep 10
done
case "$BODY" in
  *'"status":"ok"'*) : ;;
  *) echo "Not healthy after the deploy: $BODY"; exit 1 ;;
esac

RUNNING=$("$AZ" containerapp show -g "$RG" -n "$APP" \
  --query "properties.template.containers[0].image" -o tsv)
echo
echo "Deployed:  $RUNNING"
echo "Health:    $BODY"
echo "URL:       https://$URL/docs"
