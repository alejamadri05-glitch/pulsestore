#!/usr/bin/env bash
# Gives the deployed API a database password, because managed identity is unavailable:
# Azure Container Apps environments in this subscription are "express" only, and express
# environments do not support managed identity (docs/azure.md explains the constraint).
#
# Run it yourself, from the repository root. It generates both secrets locally, applies them,
# and writes them to a file only you can read. No secret is printed to the terminal.
#
#   ./scripts/azure_app_password.sh
#
# Requires: az login as the server's Microsoft Entra admin, and .venv with psycopg installed.

set -euo pipefail

RG=${RG:-rg-pulsestore}
SERVER=${SERVER:-pulsestore-pg-sjlnu}
APP=${APP:-pulsestore-api}
ADMIN_USER=${ADMIN_USER:-pulseadmin}
SECRETS_FILE=${SECRETS_FILE:-$HOME/pulsestore-secrets.txt}
AZ=${AZ:-/opt/homebrew/bin/az}

command -v "$AZ" >/dev/null || { echo "Azure CLI not found at $AZ; set AZ=/path/to/az"; exit 1; }
[ -x .venv/bin/python ] || { echo "Run this from the repository root (.venv/bin/python missing)"; exit 1; }

# 25 characters, letters and digits only: safe inside connection strings and shell quoting.
# Generated with Python rather than `tr </dev/urandom | head`: head closes the pipe early, which
# with `set -o pipefail` aborts the script (exit 141) before it does anything.
gen_password() { .venv/bin/python -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(25)))"; }
ADMIN_PW=$(gen_password)
APP_PW=$(gen_password)

echo "1/4 Enabling password authentication on $SERVER (Entra stays enabled)…"
SUB=$("$AZ" account show --query id -o tsv)
"$AZ" rest --method patch \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.DBforPostgreSQL/flexibleServers/$SERVER?api-version=2024-08-01" \
  --body "{\"properties\":{\"administratorLogin\":\"$ADMIN_USER\",\"administratorLoginPassword\":\"$ADMIN_PW\",\"authConfig\":{\"activeDirectoryAuth\":\"Enabled\",\"passwordAuth\":\"Enabled\"}}}" \
  -o none

for _ in $(seq 1 30); do
  STATE=$("$AZ" postgres flexible-server show -g "$RG" -n "$SERVER" --query authConfig.passwordAuth -o tsv)
  [ "$STATE" = "Enabled" ] && break
  sleep 10
done
[ "$STATE" = "Enabled" ] || { echo "Password authentication is still $STATE; stopping."; exit 1; }

echo "2/4 Setting the password of the pulse_app role…"
PGHOST="$SERVER.postgres.database.azure.com" \
PGUSER="$("$AZ" account show --query user.name -o tsv)" \
PGPASSWORD="$("$AZ" account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)" \
APP_PW="$APP_PW" .venv/bin/python - <<'PY'
import os
import psycopg
from psycopg import sql

with psycopg.connect(dbname="pulsestore", sslmode="require", autocommit=True) as conn:
    # ALTER ROLE takes no bound parameters; sql.Literal quotes the value safely.
    conn.execute(sql.SQL("ALTER ROLE pulse_app WITH LOGIN PASSWORD {}").format(sql.Literal(os.environ["APP_PW"])))
    print("   pulse_app can now log in with a password")
PY

echo "3/4 Storing it as a Container Apps secret…"
"$AZ" containerapp secret set -g "$RG" -n "$APP" --secrets "db-password=$APP_PW" -o none
REVISION=$("$AZ" containerapp show -g "$RG" -n "$APP" --query properties.latestRevisionName -o tsv)
"$AZ" containerapp revision restart -g "$RG" -n "$APP" --revision "$REVISION" -o none 2>/dev/null || true

echo "4/4 Writing both secrets to $SECRETS_FILE (readable only by you)…"
umask 077
cat > "$SECRETS_FILE" <<EOF
PulseStore secrets, generated $(date +%F)

Server admin (PostgreSQL, $SERVER):
  user:     $ADMIN_USER
  password: $ADMIN_PW

Application role (used by the Container App):
  user:     pulse_app
  password: $APP_PW

Move these to a password manager and delete this file.
The API key of the deployed service is a separate secret; read it with:
  $AZ containerapp secret show -g $RG -n $APP --secret-name api-key --query value -o tsv
EOF
chmod 600 "$SECRETS_FILE"

echo
echo "Done. Check the service:"
echo "  curl https://$("$AZ" containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)/healthz"
