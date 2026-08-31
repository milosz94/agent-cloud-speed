#!/usr/bin/env bash
# Reusable orphan sweep for the acspeed medium/hard runs: deletes every acspeed-* resource left on the
# three public clouds (redu is swept separately via its MCP by the operator). All acspeed resources are
# named with an "acs"/"umami-acs"/"acspeed"/"probe"/"sentinel"/"siteb" token, and nothing else on these
# test accounts uses that prefix, so a prefix sweep is safe. Idempotent; prints what it deletes.
set -u
AWS_P="--profile acspeed-batch"
echo "########## AWS (profile acspeed-batch, us-east-1) ##########"
R=us-east-1
# RDS
for db in $(aws $AWS_P --region $R rds describe-db-instances \
      --query "DBInstances[?starts_with(DBInstanceIdentifier,'umami-acs') || starts_with(DBInstanceIdentifier,'acs')].DBInstanceIdentifier" --output text 2>/dev/null); do
  aws $AWS_P --region $R rds delete-db-instance --db-instance-identifier "$db" --skip-final-snapshot --delete-automated-backups >/dev/null 2>&1 && echo "  RDS deleting $db"
done
# ECS services + clusters
for cl in $(aws $AWS_P --region $R ecs list-clusters --query "clusterArns" --output text 2>/dev/null | tr '\t' '\n' | grep -iE 'umami-acs|/acs'); do
  for svc in $(aws $AWS_P --region $R ecs list-services --cluster "$cl" --query "serviceArns" --output text 2>/dev/null | tr '\t' '\n'); do
    aws $AWS_P --region $R ecs update-service --cluster "$cl" --service "$svc" --desired-count 0 >/dev/null 2>&1
    aws $AWS_P --region $R ecs delete-service --cluster "$cl" --service "$svc" --force >/dev/null 2>&1 && echo "  ECS service deleted $svc"
  done
  aws $AWS_P --region $R ecs delete-cluster --cluster "$cl" >/dev/null 2>&1 && echo "  ECS cluster deleted $cl"
done
# ELBv2 + target groups
for arn in $(aws $AWS_P --region $R elbv2 describe-load-balancers --query "LoadBalancers[?starts_with(LoadBalancerName,'umami-acs') || starts_with(LoadBalancerName,'acs')].LoadBalancerArn" --output text 2>/dev/null); do
  aws $AWS_P --region $R elbv2 delete-load-balancer --load-balancer-arn "$arn" >/dev/null 2>&1 && echo "  ELB deleted $arn"
done
sleep 15
for tg in $(aws $AWS_P --region $R elbv2 describe-target-groups --query "TargetGroups[?starts_with(TargetGroupName,'umami-acs') || starts_with(TargetGroupName,'acs')].TargetGroupArn" --output text 2>/dev/null); do
  aws $AWS_P --region $R elbv2 delete-target-group --target-group-arn "$tg" >/dev/null 2>&1 && echo "  TG deleted $tg"
done
# App Runner
for arn in $(aws $AWS_P --region $R apprunner list-services --query "ServiceSummaryList[?starts_with(ServiceName,'acs') || starts_with(ServiceName,'umami') || contains(ServiceName,'siteb')].ServiceArn" --output text 2>/dev/null); do
  aws $AWS_P --region $R apprunner delete-service --service-arn "$arn" >/dev/null 2>&1 && echo "  AppRunner deleted $arn"
done
# S3 buckets
for b in $(aws $AWS_P s3api list-buckets --query "Buckets[?starts_with(Name,'acs') || contains(Name,'siteb') || contains(Name,'umami-acs') || contains(Name,'acspeed')].Name" --output text 2>/dev/null); do
  aws $AWS_P s3 rm "s3://$b" --recursive >/dev/null 2>&1; aws $AWS_P s3api delete-bucket --bucket "$b" >/dev/null 2>&1 && echo "  S3 deleted $b"
done

echo "########## GCP ##########"
for svc in $(gcloud run services list --platform managed --format="value(metadata.name)" 2>/dev/null | grep -iE '^acs|umami|probe|sentinel|siteb'); do
  reg=$(gcloud run services list --platform managed --filter="metadata.name=$svc" --format="value(region)" 2>/dev/null | head -1)
  gcloud run services delete "$svc" --region "$reg" --platform managed --quiet >/dev/null 2>&1 && echo "  CloudRun deleted $svc ($reg)"
done
for db in $(gcloud sql instances list --format="value(name)" 2>/dev/null | grep -iE '^acs|umami|probe|sentinel'); do
  gcloud sql instances delete "$db" --quiet >/dev/null 2>&1 && echo "  CloudSQL deleted $db"
done
for b in $(gsutil ls 2>/dev/null | grep -iE 'acs|siteb|umami|probe|sentinel'); do
  gsutil -m rm -r "$b" >/dev/null 2>&1 && echo "  GCS deleted $b"
done

echo "########## AZURE ##########"
for rg in $(az group list --query "[?starts_with(name,'rg-acs')].name" -o tsv 2>/dev/null); do
  az group delete -n "$rg" --yes --no-wait >/dev/null 2>&1 && echo "  RG deleting $rg"
done

echo "########## sweep done ($(date +%H:%M)); redu swept via MCP by the operator ##########"
