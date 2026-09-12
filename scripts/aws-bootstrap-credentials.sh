#!/usr/bin/env bash
# Create the dedicated benchmark IAM user and the NON-EXPIRING key acspeed signs with, and wire it
# into a named profile. Run this once per AWS account, deliberately: it creates an IAM user with
# AdministratorAccess in the account your current credentials point at.
#
#     bash scripts/aws-bootstrap-credentials.sh --dry-run    # show what it would do
#     bash scripts/aws-bootstrap-credentials.sh              # do it
#     bash scripts/aws-bootstrap-credentials.sh --user my-bench --region eu-west-1
#
# Why a static key and not `aws login`: an SSO session expires after a few hours. If it lapses
# mid-run the deprovision agent has no credentials and leaves a LIVE, BILLING orphan. A static key
# never expires, so deploy and deprovision always authenticate.
#
# Why AdministratorAccess: the agent provisions arbitrary services (ECS, RDS, ELB, CloudFront, EC2)
# and must tear them all down. Scope it down if you know exactly which services your runs will use.
# Treat the key as admin on that account.
set -euo pipefail

USER_NAME="acspeed-batch"; REGION="us-east-1"; DRY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --user)    USER_NAME="${2:?--user needs a name}"; shift ;;
    --region)  REGION="${2:?--region needs a region}"; shift ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
  shift
done

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found. Run: bash setup.sh --adapter aws" >&2; exit 1; }

echo "== acspeed AWS credential bootstrap =="
echo "  IAM user : $USER_NAME"
echo "  profile  : $USER_NAME"
echo "  region   : $REGION"
echo

# 1. Who am I? The caller must already be an admin of the target account.
if ! CALLER="$(aws sts get-caller-identity --output json 2>/dev/null)"; then
  echo "No usable AWS credentials in this shell." >&2
  echo "Authenticate as an admin of the target account first (aws configure, SSO, or env vars)," >&2
  echo "then re-run. This script only creates the benchmark user; it cannot bootstrap itself." >&2
  exit 1
fi
ACCOUNT="$(printf '%s' "$CALLER" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Account"])')"
WHO="$(printf '%s' "$CALLER" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Arn"])')"
echo "  acting as: $WHO"
echo "  account  : $ACCOUNT"
echo

if [ -n "$DRY" ]; then
  echo "DRY RUN. Would:"
  echo "  1. create IAM user '$USER_NAME' (if absent), tagged purpose=acspeed-benchmark"
  echo "  2. attach arn:aws:iam::aws:policy/AdministratorAccess to it"
  echo "  3. create a non-expiring access key for it (if it has none)"
  echo "  4. write it into the '$USER_NAME' profile with region $REGION"
  echo "  5. verify with sts get-caller-identity --profile $USER_NAME"
  echo
  echo "Nothing was changed."
  exit 0
fi

# 2. The user ---------------------------------------------------------------------------------------
if aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  echo "[1/4] IAM user $USER_NAME already exists, leaving it alone"
else
  echo "[1/4] creating IAM user $USER_NAME"
  aws iam create-user --user-name "$USER_NAME" --tags Key=purpose,Value=acspeed-benchmark >/dev/null
fi

# 3. The policy -------------------------------------------------------------------------------------
POLICY="arn:aws:iam::aws:policy/AdministratorAccess"
if aws iam list-attached-user-policies --user-name "$USER_NAME" --output text 2>/dev/null | grep -q AdministratorAccess; then
  echo "[2/4] AdministratorAccess already attached"
else
  echo "[2/4] attaching AdministratorAccess"
  aws iam attach-user-policy --user-name "$USER_NAME" --policy-arn "$POLICY"
fi

# 4. The key ----------------------------------------------------------------------------------------
EXISTING="$(aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null || true)"
if [ -n "$EXISTING" ] && aws sts get-caller-identity --profile "$USER_NAME" >/dev/null 2>&1; then
  echo "[3/4] a key exists and profile '$USER_NAME' already authenticates, leaving it alone"
else
  if [ -n "$EXISTING" ]; then
    echo "[3/4] user has key(s) ($EXISTING) but profile '$USER_NAME' does not authenticate."
    echo "      AWS never re-shows a secret, so that key is unusable here. Creating a new one."
    echo "      Delete the stale one afterwards if you do not need it:"
    echo "        aws iam delete-access-key --user-name $USER_NAME --access-key-id <id>"
  else
    echo "[3/4] creating a non-expiring access key"
  fi
  CRED="$(aws iam create-access-key --user-name "$USER_NAME" --output json)"
  KID="$(printf '%s' "$CRED" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')"
  SEC="$(printf '%s' "$CRED" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')"
  aws configure set aws_access_key_id     "$KID" --profile "$USER_NAME"
  aws configure set aws_secret_access_key "$SEC" --profile "$USER_NAME"
  aws configure set region                "$REGION" --profile "$USER_NAME"
  unset CRED SEC
  echo "      wrote profile '$USER_NAME' (key $KID)"
fi

# 5. Verify -----------------------------------------------------------------------------------------
echo "[4/4] verifying (a brand new key can take a few seconds to propagate)"
for i in 1 2 3 4 5; do
  if OUT="$(aws sts get-caller-identity --profile "$USER_NAME" --output json 2>/dev/null)"; then
    printf '%s' "$OUT" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("      ok:",d["Arn"])'
    echo
    echo "Done. acspeed signs with this profile: config/aws.mcp.json already passes --profile $USER_NAME."
    echo "If you changed --user, update that file to match."
    exit 0
  fi
  sleep 3
done
echo "      still failing after ~15s. Check: aws sts get-caller-identity --profile $USER_NAME" >&2
exit 1
