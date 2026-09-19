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
  --set-env-vars "DB_HOST=10.128.0.3,DB_NAME=puddlejumper,DB_USER=atjb,FIREBASE_PROJECT_ID=puddle-jumper-477316,DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1488253848433852561/LsBkk0pn755rwuR6CPbZWHoHMhBV_lNrIz2T7Nd7cDbc0EaHviEB_vElPhBYPMbgD841" \
  --set-secrets "SMOKE_TOKEN=SMOKE_TOKEN:latest,DB_PASSWORD=DB_PASSWORD:latest,GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest,MAPBOX_API_KEY=MAPBOX_API_KEY:latest,DB_PASSWORD_READONLY=DB_PASSWORD_READONLY:latest" \
  --vpc-connector puddle-bridge \
  --vpc-egress private-ranges-only \
  --min-instances=0 \
  # 1 -> 5 on 2026-09-19, before inviting testers. A ceiling, not a reservation: with
  # min-instances=0 nothing runs (or bills) while nobody is driving, and Cloud Run only
  # starts a second container when requests actually overlap. At ~1.5s per scoring request
  # that is about 4 cents a day per thousand offers. One instance with four threads was
  # fine for one driver; offers arrive in bursts, and a driver who waits four seconds for
  # a verdict stops trusting it.
  --max-instances=5 \
  --cpu-throttling

# 3. Force traffic to the new revision.
# Cloud Run pins traffic to specific named revisions once any manual route
# has been applied; subsequent deploys do NOT auto-route to LATEST until
# we explicitly request it. This restores the auto-promote behavior.
gcloud run services update-traffic puddlejumper-api \
  --region=us-central1 --to-latest

# 4. Prove the decision path actually RUNS on the new revision.
#
# A deploy can succeed, the container boot, /api/v1/health answer 200, and every scoring
# call still fail -- that is exactly what happened on 2026-09-19, when a function-level
# import of a deleted module returned {"error": "No module named 'bead_on_wire'"} for
# every offer. Nothing noticed until a driver watched three offers go unscored. This
# scores one canned offer through parse_request + run_decision_engine and rolls back.
echo "==> smoke test: scoring a canned offer on the new revision"
SMOKE_TOKEN_VALUE=$(gcloud secrets versions access latest --secret=SMOKE_TOKEN 2>/dev/null)
if [ -z "$SMOKE_TOKEN_VALUE" ]; then
    echo "SMOKE FAIL: no SMOKE_TOKEN secret; cannot verify the deploy" >&2
    exit 1
fi
SMOKE_URL="https://puddlejumper-api-152974241923.us-central1.run.app/internal/smoke/decision"
SMOKE_BODY=$(curl -s --max-time 60 -H "X-Smoke-Token: $SMOKE_TOKEN_VALUE" "$SMOKE_URL")
case "$SMOKE_BODY" in
    *'"ok":true'*)
        echo "    OK: $SMOKE_BODY"
        ;;
    *)
        echo "SMOKE FAIL: the decision path did not return a verdict" >&2
        echo "    response: $SMOKE_BODY" >&2
        echo "    the revision is LIVE and scoring is broken -- roll back with:" >&2
        echo "    gcloud run services update-traffic puddlejumper-api --region=us-central1 --to-revisions=PREVIOUS=100" >&2
        exit 1
        ;;
esac
