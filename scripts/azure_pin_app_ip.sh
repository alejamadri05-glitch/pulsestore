#!/usr/bin/env bash
# Pins the database firewall to the Container App's current outbound IP address.
#
# Why this exists: `az containerapp show --query properties.outboundIpAddresses` returns nothing
# on the environments available in this subscription, and the address changes whenever the app is
# recreated. So the address is read where it is visible for certain: pg_stat_activity, after the
# app connects. The script opens the firewall to Azure services just long enough for the app to
# connect, reads the address, pins it, and closes the wide rule again.
#
# Run it from the repository root after any redeploy that recreates the app:
#   ./scripts/azure_pin_app_ip.sh
#
# Requires: az login as the server's Microsoft Entra admin, and .venv with psycopg installed.

set -euo pipefail

RG=${RG:-rg-pulsestore}
SERVER=${SERVER:-pulsestore-pg-sjlnu}
APP=${APP:-pulsestore-api}
DB=${DB:-pulsestore}
WIDE_RULE=${WIDE_RULE:-AllowAzureServicesTemporal}
PINNED_RULE=${PINNED_RULE:-app-container-salida}
AZ=${AZ:-/opt/homebrew/bin/az}

[ -x .venv/bin/python ] || { echo "Run this from the repository root (.venv/bin/python missing)"; exit 1; }

URL=$("$AZ" containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)

echo "1/4 Opening the firewall to Azure services, temporarily…"
"$AZ" postgres flexible-server firewall-rule create -g "$RG" -s "$SERVER" \
  --name "$WIDE_RULE" --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none

echo "2/4 Waking the app so that it connects (cold start included)…"
for _ in $(seq 1 8); do
  CODE=$(curl -s -m 90 -o /dev/null -w '%{http_code}' "https://$URL/healthz" || true)
  [ "$CODE" = "200" ] && break
  sleep 12
done
[ "$CODE" = "200" ] || { echo "The app is not healthy even with the wide rule; check its logs."; exit 1; }

echo "3/4 Reading the address the app connected from…"
IP=$(PGHOST="$SERVER.postgres.database.azure.com" \
     PGUSER="$("$AZ" account show --query user.name -o tsv)" \
     PGPASSWORD="$("$AZ" account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)" \
     DB="$DB" .venv/bin/python -c "
import os, psycopg
with psycopg.connect(dbname=os.environ['DB'], sslmode='require') as conn:
    row = conn.execute(\"\"\"SELECT host(client_addr) FROM pg_stat_activity
                            WHERE usename = 'pulse_app' AND client_addr IS NOT NULL
                            ORDER BY backend_start DESC LIMIT 1\"\"\").fetchone()
print(row[0] if row else '')")
[ -n "$IP" ] || { echo "No connection from pulse_app found; try again in a few seconds."; exit 1; }
echo "    $IP"

echo "4/4 Pinning that address and removing the wide rule…"
"$AZ" postgres flexible-server firewall-rule create -g "$RG" -s "$SERVER" \
  --name "$PINNED_RULE" --start-ip-address "$IP" --end-ip-address "$IP" -o none
"$AZ" postgres flexible-server firewall-rule delete -g "$RG" -s "$SERVER" --name "$WIDE_RULE" --yes -o none

"$AZ" postgres flexible-server firewall-rule list -g "$RG" -s "$SERVER" \
  --query "[].{rule:name, from:startIpAddress, to:endIpAddress}" -o table

echo
echo "Still healthy with the narrow rule:"
curl -s -m 90 -w ' [HTTP %{http_code}]\n' "https://$URL/healthz"
