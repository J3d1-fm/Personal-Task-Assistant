#!/usr/bin/env bash
set -euo pipefail

# Fill these in before running.
PROJECT_ID="your-gcp-project"
REGION="europe-west1"
SERVICE="personal-task-assistant"
REPOSITORY="personal-apps"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:latest"
API_KEY_SECRET="personal-task-assistant-api-key"
SESSION_SECRET_SECRET="personal-task-assistant-session-secret"
GOOGLE_CLIENT_ID_SECRET="personal-task-assistant-google-client-id"
GOOGLE_CLIENT_SECRET_SECRET="personal-task-assistant-google-client-secret"
ALLOWED_GOOGLE_EMAILS="you@example.com"
PUBLIC_BASE_URL="https://your-cloud-run-url"

gcloud config set project "${PROJECT_ID}"

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  sheets.googleapis.com

gcloud artifacts repositories create "${REPOSITORY}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Personal app containers" || true

gcloud builds submit --tag "${IMAGE}"

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --allow-unauthenticated \
  --set-secrets="TASK_TRACKER_API_KEY=${API_KEY_SECRET}:latest,SESSION_SECRET_KEY=${SESSION_SECRET_SECRET}:latest,GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_CLIENT_ID_SECRET}:latest,GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET_SECRET}:latest" \
  --set-env-vars="APP_NAME=Personal Task Assistant,TASK_STORE=firestore,ALLOWED_GOOGLE_EMAILS=${ALLOWED_GOOGLE_EMAILS},PUBLIC_BASE_URL=${PUBLIC_BASE_URL},SESSION_COOKIE_HTTPS=true,TASK_HISTORY_SHEET_ID=,TASK_HISTORY_SHEET_TAB=Task History"
