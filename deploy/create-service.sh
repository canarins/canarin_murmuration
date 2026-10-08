#!/usr/bin/env bash
# One-time bootstrap of the ECS service (task defs/services are CLI-managed on
# Canarin, not CloudFormation — see canarin_infra/cfn/08-ecs-services.yml header).
# Prereqs: ECR repo canarin-murmuration, log group /ecs/canarin-murmuration,
# security group (deploy/cfn-snippets.yml), secret canarin/db-murmuration-app-prod,
# MySQL user murmuration_app (deploy/mysql-user.sql), S3 bucket for artifacts,
# and a first image pushed with tag `bootstrap`.
set -euo pipefail
REGION=eu-west-3; CLUSTER=canarin-cluster; SVC=canarin-murmuration
SG=${MURM_SG:?security group id of MurmurationSG}
SUBNETS=subnet-0b7cee1348b4e2a2b,subnet-097e01680ccb0018d     # private app subnets

aws ecs register-task-definition --region "$REGION" --cli-input-json file://deploy/task-definition.json >/dev/null
aws ecs create-service --region "$REGION" --cluster "$CLUSTER" --service-name "$SVC" \
  --task-definition "$SVC" --desired-count 1 --launch-type FARGATE \
  --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}"
# minimumHealthyPercent=0 : one consumer at a time — two concurrent cycles would
# both pull from the stream group and write the same keys (idempotent, but wasteful).
echo "service $SVC created; CI (deploy.yml) takes over from here."
