# OKCard purchase storage

The service now saves purchases in PostgreSQL. `GET /okcard_history` reads saved
data; `POST /okcard_history/sync` imports purchases using the existing token cache.
The HTML and JSON response formats remain the same. Before the first successful
import, the GET endpoint returns HTTP 503 with instructions to sync.

The implementation uses the existing single configured OKCard account. Changing
`OKCARD_USER` selects a separate set of stored purchases. Tokens stay in memory.

## 1. Prepare the Linux service environment

Use Python 3.9 or later and an existing PostgreSQL database. The following setup
commands are for Ubuntu/Debian; use equivalent packages on other Linux systems.
Replace `YOUR_REPOSITORY_URL` with the Git URL for this project:

```sh
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip postgresql-client curl
git clone YOUR_REPOSITORY_URL "$HOME/OKCard"
cd "$HOME/OKCard"
python3 -m venv .venv
. .venv/bin/activate
```

Install dependencies and check the checkout:

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest -v test_okcard_history test_okcard_storage
```

Database integration tests are skipped until `TEST_DATABASE_URL` is configured
(step 5). No production OKCard calls are made by the tests.

Create a local environment file and edit it with your database and OKCard
credentials. The database and its login role must already exist; installing
`postgresql-client` only installs client tools.

```sh
touch .env
chmod 600 .env
nano .env
```

Use this format in `.env` (without `export`):

```dotenv
DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/DATABASE'
OKCARD_USER='your-login'
OKCARD_PASS='your-password'
```

The `.env` file is ignored by Git. The application and CLI do not load it
automatically. Load it into the current shell before running the CLI or starting
the API manually:

```sh
set -a
. ./.env
set +a
```

`DATABASE_URL` is required; the API and CLI exit at startup if it is not set.
There are no database settings in `main.py`. URL-encode special characters in
database credentials. `.env.example` lists every variable.
OKCard credentials now come from the environment; the previous hardcoded login
has been removed. Database reads require `OKCARD_USER`, but not `OKCARD_PASS`.

## 2. Inspect the actual purchase fields

```sh
python3 okcard_sync.py --inspect
```

This logs in and fetches the existing history response. It prints field names,
types, coverage, and distinct ID counts, without receipt values, login credentials,
or tokens. It does not connect to PostgreSQL or create tables.

Choose a **stable, top-level receipt/transaction identifier**, or a combination
of fields that identifies a receipt. All components must be present on every
purchase. A date, store name, or amount alone is not a reliable purchase ID, even
if it happens to be unique in this sample. For example, *only if the actual
response contains a suitable field named `receipt_id`*:

```sh
export OKCARD_PURCHASE_ID_FIELD='receipt_id'
```

This field name is illustrative; it is not assumed to be an actual OKCard API
field. The observed response has 155 purchases and 152 distinct `id` values, so
`id` alone is unsuitable. Run the updated inspection command to see
`identity_checks` and `repeated_id_groups`: these report which combinations are
unique and which fields differ between receipts sharing an ID, without printing
the values themselves.

Composite keys use comma-separated names in the same environment variable:

```sh
export OKCARD_PURCHASE_ID_FIELD='id,store_name,purchase_date'
```

This combination is a candidate for the observed response. It assumes the ID,
store name, and original purchase timestamp remain unchanged when prices/items
are corrected. Uniqueness in one fetch cannot establish that assumption. If the
timestamp is an update time, or store names are rewritten, this key would create
a new row instead of updating the original; investigate before importing. Do not
include price/items in the identity. Sync rejects duplicate composite keys too.

Add the chosen specification to `.env` too, for example, if the composite above
is suitable:

```dotenv
OKCARD_PURCHASE_ID_FIELD='id,store_name,purchase_date'
```

Reload `.env` using the `set -a` commands in step 1 after editing it. All four
variables must be available to the API process; setting them in a separate shell
does not update an already-running service. A missing identity field produces
`Set OKCARD_PURCHASE_ID_FIELD; run python okcard_sync.py --inspect first`.

Set the chosen specification in the running service environment as well. Keys
are encoded as JSON arrays to avoid delimiter collisions; the full receipt stays
in `raw_payload`. Existing single-field keys remain compatible, and no schema
change is needed for composite keys. After a successful import, changing fields
or their order requires an explicit identity migration. A failed first import
does not prevent correcting the configuration.

The fetch retains the existing single-response API behavior. Pagination and the
upstream retention window have not been verified; compare the initial imported
count against your app's available history before relying on it as a full archive.

## 3. Create the two new tables

Run the additive migration against the service database:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_okcard_history.sql
```

The script creates only `okcard_purchases`, `okcard_sync_state`, and their indexes.
It can be rerun for this initial schema. It does not alter existing tables. The
existing application startup also creates missing SQLModel tables, but applying
the migration explicitly lets you review database changes before starting the app.
Future schema changes need additional migrations; `CREATE TABLE IF NOT EXISTS`
does not upgrade an existing table definition.

### Start manually

From the repository directory, activate the virtual environment and load the
configuration, then start the API:

```sh
cd "$HOME/OKCard"
. .venv/bin/activate
set -a
. ./.env
set +a
python3 -m uvicorn main:app --host 0.0.0.0 --port 3007
```

Leave this terminal running and use a second terminal for the curl commands in
step 4. To restart a manual run, press `Ctrl+C` in its terminal, then repeat the
commands above. This reloads both the code and environment configuration.

### Run continuously with systemd

Create a service so the API keeps running after logout and starts after reboot:

```sh
sudo nano /etc/systemd/system/okcard-api.service
```

Paste the following unit, replacing `YOUR_USER` and every `/home/YOUR_USER/OKCard`
path with your Linux login and the actual absolute clone path. Use an ordinary
user with access to the checkout and database:

```ini
[Unit]
Description=OKCard API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/OKCard
EnvironmentFile=/home/YOUR_USER/OKCard/.env
ExecStart=/home/YOUR_USER/OKCard/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 3007
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Stop any manual API run first so port 3007 is available. Then enable and start
the service:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now okcard-api.service
sudo systemctl status okcard-api.service --no-pager
sudo journalctl -u okcard-api.service -n 50 --no-pager
```

### Restart or update the API

After editing `.env` or application files, restart the API to load the changes:

```sh
sudo systemctl restart okcard-api.service
sudo systemctl status okcard-api.service --no-pager
```

To deploy a Git update, run these commands as the user who owns the checkout:

```sh
cd "$HOME/OKCard"
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v test_okcard_history test_okcard_storage
```

Continue only if the pull, installation, and tests succeed. Follow any new
migration instructions before restarting with the commands above. If you edit
the systemd unit itself, run `sudo systemctl daemon-reload` before restarting.
Verify the running API after each restart:

```sh
curl --fail --show-error http://127.0.0.1:3007/okcard_history/sync_status
```

To stop the API, use `sudo systemctl stop okcard-api.service`. The separate
`okcard-sync.service` in step 6 only triggers an import; restarting it does not
restart the API.

## 4. Import and verify

```sh
curl --fail --show-error -X POST http://127.0.0.1:3007/okcard_history/sync
curl --fail --show-error http://127.0.0.1:3007/okcard_history/sync_status
curl --fail --show-error 'http://127.0.0.1:3007/okcard_history?year_month=2026-09&format=json'
```

Sync returns `fetched`, `inserted`, `updated`, `unchanged`, and `last_success_at`.
Repeat the POST: unchanged receipts should not create more rows. Compare monthly
purchase counts, item counts, and totals with the app. Open the same GET URL
without `format=json` to check the HTML page.

You can also import without running the web service:

```sh
python3 okcard_sync.py
```

The standalone CLI creates a new process and therefore logs in anew. Calling the
running API reuses that worker's cached token. Both paths use the same database
lock and sync implementation.

If sync reports HTTP 400 with `error_code: a001` (`invalid token error`), the
upstream history API rejected the token. The service now evicts that token,
logs in again, and retries once. Deploy the updated `main.py` and restart all
API workers to enable this handling. Other HTTP 400 errors are not retried.
If the same error persists after the retry, run `python3 okcard_sync.py --inspect`
with the same service credentials to check a fresh login independently of the
running worker's cache. A rejected fresh token needs further investigation;
repeated sync calls will not fix it. Existing saved purchases are preserved.

Data behavior:

- Every returned purchase is validated before any purchase rows are written.
  Missing dates/stores/items, invalid numbers, malformed responses, and duplicate
  IDs fail the batch. Empty `items` lists are allowed. Whole-yen and fractional
  amounts up to four decimal places are preserved in `NUMERIC(18,4)`.
- Raw purchase objects, including items and unknown fields, are saved as JSONB.
  A repeated ID updates the existing receipt; `first_seen_at` stays unchanged.
- Purchases and the successful-sync marker commit together. A failed sync keeps
  the previous purchases and success timestamp; failure status is recorded in a
  separate transaction when the database is available.
- A PostgreSQL advisory lock serializes syncs across workers. A concurrent sync
  receives HTTP 409. Network fetches happen without an open write transaction.
- Stored purchases are not deleted when missing from a later response. Explicit
  upstream deletion/refund reconciliation is not implemented. Corrections under
  the same ID are updated normally.
- Dates without a timezone are interpreted as Japan time. Month boundaries use
  Japan time; original date strings are preserved in responses. Confirm the
  source timestamp semantics during the first import.
- A successfully imported empty list is different from never having synced.
  Reads return an empty month after a successful sync, and HTTP 503 beforehand.
- Check `/okcard_history/sync_status` for freshness. The existing HTML and JSON
  bodies do not add freshness fields.

## 5. Run PostgreSQL integration tests

Use a dedicated test database and a role allowed to create schemas:

```sh
export TEST_DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/okcard_test'
python3 -m unittest -v test_okcard_history test_okcard_storage
```

The suite creates a randomly named `okcard_test_*` schema, applies the shipped
migration twice, tests storage, and drops that schema afterward. It verifies
idempotent imports, corrections, account isolation, Japan month boundaries,
rollback after write failure, retained history after upstream failure, and
concurrent sync exclusion. It never contacts the real OKCard API.

## 6. Enable daily imports after verification

The included systemd timer calls the running API daily at midnight (00:00 Japan
time), independent of the server's local timezone. With `Persistent=true`, a
missed scheduled run is triggered when the timer becomes active again.
Adjust the URL/port in `deploy/okcard-sync.service` to match your deployment.
The API must already be running and have the environment variables above.

```sh
sudo cp deploy/okcard-sync.service deploy/okcard-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okcard-sync.timer
sudo systemctl restart okcard-sync.timer
systemctl list-timers okcard-sync.timer
```

Test a scheduled invocation and inspect its result:

```sh
sudo systemctl start okcard-sync.service
journalctl -u okcard-sync.service -n 30 --no-pager
```

The timer uses the existing API access setup; no separate scheduler runs in each
Uvicorn worker. The service preserves saved history if an import fails and tries
again at the next scheduled invocation (or a manual POST).

## 7. Enable daily BOCD exchange-rate fetches

The API no longer runs an in-process fetch loop. A systemd timer calls the
running API once a day at 10:00 China time, matching how OKCard imports are
scheduled. Rates are upserted by publish time and currency, so repeated fetches
do not create duplicate rows. Adjust the URL/port in `deploy/bocd-fetch.service`
to match your deployment.

```sh
sudo cp deploy/bocd-fetch.service deploy/bocd-fetch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bocd-fetch.timer
systemctl list-timers bocd-fetch.timer
```

Run a fetch now and inspect its result:

```sh
sudo systemctl start bocd-fetch.service
journalctl -u bocd-fetch.service -n 30 --no-pager
curl --fail --show-error http://127.0.0.1:3007/latest_jpy_sell
```

A failed fetch makes the service unit fail (visible in `journalctl` and
`systemctl --failed`); the next scheduled run, or a manual
`POST /fetch_bocd_exchange_rates`, tries again.
