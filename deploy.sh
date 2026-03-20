# 1. Build the new image from your local code
gcloud builds submit --tag us-central1-docker.pkg.dev/puddle-jumper-477316/cloud-run-source-deploy/puddlejumper-api:latest

# 2. Deploy that fresh image to Cloud Run
gcloud run deploy puddlejumper-api \
  --image us-central1-docker.pkg.dev/puddle-jumper-477316/cloud-run-source-deploy/puddlejumper-api:latest \
  --region us-central1 \
  --cpu=2 \
  --memory=2Gi \
  --timeout=600 \
  --cpu-boost \
  --command="gunicorn" \
  --args="--bind=0.0.0.0:8080,--workers=1,--threads=4,--timeout=120,app:app" \
  --set-env-vars "DB_HOST=10.128.0.2,DB_NAME=puddlejumper,DB_USER=atjb,FIREBASE_PROJECT_ID=puddle-jumper-477316" \
  --set-secrets "DB_PASSWORD=DB_PASSWORD:3,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,TAVILY_API_KEY=TAVILY_API_KEY:latest,GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest,MAPBOX_API_KEY=MAPBOX_API_KEY:latest,APPLE_WEATHER_KEY=APPLE_WEATHER_PRIVATE_KEY:latest,DB_PASSWORD_READONLY=DB_PASSWORD_READONLY:latest" \
  --vpc-connector puddle-bridge \
  --vpc-egress private-ranges-only \
  --min-instances=1 \
  --no-cpu-throttling \
