# Working on vaad_bait

A ועד בית (building committee) app for one treasurer: monthly dues for 12
apartments, cheques and transfers, numbered PDF receipts sent on WhatsApp,
expenses, the bank and treasurer accounts, and yearly reports. Hebrew, RTL,
mobile-first. Live at https://vaad.asadodevculture.com, about $1/month.

`PLAN.md` is the original design; this file is the current state. `README.md`
has first-time setup.

## How to work here (the user's rules)

- **Local first, ship at the end.** Iterate on `make dev` (localhost:8000,
  `.devdata/`, login skipped) and commit locally. Only when the user says
  "ship it": run tests, `git push`, `make apply`, check Lambda logs. Don't
  deploy or push after every change.
- **Never touch `~/projects/aws_gallery`** or any of its AWS resources (code,
  git, buckets, state bucket). Reading it for patterns is fine.
- AWS profile is `personal` (`config.env`, gitignored). The other local
  profiles are work accounts; never use them here.
- Repo: `git@github-personal:javitolin/vaad_bait.git`, branch `main`, commit
  as the user's personal identity (set in this repo's git config). No
  force-push without asking.
- Present plans for review in Crit when the user asks for a plan review.
- Personal data (residents' names, phones) lives only in the database, never in
  the repo, tests or commit messages. Use made-up names in tests.
- **Other income / offsets** (`incomes` table): keep all wording generic. The
  user decides the descriptions; never add example descriptions or name a
  specific use of it anywhere (code, UI, tests, commits, docs).

## Layout

```
app/            the whole web app (deployed as the Lambda zip)
  main.py       FastAPI routes; Mangum handler for Lambda
  ledger.py     all money logic (pure functions over a sqlite3 connection)
  db.py         schema, migrations, settings
  store.py      SQLite-file-in-S3 store (or a local dir without BUCKET)
  auth.py       Cognito PKCE + HMAC session cookie (ported from aws_gallery)
  pdf.py        receipts, apartment statements, yearly summary (fpdf2, Hebrew)
  templates/    Jinja; _ui.html holds macros (icon, status chip, cell data)
  static/       app.css, app.js, fonts/ (Rubik: TTF for PDFs, WOFF2 for UI)
terraform/      modules: storage, auth (Cognito), app (Lambda), cdn (CloudFront+ACM)
layers/build.sh Lambda layer from requirements.txt, no Docker (manylinux arm64 wheels)
scripts/        bootstrap-keys.sh (SSM session key), import_xlsx.py (one-time import)
tests/          stdlib unittest: `make test`
```

## Architecture

```
vaad.asadodevculture.com -> CloudFront (ACM cert) -> Lambda function URL (OAC, AWS_IAM)
  /auth/*  Cognito managed login (passkeys + password), PKCE, HMAC cookie `vaad_user`
  else     session checked in middleware; DEV_USER bypass only locally (refused on Lambda)
S3 bucket (private, versioned, SSE-S3, Block Public Access on; name in terraform.tfvars)
  db/vaad.sqlite            the whole database
  receipts/<year>/<no>.pdf  rendered on first open, then kept; voiding writes <no>-void.pdf
  expenses/<year>/<id>-<n>.<ext>  vendor receipts (photo/PDF)
  summaries/<year>.pdf      yearly summary, overwritten each time (versions keep history)
```

- **Store**: each request downloads `db/vaad.sqlite` (cached in /tmp by ETag),
  and a write uploads it with `If-Match`; a race raises `Conflict` ("try
  again" page) instead of overwriting. Use `store.read()` / `store.write()`;
  never write in a read block.
- **Lifecycle**: S3 keeps the 30 newest previous versions of `db/` only.
- **Form posts go through `fetch`** with an `x-amz-content-sha256` header (app.js
  `bodyHash`), because CloudFront OAC signs requests to the function URL and
  Lambda rejects unsigned bodies. Multipart uploads hash the exact bytes; photos
  are shrunk in the browser (Lambda payload limit is 6 MB).
- A redirect back to the same URL triggers `location.reload()` so the scroll
  position is kept.
- `refresh()` in main.py creates missing dues charges and due recurring
  expenses; it is called by the pages that show balances.
- Static asset URLs carry a content hash (`asset_version`) so deploys never
  serve stale JS/CSS.

## Data model and rules

Money is integer agorot everywhere. Periods are `YYYY-MM`.

- `apartments` (12, ids 1-12), `charges` (dues per month; `opening` debt; `other`)
- `payments`: method `transfer|cheque|cash|bit|offset`; `account` follows the
  method (transfer/cheque -> bank, else -> treasurer, enforced by CHECK);
  status `held` (marked as received, no receipt yet) | `cleared` | `bounced` | `void`.
- `receipts`: numbers start at 1000, never reused; triggers allow only voiding,
  once, and forbid deletes. Receipts are **issued to the apartment**
  ("התקבל מאת: דירה N"), not to a person.
- `expenses` (+ `expense_files`), `recurring` (monthly rules; each month's
  expense is created when its day arrives, never ahead), `incomes`,
  `transfers` (bank <-> treasurer), `bank_statements` (typed in from paper),
  `settings` (key/value, defaults in `db.DEFAULT_SETTINGS`).
- Apartment balance = charges - cleared payments (positive = owes).
- Account balances count from `accounts_start` with `opening_bank` /
  `opening_treasurer`. The treasurer account (קופת גזבר) negative = the ועד
  owes the treasurer; offset dues and treasurer-kept income reduce it.
- Bulk deposit takes numbered held cheques for the current month or earlier.
  There is no cheque due date (removed on purpose).

### Migrations

`db.SCHEMA` is the version-1 baseline and must not change for existing tables.
A new database runs `SCHEMA` then `MIGRATIONS[1:]`; an existing one runs the
migrations after its `PRAGMA user_version`. **New tables** can be added to
`SCHEMA` (`CREATE TABLE IF NOT EXISTS`). **Changes to existing tables** go in a
new `MIGRATIONS` entry (ADD/DROP COLUMN, or a rebuild for CHECK changes). Live
and local are at version 5. When shipping a migration, run it on live under
watch (download -> `Store.write()` with no changes -> verify `user_version`).

## Live data vs local data

They are separate SQLite files: live in S3, local in `.devdata/vaad.sqlite`.
The user edits both. Before copying data in either direction, diff them row by
row (compare by content, not ids) and merge; never blindly overwrite. Back up
local to `backups/` (gitignored) first. One-off data scripts run with
`.venv/bin/python -I`, use `Store` with `BUCKET` and a **temp dir** (never
`.devdata`, or the live copy would replace the local one), and stay out of the
repo.

## Gotchas

- DNS for asadodevculture.com is at **Sered** (domain registered in Route 53
  Domains, no hosted zone); the user adds records there. Its SOA negative TTL
  is 24h: never look up a new hostname before its record exists. If cached as
  NXDOMAIN, purge 1.1.1.1 (`POST https://one.one.one.one/api/v1/purge?domain=<host>&type=A`)
  and have the user flush macOS DNS.
- Terraform state is in its own bucket (see `terraform/backend.hcl`, gitignored).
- `sqlite3.Row` never equals a tuple: build sets with `tuple(row)`.
- Hebrew PDFs need fpdf2 text shaping (`uharfbuzz`) for correct bidi; use
  words, not minus signs, for balances in PDFs. fpdf2 tables are laid out LTR,
  so `pdf._table` reverses the columns.
- Headless Chrome screenshots below ~500px wide are cut off; that is the
  browser's minimum window, not the layout.
- In zsh, don't name a loop variable `path` (it is tied to `PATH`).

## Commands

```
make dev        # localhost:8000, .devdata/, no login
make test       # unittest suite
make layers     # rebuild layers/deps.zip after changing requirements.txt
make apply      # deploy infra + app code (terraform apply)
make backup     # download the live database into backups/
make add-user EMAIL=...   # Cognito user; prints a temporary password
```

## Status and open ideas

Done: marking months, לטיפול processing (cheque numbers inline, deposit,
send receipts), receipts, statements, expenses with vendor files, recurring
expenses, other income/offsets, transfers, bank reconciliation, yearly
summary, debt reminders, settings, Excel import.

Not done, only if asked: bank CSV import (needs online bank access), resident
logins. Rejected: cheque photos, a monthly report, a "real" database (the user
chose SQLite in S3 over Aurora DSQL/RDS).
