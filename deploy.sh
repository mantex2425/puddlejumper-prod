# 1. Build the new image from your local code
gcloud builds submit --tag us-central1-docker.pkg.dev/puddle-jumper-477316/cloud-run-source-deploy/puddlejumper-api:latest

# 2. Deploy that fresh image to Cloud Run
gcloud run deploy puddlejumper-api \
  --image us-central1-docker.pkg.dev/puddle-jumper-477316/cloud-run-source-deploy/puddlejumper-api:latest \
  --region us-central1 \
  --cpu=1 \
  --memory=1Gi \
  --timeout=600 \
  --cpu-boost \
  --command="gunicorn" \
  --args="--bind=0.0.0.0:8080,--workers=1,--threads=4,--timeout=120,--log-level=info,--access-logfile=-,--error-logfile=-,app:app" \
  --set-env-vars "DB_HOST=10.128.0.2,DB_NAME=puddlejumper,DB_USER=atjb,FIREBASE_PROJECT_ID=puddle-jumper-477316,DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1488253848433852561/LsBkk0pn755rwuR6CPbZWHoHMhBV_lNrIz2T7Nd7cDbc0EaHviEB_vElPhBYPMbgD841" \
  --set-secrets "DB_PASSWORD=DB_PASSWORD:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,TAVILY_API_KEY=TAVILY_API_KEY:latest,GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest,MAPBOX_API_KEY=MAPBOX_API_KEY:latest,APPLE_WEATHER_KEY=APPLE_WEATHER_PRIVATE_KEY:latest,DB_PASSWORD_READONLY=DB_PASSWORD_READONLY:latest" \
  --vpc-connector puddle-bridge \
  --vpc-egress private-ranges-only \
  --min-instances=0 \
  --max-instances=1 \
  --cpu-throttling

# 3. Force traffic to the new revision.
# Cloud Run pins traffic to specific named revisions once any manual route
# has been applied; subsequent deploys do NOT auto-route to LATEST until
# we explicitly request it. This restores the auto-promote behavior.
gcloud run services update-traffic puddlejumper-api \
  --region=us-central1 --to-latest
