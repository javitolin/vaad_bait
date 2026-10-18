# ועד בית — System Plan

> The original design, kept for context. Parts changed during the build; `CLAUDE.md` describes the current state.

## Goal
Replace the manual sheet and handwritten receipts with a small web app that only the treasurer uses.
Residents get their receipt PDF on WhatsApp (or printed). A yearly summary is produced on demand and shared manually.

## Decisions
| Topic | Decision |
|---|---|
| Approach | Own small app |
| Users | Treasurer only. No resident logins. |
| Receipts | PDF stored in S3. The file itself is sent on WhatsApp, or printed. |
| Bank | ועד account has no online access, so no statement import. Payments are entered by hand. |
| Payment methods | cheque, transfer, cash, Bit, and **קיזוז (offset)**. Offset = your own dues are deducted from what the ועד owes you, like the "החזר חוב" rows in the sheet. |
| Treasurer pays cash out of pocket | Unchanged. Tracked as the treasurer account (קופת גזבר); see the model below. |
| Stack | Python 3.13 + FastAPI (Mangum on Lambda, uvicorn locally) + Jinja + SQLite + fpdf2 (PDF, Hebrew via text shaping) |
| Hosting | Your AWS account, `us-east-1` (cheapest; same as aws_gallery) |
| Auth | Same as aws_gallery: Cognito managed login with passkey or password |
| IaC | Terraform with one module per concern and an S3 backend, like aws_gallery |
| UI language | Hebrew, `dir="rtl"`, mobile-first |

## Hosting (AWS)
```
vaad.asadodevculture.com
   └── CloudFront (ACM cert; DNS records added at the registrar, like gallery)
        └── *  → vaad-app Lambda (function URL, OAC + AWS_IAM)
                   ├── /auth/login|callback|logout → Cognito managed login (PKCE)
                   └── everything else → requires a valid session cookie
S3 vaad bucket (private, versioned)
   db/vaad.sqlite            database file
   receipts/<year>/<no>.pdf  receipts issued to residents
   expenses/<year>/<id>-<n>  vendor receipts (photo/PDF)
   summaries/<year>.pdf      yearly summary
```
- **One Lambda** (arm64, zip + layer built without Docker, like gallery) serves both the app and the `/auth/*` routes.
  The function URL can only be reached through CloudFront (OAC).
- **Database**: the SQLite file in S3. The app downloads it (cached in `/tmp` by ETag) and writes it back with `If-Match: <etag>`,
  so a write that races another one fails instead of overwriting it. Versioning means every save is a backup.
- **Uploads** (vendor receipts) go through the app: the browser shrinks photos first, the app stores them in S3 (max 5 MB, image or PDF).
  This avoids the Lambda payload limit and the body-hash requirement.
- **Form POSTs through CloudFront OAC need `x-amz-content-sha256`.** Port gallery's `bodyHash` into a small `submit.js` that sends forms with `fetch`.
- **IAM**: the Lambda role can access only this bucket and the SSM session key. No access keys anywhere; local dev uses your AWS CLI profile.
- **Cost guard**: AWS Budget alert at $5/month. Lambda reserved concurrency of 5.

### Auth (ported from aws_gallery)
- A Cognito user pool, separate from gallery's:
  - **Essentials tier**, so passkeys and managed login v2 work.
  - Email username, admin-create-only, password policy 12+.
  - `relying_party_id = vaad.asadodevculture.com`.
- Public app client + PKCE: code flow, no client secret.
- `/auth/callback` exchanges the code, verifies the `id_token` against the JWKS, and sets an HMAC-signed `vaad_user` cookie (`email|expiry|sig`).
  The HMAC key is in SSM as a SecureString, written out of band by `bootstrap-keys.sh`.
- No CloudFront signed cookies: nothing is served straight from S3. PDFs are opened through short-lived presigned URLs after the session check.
- `make add-user EMAIL=…` / `make reset-password EMAIL=…` copied from gallery.

### Monthly cost (about 100 requests and 15 receipts a month)
| Item | Cost |
|---|---|
| Lambda requests + compute | $0 (always-free tier: 1M requests / 400k GB-s) |
| CloudFront | $0 (always-free tier) |
| Cognito Essentials (1 user) | $0 (free MAU tier) |
| S3 (a few MB + DB versions + uploads) | ~$0.01 |
| SSM SecureString (standard), ACM | $0 |
| **Total** | **under $1/month** |

## Data model (SQLite, money stored as integer agorot)
```
apartments       id (apt no) | owner | occupant | phone | monthly_fee | default_method
charges          apt_id | period 'YYYY-MM' | kind: dues|other|opening | amount | note
                 UNIQUE(apt_id, period, kind) where kind='dues'
payments         apt_id | date | amount | method: transfer|cheque|cash|bit|offset | period
                 account: bank|treasurer | cheque_no | cheque_due | status: held|cleared|bounced | note
receipts         no (sequential PK, starts at 1000, never reused) | payment_id UNIQUE | s3_key
                 issued_at | voided_at | void_reason
expenses         date | vendor | category | amount | account: bank|treasurer | note
expense_files    expense_id | s3_key | content_type      -- vendor receipts
transfers        date | from_account | to_account | amount | cheque_no | note
bank_statements  date | balance     -- typed in from the paper statement, monthly
settings         key | value
```

There are two accounts:
- **bank**: the ועד bank account. Transfers and cheques come in here; expenses paid by ועד cheque or transfer go out.
- **treasurer** (קופת גזבר): ועד money in your hands.
  - In: cash and Bit from residents, and your own dues paid by `offset`.
  - Out: anything you pay from your pocket (gardener cash, bulbs, keys…).
  - Negative balance = **the ועד owes you**. A reimbursement cheque from the ועד is a `transfer` from bank to treasurer.

Rules:
- **Apartment balance** = charges − cleared payments. Positive = the apartment owes money.
- **Account balance** = opening + payments in − expenses ± transfers.
  - Bank is compared with the last `bank_statements` row, and the difference is shown.
  - Treasurer replaces the "חובות של ועד הבית" table.
- **Receipts can't be changed once issued.** Voiding stamps the PDF "מבוטלת" (S3 versioning keeps the original) and the number stays used. Never delete.
- **Order when issuing a receipt:** save the DB row first, then render the PDF and upload it. If the PDF is missing when opened, it is re-rendered.
- **Cheques** are entered once as a series (numbers + due dates, status `held`). When a cheque is deposited it becomes `cleared` and its receipt is issued, as you do today. A bounced cheque gets status `bounced`, and its receipt is voided.

## Screens
1. **Monthly grid**: apartments × months, like the current sheet. Each cell shows paid, held cheque, or owes.
   Tap a cell to record a payment, prefilled from the apartment's fee and usual method.
2. **Record payment**: save → receipt PDF → buttons:
   - **Send on WhatsApp**: on the phone, the share sheet (Web Share API) sends the **PDF file** with the template text as its caption. You pick WhatsApp → the resident.
     On desktop, the PDF downloads and `wa.me/972…?text=<template>` opens the resident's chat, so you attach the file there.
   - **Print**: opens the PDF (A5).
   - The PDF shows receipt no., date, apartment, payer, amount, method/cheque no., period, ועד name, treasurer.
3. **Marking + לטיפול**: tap a tenant's month on the grid to mark what came in (cheque/transfer/cash/Bit/offset), no details.
   On the 15th, the לטיפול page lists everything marked: add each cheque's number inline, then deposit / issue the receipt,
   and send the day's receipts on WhatsApp from the same page.
4. **Expenses**: add/list, with a "paid from my pocket" toggle (account = treasurer). Attach vendor receipts (camera photo or PDF), view them later.
5. **Balances**: per apartment, plus the bank and treasurer accounts.
6. **Yearly summary**: pick a year → generate a PDF on demand and share it the same way as receipts (WhatsApp share sheet / download).
   - Income per apartment per month.
   - Expenses by category.
   - Opening and closing balances of bank and treasurer.
   - Who owes what at year end.
7. **Settings**:
   - Building name and address, treasurer name.
   - Default monthly fee.
   - **WhatsApp receipt message**. Placeholders: `{name} {apt} {amount} {period} {receipt_no}`.
   - **WhatsApp reminder message**. Placeholders: `{name} {apt} {balance}`.
   - **WhatsApp yearly summary message**. Placeholder: `{year}`.
   - Expense categories (seeded: גנן, מנקה, ביטוח, מעלית שירות, מעלית שנתי, אגודה לתרבות הדיור, שונות).
   - Apartments: owner, occupant, phone, fee, default method.

## Migration
`scripts/import_xlsx.py` loads the old sheet: apartment names and usual methods, 2026 months already paid
(as payments marked "קבלה ידנית", no new receipt numbers), and Nov–Dec cheques as held cheques with unknown numbers.
Dues are charged from 2026-01. Still to enter by hand in Phase 2:
- Bank balance from the latest statement.
- Treasurer account: **−₪2,176.5** (9,610 owed − 7,433.5 repaid, according to the sheet).

Note: the sheet's income row (`=250*12`) counts all 12 apartments as paid every month, so 2026 surplus is overstated.
Apt 6 paid by offset, not cash, so the overstatement is ₪2,500 for Jan–Oct. Fix this before sending a 2026 annual summary.

## Phases
- **Phase 1 (done)**: AWS setup, auth, settings, apartments, grid with one-tap marks, לטיפול, receipts (PDF to S3, WhatsApp + print), Excel import.
- **Phase 2 (done)**: expenses with vendor receipts, transfers, bank + treasurer balances with bank reconciliation,
  per-apartment payment statement PDF, yearly summary PDF, payer name kept per payment (tenant changes are safe).
- **Later, only if needed**: bank CSV import (needs online access), resident logins.

## Safety
- S3: Block Public Access on, SSE-S3, versioning on, `prevent_destroy`. No `s3:DeleteObject` in the Lambda role — receipts, vendor files and DB versions are never deleted.
- Every page is behind Cognito; there are no public links. Presigned S3 URLs expire in 5 minutes.
- Tests (`unittest`, like gallery):
  - apartment and account balances (including the treasurer offset)
  - receipt numbering from 1000 + void
  - cheque status changes
  - a stale-ETag write is rejected
  - session cookie HMAC verification
