# ועד בית

Payments, cheques and receipts for a 12-apartment building committee, for one treasurer.
Receipts are numbered PDFs sent to residents on WhatsApp. Runs on AWS for under a dollar a month.

See `PLAN.md` for the design and what comes next.

## How it works

```
vaad.asadodevculture.com
   └── CloudFront (ACM cert)
        └── vaad-app Lambda (function URL, OAC + AWS_IAM)
             ├── /auth/*  Cognito managed login, PKCE, passkeys
             └── the app  FastAPI + Jinja, session cookie checked on every request
S3 bucket (private, versioned)
   db/vaad.sqlite            the whole database, written back with If-Match
   receipts/<year>/<no>.pdf  rendered on first open, then kept
```

Each Lambda instance caches the SQLite file in `/tmp` by ETag. A save that races
another fails with a "try again" page instead of overwriting it, and every save is
an S3 version, so any bad write can be rolled back.

## First-time setup

DNS for `asadodevculture.com` lives at the registrar, so two records are added by hand.

    cp config.env.example config.env                    # profile + domain
    cp terraform/backend.hcl.example terraform/backend.hcl
    cp terraform/terraform.tfvars.example terraform/terraform.tfvars
    make venv && make layers
    make keys                                           # session key -> SSM
    make init
    cd terraform && terraform apply -target=module.cdn.aws_acm_certificate.vaad

Add the printed `acm_validation_record` CNAME at the registrar and wait for
`make cert-status` to say `ISSUED`, then:

    make apply

Point a `vaad` CNAME at the `distribution_domain` output, create your user, and sign in:

    make add-user EMAIL=you@example.com

Then, in the app, fill in the apartments and **Settings** (building name, treasurer,
the month dues start, the WhatsApp message).

## Day to day

    make dev      # localhost:8000 against .devdata/, login skipped
    make test
    make apply    # ships infrastructure and app code changes
    make backup   # copy the live database to backups/

## Rules that keep the books trustworthy

- **Receipt numbers start at 1000 and are never reused.** SQLite triggers block
  deleting a receipt or changing one other than voiding it, once.
- **Voiding** writes a new `-void.pdf` key; the PDF the resident already has stays as it was.
- **No `s3:DeleteObject`** in the Lambda role.
- `DEV_USER` turns login off for `make dev`; the app refuses to start on Lambda if it is set.
- Form posts go through `fetch` with an `x-amz-content-sha256` header, because CloudFront
  signs requests to the function URL and Lambda rejects unsigned bodies.
