SHELL := /bin/bash

# Personal settings live in config.env, which is gitignored.
ifneq (,$(wildcard config.env))
include config.env
export
endif

VENV := .venv/bin

.PHONY: help venv layers test dev keys init plan apply outputs cert-status add-user reset-password backup

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

venv: ## create the local python env
	python3 -m venv .venv && $(VENV)/pip install -q --upgrade pip && $(VENV)/pip install -q -r requirements.txt uvicorn boto3

layers: ## build the lambda dependency layer (no container runtime needed)
	./layers/build.sh

test: ## run the unit tests
	$(VENV)/python -m unittest discover -s tests -t .

dev: ## run the app at localhost:8000 against .devdata/, login skipped
	DEV_USER=dev@local DATA_DIR=.devdata $(VENV)/uvicorn main:app --app-dir app --reload --port 8000

keys: ## write the session HMAC key to SSM (once)
	./scripts/bootstrap-keys.sh

init: ## terraform init against the S3 backend
	cd terraform && terraform init -backend-config=backend.hcl

plan: ## show pending infrastructure changes
	cd terraform && terraform plan

apply: ## apply infrastructure and app code
	cd terraform && terraform apply

outputs:
	cd terraform && terraform output

cert-status: ## acm validation state
	@aws acm list-certificates --query "CertificateSummaryList[?DomainName=='$(VAAD_DOMAIN)'].CertificateArn" --output text \
	  | xargs -I{} aws acm describe-certificate --certificate-arn {} --query 'Certificate.Status' --output text

add-user: ## add the treasurer: make add-user EMAIL=you@example.com
	@test -n "$(EMAIL)" || { echo "usage: make add-user EMAIL=you@example.com"; exit 1; }
	@pool=$$(cd terraform && terraform output -raw user_pool_id); \
	 pw="Vaad-$$($(VENV)/python -c 'import secrets,string;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(14)))')"; \
	 aws cognito-idp admin-create-user --user-pool-id $$pool --username "$(EMAIL)" \
	   --user-attributes Name=email,Value="$(EMAIL)" Name=email_verified,Value=true \
	   --message-action SUPPRESS --query 'User.UserStatus' --output text >/dev/null && \
	 aws cognito-idp admin-set-user-password --user-pool-id $$pool --username "$(EMAIL)" --password "$$pw" && \
	 echo "" && echo "  user:     $(EMAIL)" && echo "  password: $$pw" && \
	 echo "  change it at first sign-in, then register a passkey."

reset-password: ## reset a password: make reset-password EMAIL=you@example.com
	@test -n "$(EMAIL)" || { echo "usage: make reset-password EMAIL=you@example.com"; exit 1; }
	@pool=$$(cd terraform && terraform output -raw user_pool_id); \
	 pw="Vaad-$$($(VENV)/python -c 'import secrets,string;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(14)))')"; \
	 aws cognito-idp admin-set-user-password --user-pool-id $$pool --username "$(EMAIL)" --password "$$pw" && \
	 echo "" && echo "  user:     $(EMAIL)" && echo "  password: $$pw" && \
	 echo "  must be changed at next sign-in. Any passkey already registered still works."

backup: ## download the current database to backups/
	@mkdir -p backups && bucket=$$(cd terraform && terraform output -raw bucket) && \
	 aws s3 cp "s3://$$bucket/db/vaad.sqlite" "backups/vaad-$$(date +%Y%m%d-%H%M%S).sqlite"
