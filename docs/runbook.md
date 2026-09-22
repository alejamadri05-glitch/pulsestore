# PulseStore runbook

What to do when something pages you. Written to be followed at 3 a.m. by someone who did not
build this, which in practice means: the commands are here, not in someone's shell history.

| | |
|---|---|
| Service | `pulsestore-api` on Azure Container Apps (`cae-pulsestore-wp`, North Central US) |
| Database | `pulsestore-pg-sjlnu`, Azure Database for PostgreSQL Flexible Server, Burstable B1ms |
| Telemetry | Application Insights `ai-pulsestore`, backed by Log Analytics `log-pulsestore` |
| Alerts | Action group `ag-pulsestore` (email) |
| Public URL | `https://pulsestore-api.calmmoss-34df9f8f.northcentralus.azurecontainerapps.io` |

Every command below assumes `az login` as the subscription owner and `RG=rg-pulsestore`.

## First 60 seconds, whatever the alert says

```bash
az containerapp show -g rg-pulsestore -n pulsestore-api --query properties.runningStatus -o tsv
az postgres flexible-server show -g rg-pulsestore -n pulsestore-pg-sjlnu --query state -o tsv
curl -s -o /dev/null -w '%{http_code} in %{time_total}s\n' \
  https://pulsestore-api.calmmoss-34df9f8f.northcentralus.azurecontainerapps.io/healthz
```

`/healthz` runs `SELECT 1`, so a 200 means the API is up **and** can reach the database. It also
reports whether telemetry is enabled, which separates "broken" from "misconfigured".

## Alert: `pg-cpu-high` (database CPU above 80 % for 5 minutes)

**What it means.** The B1ms tier has one shared vCPU. Sustained CPU above 80 % means queries are
queueing, so API latency rises even though nothing is down. It does not mean data is at risk.

**Step 1: see what is running.** In Application Insights → Logs:

```kql
requests
| where timestamp > ago(1h)
| summarize p50 = percentile(duration, 50), p95 = percentile(duration, 95),
            failures = countif(success == false), total = count()
  by bin(timestamp, 5m), name
| order by timestamp desc
```

**Step 2: find the expensive statement.** `pg_stat_statements` is enabled with `track = top`:

```sql
SELECT calls, round(mean_exec_time::numeric, 2) AS mean_ms,
       round(total_exec_time::numeric) AS total_ms, left(query, 70) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

Connect with an Entra token, no password needed:

```bash
PGHOST=pulsestore-pg-sjlnu.postgres.database.azure.com \
PGUSER="$(az account show --query user.name -o tsv)" \
PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)" \
  psql "dbname=pulsestore sslmode=require"
```

**Step 3: match it to a known cause.**

| What you see | Likely cause | Action |
|---|---|---|
| A sequential scan on `annotations` in `EXPLAIN` | An index is missing, usually after a migration or a restore | Re-apply `migrations/002_indexes.sql`; it is idempotent enough to read first |
| One client hammering `POST /recordings/{id}/annotations` | A loader retry loop, or a backfill someone started | Stop the loader; ingest is not idempotent per segment, so check for 409s before restarting |
| `beat-distribution` without `recording_id` called repeatedly | The only query that reads every row (807 buffers, ~56 ms on this tier) | Cache it, or always pass `recording_id`; see docs/results.md |
| High CPU with no slow statement | The burstable tier exhausted its CPU credits | Scale the tier temporarily (below), or wait for credits to refill |

**Step 4: recover.** In increasing order of disruption:

```bash
# 1. Cap the damage: let fewer requests through at once.
az containerapp update -g rg-pulsestore -n pulsestore-api --max-replicas 1

# 2. Give the database more CPU (billed while it lasts; revert afterwards).
az postgres flexible-server update -g rg-pulsestore -n pulsestore-pg-sjlnu \
  --tier GeneralPurpose --sku-name Standard_D2ds_v5

# 3. Last resort: stop ingest entirely by scaling the API to zero.
az containerapp update -g rg-pulsestore -n pulsestore-api --min-replicas 0 --max-replicas 0
```

**Step 5: write it down.** A short blameless note in `docs/postmortems/`: what fired, what you
checked, what fixed it, what you would automate next time.

## Symptom: HTTP 503 "App port is not ready"

The container starts but its port never opens. The pool is opened at startup with
`wait=True`, so **the app refuses to start when it cannot reach the database**. Almost always
one of these:

1. **The firewall does not list the app's outbound IP.** It changes whenever the app is
   recreated, and ARM does not expose it. Fix: `./scripts/azure_pin_app_ip.sh`.
2. **The database is stopped.** `az postgres flexible-server start -g rg-pulsestore -n pulsestore-pg-sjlnu`.
3. **The `db-password` secret does not match the `pulse_app` role.** Re-run
   `./scripts/azure_app_password.sh`, which sets both sides at once.

Container logs are not available through `az containerapp logs show` on these environments
(`KeyError: 'eventStreamEndpoint'`); read them in the portal, under the app's *Log stream*, or
query `ContainerAppConsoleLogs_CL` in Log Analytics.

## Symptom: every request returns 401

The API key changed or was rotated. Read the current one:

```bash
az containerapp secret show -g rg-pulsestore -n pulsestore-api --secret-name api-key --query value -o tsv
```

## Symptom: telemetry stopped arriving

`/healthz` reports `"telemetry": false` when `APPLICATIONINSIGHTS_CONNECTION_STRING` is missing
from the revision. Check the env var and the secret it references:

```bash
az containerapp show -g rg-pulsestore -n pulsestore-api \
  --query "properties.template.containers[0].env[].name" -o tsv
```

Note that database calls never appear as dependencies: the Azure package instruments psycopg2
and this service uses psycopg 3. Request duration, failures and the two ingest metrics do.

## Cost controls

The database bills while it runs, so it is stopped between work sessions. Azure restarts a
stopped server automatically after seven days.

```bash
az postgres flexible-server stop  -g rg-pulsestore -n pulsestore-pg-sjlnu
az postgres flexible-server start -g rg-pulsestore -n pulsestore-pg-sjlnu
```

The API scales to zero on its own and costs nothing while idle. A budget alert
(`pulsestore-mensual`, USD 25/month) emails at 80 % actual and 100 % forecast spend.

To remove everything: `az group delete -n rg-pulsestore`.
