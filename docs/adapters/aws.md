# AWS adapter

## You must do (not automatable)

1. **An AWS account you are an administrator of.** The agent provisions ECS/Fargate, RDS, load
   balancers, EC2 and CloudFront, and must be able to delete all of it.
2. **Authenticate as that admin in your shell**, any way you like (`aws configure`, SSO, env vars).
   This is only so the bootstrap below can create the benchmark user; the runs do not use it.

## The repo does

```bash
bash scripts/aws-bootstrap-credentials.sh --dry-run   # show the plan
bash scripts/aws-bootstrap-credentials.sh             # create it
```

Creates a dedicated `acspeed-batch` IAM user, attaches AdministratorAccess, mints a **non-expiring**
access key, writes the `acspeed-batch` profile, and verifies it. Idempotent; `--user` and `--region`
override. `config/aws.mcp.json` already passes `--profile acspeed-batch`, so nothing else to wire.

**Why a static key and not `aws login`:** an SSO session expires after a few hours. If it lapses
mid-run, the deprovision agent has no credentials and leaves a live, billing orphan. A static key
never expires, so deploy and deprovision always authenticate. Before every AWS run the harness does
an `sts get-caller-identity` preflight with that same profile and refuses to deploy if it fails.

**Why AdministratorAccess:** the agent provisions arbitrary services and must tear them all down.
Scope it down if you know exactly which services your runs will touch. Treat the key as admin.
