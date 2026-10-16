#!/usr/bin/env bash
# Writes the session-cookie HMAC key straight to SSM, so it never touches
# Terraform state or the repo. Created only if absent; safe to re-run.
set -euo pipefail

HMAC_PARAM="${HMAC_PARAM:-/vaad/session/hmac-key}"

if aws ssm get-parameter --name "$HMAC_PARAM" >/dev/null 2>&1; then
  echo "session key -> exists, left alone (rotating it signs you out)"
else
  aws ssm put-parameter \
    --name "$HMAC_PARAM" \
    --type SecureString \
    --description "HMAC key for the vaad session cookie" \
    --value "$(openssl rand -hex 32)" >/dev/null
  echo "session key -> SSM $HMAC_PARAM"
fi
