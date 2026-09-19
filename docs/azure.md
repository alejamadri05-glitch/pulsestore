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

**No passwords anywhere.** Password authentication is disabled on the server. The admin is a
Microsoft Entra user who connects with a short-lived token from `az`; the API will connect with
a managed identity (next phase). This is the "stretch" goal of the build guide's Phase 7.

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

## Things that went wrong while creating it

Recorded because each one will happen to the next person too:

1. `--high-availability` no longer exists in Azure CLI 2.90; it is `--zonal-resiliency`.
2. The required-tags policy reported two tags on the first refusal and five more on the next.
3. The create command made the server, then failed adding the firewall rule with
   `ServerIsBusy`: the server was still finishing. The rule had to be added separately
   (`firewall-rule create -s <server> --name <rule>`; the flag is `--name`, not `--rule-name`).

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
