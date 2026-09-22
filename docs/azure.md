# Azure deployment

What runs in Azure, how it was created, and what the subscription's rules forced. Every
command here was run as written; nothing in this file is a secret.

## Subscription constraints

The subscription is Azure for Students (USD 100 of credit, no card attached). Two sets of rules
shaped every command below:

- **Allowed regions:** a built-in policy limits deployments to `canadacentral`,
  `northcentralus`, `spaincentral`, `chilecentral` and `mexicocentral`. The build guide's
  `eastus` is refused. **`northcentralus`** was chosen: it has PostgreSQL Flexible Server,
  Container Apps and Application Insights, and the cheapest B1ms of the five
  (USD 0.017/hour, from the Azure retail price API).
- **Required tags:** the university that issues the student identity enforces, at management
  group level, a deny policy on any resource without seven tags. The error reveals only some
  of them per attempt, so the full list is recorded here. Resource groups are exempt.

| Tag | Value used |
|---|---|
| `Responsable` | owner's university email |
| `FechaCreacion` | creation date, `YYYY-MM-DD` |
| `Proyecto` | `PulseStore` |
| `Entorno` | `Desarrollo` |
| `ClasificacionDatos` | `Publica` (MIT-BIH is public and de-identified) |
| `UnidadOrganizacional` | `N/A` |
| `CentroCostos` | `N/A` |

## Database: Azure Database for PostgreSQL Flexible Server

```bash
az group create --name rg-pulsestore --location northcentralus

az postgres flexible-server create \
  --resource-group rg-pulsestore --name pulsestore-pg-sjlnu --location northcentralus \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --storage-auto-grow Disabled \
  --version 16 --zonal-resiliency Disabled --backup-retention 7 \
  --microsoft-entra-auth Enabled --password-auth Disabled \
  --admin-object-id "$(az ad signed-in-user show --query id -o tsv)" \
  --admin-display-name "$(az account show --query user.name -o tsv)" --admin-type User \
  --public-access <your IPv4> \
  --tags Responsable=<email> FechaCreacion=<date> Proyecto=PulseStore Entorno=Desarrollo \
         ClasificacionDatos=Publica UnidadOrganizacional=N/A CentroCostos=N/A \
  --yes

az postgres flexible-server db create -g rg-pulsestore -s pulsestore-pg-sjlnu --name pulsestore
```

**The server was created with no passwords at all.** Password authentication was disabled and
the admin is a Microsoft Entra user who connects with a short-lived token from `az`. This is
the "stretch" goal of the build guide's Phase 7.

> **Superseded for the application.** Password authentication was re-enabled later, because
> the managed identity this design depended on is not available in this subscription. Human
> access is still Entra-only. The section *Managed identity was the plan, and it is not
> possible here* below has the evidence and what it costs.

```bash
export PGHOST=pulsestore-pg-sjlnu.postgres.database.azure.com
export PGUSER="$(az account show --query user.name -o tsv)"
export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
psql "dbname=pulsestore sslmode=require"      # the token lasts about an hour
```

### Migrations and `pg_stat_statements`

The three migrations were applied as the Entra admin, unchanged from local. The result was
checked, not assumed: four tables, both Phase 6 indexes, and `pulse_app` with exactly
`SELECT, INSERT` on the four tables plus `UPDATE (model)` on `devices`.

`pg_stat_statements` needed two server parameters, both dynamic (no restart):

```bash
az postgres flexible-server parameter set -g rg-pulsestore -s pulsestore-pg-sjlnu \
  --name azure.extensions --value PG_STAT_STATEMENTS
az postgres flexible-server parameter set -g rg-pulsestore -s pulsestore-pg-sjlnu \
  --name pg_stat_statements.track --value top
```

**Trap worth knowing:** on this server `pg_stat_statements.track` defaults to `none`. The
extension can be created and queried, and it records nothing. The library itself was already
in `shared_preload_libraries`.

### Differences from the local database that matter

| Setting | Local (Docker) | Azure | Why it matters |
|---|---|---|---|
| `random_page_cost` | 4 | **2** | Phase 6 found the heart-rate query sorting after a bitmap scan because index scans looked expensive. With 2, the planner may choose an ordered index scan instead. To be measured once data is loaded. |
| Authentication | password | Entra token only | The API needs a token-refreshing connection instead of a static URL. |
| Round trip from the author's laptop (Costa Rica) | < 1 ms | ~110 ms per statement | Network, not the database. The API will run next to the database, so its queries do not pay this. |
| New connection | a few ms | ~1.2 s (TLS 1.3 + token validation) | Why the API keeps a connection pool rather than connecting per request. |

## The API on Azure Container Apps

```bash
az containerapp env create -g rg-pulsestore -n cae-pulsestore-wp -l northcentralus \
  --enable-workload-profiles --logs-destination log-analytics \
  --logs-workspace-id <workspace> --logs-workspace-key <key> --tags <the seven tags>

az containerapp create -g rg-pulsestore -n pulsestore-api --environment cae-pulsestore-wp \
  --image ghcr.io/alejamadri05-glitch/pulsestore:latest \
  --target-port 8000 --ingress external \
  --min-replicas 0 --max-replicas 2 --cpu 0.25 --memory 0.5Gi \
  --secrets api-key=<generated> db-password=<generated> \
  --env-vars DB_AUTH=password \
    "DATABASE_URL=postgresql://pulse_app@<server>.postgres.database.azure.com:5432/pulsestore?sslmode=require" \
    PGPASSWORD=secretref:db-password API_KEY=secretref:api-key \
  --tags <the seven tags>
```

Measured from Costa Rica: **0.33 s warm**, **~7 s cold start** (the app scales to zero, so the
first request after an idle period pays for the container start and the connection pool).
`/docs` is public; every other endpoint answers 401 without the API key, and with a wrong one.

### Managed identity was the plan, and it is not possible here

The design was passwordless: the app would take an Entra token from a managed identity, exactly
as the laptop does with `az login`. It is implemented in `app/db.py` (`DB_AUTH=entra`) and
verified against this server. It is **not** what the deployed app uses, for a platform reason:

1. **User-assigned identities are denied** by a policy on the subscription's management group
   (`Deny user-assigned managed identities`, effect `deny`, targeting
   `Microsoft.ManagedIdentity/userAssignedIdentities`).
2. **System-assigned identity is accepted at creation but breaks every later update.** The app
   was created with one and it worked: it authenticated to PostgreSQL as a role mapped with
   `pgaadauth_create_principal_with_oid` and served requests. But any update, through the CLI or
   a direct ARM PATCH, is refused with `ExpressEnvironmentFeatureNotSupported: 'System-assigned
   managed identity' is not supported for container app on express environments`. An immutable
   app cannot receive the Application Insights variable of the next phase, and the CI/CD deploy
   action of the phase after that would fail the same way.
3. **Express is the only environment mode available.** Managed identity is on the documented
   list of features express does not support yet. Creating a standard environment returns
   `ManagedEnvironmentModeNotSupported: The managed environment mode Standard is not currently
   available in this region` — and the same in all five regions this subscription allows
   (`northcentralus`, `canadacentral`, `mexicocentral`, `spaincentral`, `chilecentral`).

So the deployed app authenticates with a password, and the code keeps both modes:

| `DB_AUTH` | Used by | Credential |
|---|---|---|
| `password` | CI, local database, **the deployed app** | Password of `pulse_app`, a Container Apps secret injected as `PGPASSWORD` |
| `entra` | Any laptop or host that has an Entra identity | A token per new connection, no stored secret |

**What that costs in security**, stated plainly: there is now a password for `pulse_app` and one
for the server admin, where the design had none. Both are 25 random characters, generated on the
operator's machine by `scripts/azure_app_password.sh`, never printed, stored in a file readable
only by that user and meant to be moved into a password manager. Human access still uses Entra
only. The alternative was Azure App Service (~USD 13/month), which supports managed identity;
it was rejected for a portfolio project whose whole compute bill is otherwise zero.

### The outbound IP changes, and pinning it turned out to be a bad idea

`az containerapp show --query properties.outboundIpAddresses` returns nothing on these
environments, and the address changes when the app is recreated: it moved from `52.162.220.32`
to `135.232.251.133` in one afternoon. The first answer was to pin it with a script
(`scripts/azure_pin_app_ip.sh`): open the firewall to Azure services, wake the app, read the
address it actually connected from in `pg_stat_activity`, pin that, remove the wide rule.

**The Phase 11 load test proved that wrong.** The address does not merely change on redeploy;
it rotates *within a pool* while the app runs, so a second replica starting on another address
could not reach the database at all. The firewall now allows Azure services, and the control
that matters is authentication: the `pulse_app` password, over TLS, with a role that cannot
delete a row.

The honest trade-off, stated the other way round from before: `0.0.0.0` means any Azure
customer's resources can reach the port, so the database's safety rests entirely on
authentication and on least privilege — not on the network. Pinning is kept in the repository
as optional hardening, and its header says what it costs: re-pinning after every replica
change, or an outage.

## Monitoring, alerting and the drill

```bash
# Application Insights, workspace-based (free within the workspace's 5 GB/month)
az rest --method put --url ".../microsoft.insights/components/ai-pulsestore?api-version=2020-02-02" \
  --body '{"location":"northcentralus","kind":"web","properties":{"Application_Type":"web",
           "WorkspaceResourceId":"<log-pulsestore id>","IngestionMode":"LogAnalytics"}}'

az containerapp secret set -g rg-pulsestore -n pulsestore-api \
  --secrets "appinsights-connection=<connection string>"
az containerapp update -g rg-pulsestore -n pulsestore-api \
  --image ghcr.io/alejamadri05-glitch/pulsestore:<commit sha> \
  --set-env-vars APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection

az monitor action-group create -g rg-pulsestore -n ag-pulsestore --short-name pulsestore \
  --action email alejandro <email> --tags <the seven tags>
az monitor metrics alert create -n pg-cpu-high -g rg-pulsestore --scopes <server id> \
  --condition "avg cpu_percent > 80" --window-size 5m --evaluation-frequency 1m --severity 2 \
  --action <action group id> --tags <the seven tags>
```

The image is pinned by commit SHA, not `latest`, so the running revision names the code it runs.

**Measured in production**, server side, from Application Insights after the deployment:

| Signal | Value |
|---|---|
| `GET /recordings/{id}/heart-rate` | p95 12 ms |
| `GET /stats/beat-distribution` | p95 21 ms |
| `postgresql` `SELECT` dependencies | p95 8 ms |
| `annotations_ingested` / `ingest_batch_size` | 120 beats, batch of 120, from a real ingest |

**Two instrumentation traps, both found by looking instead of assuming:**

1. `configure_azure_monitor()` sent metrics, performance counters and its own dependencies, but
   `AppRequests` stayed empty. It instruments the FastAPI *class*, and that did not cover this
   app; `FastAPIInstrumentor.instrument_app(app)` on the instance fixed it.
2. Database calls needed `opentelemetry-instrumentation-psycopg`: the Azure package only ships
   the psycopg2 one. They now arrive as `postgresql` dependencies, and the pooled connections
   are traced (checked with an in-memory exporter before trusting it).

Reading logs and querying telemetry on these environments:

```bash
# `az containerapp logs show` fails here (KeyError: 'eventStreamEndpoint') and the
# log-analytics CLI extension will not install on this CLI build. Query the API directly:
az rest --method post --url "https://api.loganalytics.io/v1/workspaces/<customerId>/query" \
  --resource "https://api.loganalytics.io" \
  --body '{"query":"AppRequests | where TimeGenerated > ago(1h) | summarize p95=percentile(DurationMs,95) by Name"}'
```

The alert was fired on purpose and written up in
[`docs/postmortems/2026-09-22-cpu-alert-drill.md`](postmortems/2026-09-22-cpu-alert-drill.md):
CPU held 83.3 % over five minutes, `pg_stat_statements` named the offending query in seconds,
and `/healthz` stayed green the whole time, which is exactly why a health check is not a
capacity alarm.

## How a change reaches production

There is no deploy job in CI, for the same reason there is no managed identity: an OIDC login
to Azure needs either an Entra app registration — `az ad app create` is refused with
*Insufficient privileges to complete the operation*, which a subscription Owner cannot grant
itself because it is a directory permission — or a user-assigned identity, denied by policy.

So the last step is a human running one command, and everything before it is automated:

| Step | Who | What it proves |
|---|---|---|
| `ruff` + `pytest` against a real PostgreSQL 16 | CI, every push and PR | The code works against a real database, as the least-privilege role |
| Build and push `ghcr.io/…:<sha>` | CI, `main` only | The registry never holds an image the tests rejected |
| Boot that image against PostgreSQL and call it | CI, `main` only | The **artifact** starts, connects and enforces its API key — not just the source |
| `./scripts/deploy.sh <sha>` | a person | The image exists, the app points at it, and `/healthz` is green afterwards |

`deploy.sh` is idempotent and names what it deployed; `docs/runbook.md` covers rolling back.

## Things that went wrong while creating it

Recorded because each one will happen to the next person too:

1. `--high-availability` no longer exists in Azure CLI 2.90; it is `--zonal-resiliency`.
2. The required-tags policy reported two tags on the first refusal and five more on the next.
3. The create command made the server, then failed adding the firewall rule with
   `ServerIsBusy`: the server was still finishing. The rule had to be added separately
   (`firewall-rule create -s <server> --name <rule>`; the flag is `--name`, not `--rule-name`).
4. Enabling password authentication needs an administrator login and password in the same
   request; `az postgres flexible-server update --password-auth Enabled` crashed with an
   internal traceback, and an ARM PATCH without the administrator fields was accepted and
   silently changed nothing. `scripts/azure_app_password.sh` sends all three together.
5. `az containerapp logs show` fails on express environments (`KeyError: 'eventStreamEndpoint'`)
   and the `log-analytics` CLI extension does not install on this CLI build, so container logs
   have to be read in the portal.

None of the failed attempts created a billable resource.

## Cost

| Resource | Price | Notes |
|---|---|---|
| PostgreSQL B1ms, North Central US | USD 0.017/hour while running | ~USD 12.4/month if never stopped |
| 32 GB storage | ~USD 3.7/month | Billed even while the server is stopped (list price, approximate) |
| Budget alert `pulsestore-mensual` | free | USD 25/month; emails at 80 % actual and 100 % forecast |

Stop the server when not working on it; Azure restarts a stopped server automatically after
seven days:

```bash
az postgres flexible-server stop  -g rg-pulsestore -n pulsestore-pg-sjlnu
az postgres flexible-server start -g rg-pulsestore -n pulsestore-pg-sjlnu
```

When the project is over, `az group delete -n rg-pulsestore` removes everything.
