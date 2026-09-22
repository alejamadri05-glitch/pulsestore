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
| `beat-distribution` without `recording_id` reaching the database repeatedly | It is the only query that reads every row (807 buffers, ~56 ms on this tier). It is cached for 30 s per replica, so a steady stream of these means the cache is off (`DISTRIBUTION_CACHE_SECONDS=0`) or a writer is clearing it constantly | Check the variable; stop the writer, or pass `recording_id` |
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

## Symptom: `/healthz` returns 503 `database unreachable: …`

**The app is up and answering; it cannot reach PostgreSQL.** That is by design: the pool is
opened without waiting, so the container starts even when the database is down and reports the
failure, instead of dying at startup and crash-looping behind an opaque platform 503. (It did
the opposite until the Phase 11 load test found it — see `docs/results.md`.) The detail names
the exception class, which usually identifies the cause on its own.

1. **The database is stopped.** It is stopped between work sessions to save credit, and that
   is the most common cause by far.
   `az postgres flexible-server start -g rg-pulsestore -n pulsestore-pg-sjlnu`
2. **The firewall does not allow the app.** The working rule is `AllowAzureServices`
   (`0.0.0.0`). If the list instead shows a single pinned address, that is the failure mode the
   load test exposed: the app's outbound address rotates within a pool, so a replica that
   starts on another address cannot connect.
   `az postgres flexible-server firewall-rule list -g rg-pulsestore -s pulsestore-pg-sjlnu -o table`
3. **The `db-password` secret does not match the `pulse_app` role.** Re-run
   `./scripts/azure_app_password.sh`, which sets both sides at once.

## Symptom: HTTP 503 "App port is not ready"

This one comes from the platform, not the app, and means the process never started listening.
Since the app now survives an unreachable database, a missing **required** variable is the
likely cause: `DATABASE_URL` and `API_KEY` are both read at import and raise immediately, by
design, so a misconfigured revision fails visibly instead of serving 500s. Check them against
`.env.example`:

```bash
az containerapp show -g rg-pulsestore -n pulsestore-api \
  --query "properties.template.containers[0].env[].name" -o tsv
```

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

Database calls arrive as `postgresql` dependencies, but only because the image installs
`opentelemetry-instrumentation-psycopg`: the Azure package ships the psycopg2 one, and this
service uses psycopg 3. If dependencies are missing while requests still arrive, suspect that
package. Request duration, failures and the two ingest metrics come from the app itself.

## Rolling back a bad deploy

Revisions are immutable and images are tagged by commit, so a rollback is a deploy of the
previous commit. Find what is running and what ran before it:

```bash
az containerapp show -g rg-pulsestore -n pulsestore-api \
  --query "properties.template.containers[0].image" -o tsv
az containerapp revision list -g rg-pulsestore -n pulsestore-api \
  --query "[].{revision:name, image:properties.template.containers[0].image, created:properties.createdTime}" -o table
```

```bash
./scripts/deploy.sh <previous commit sha>
```

The script refuses a commit whose image was never published, waits for `/healthz`, and prints
what ended up running. The database is **not** rolled back: the migrations only ever add
objects, so an older image runs against a newer schema safely, but a rollback across a
migration that changed a column would need its own plan.

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
