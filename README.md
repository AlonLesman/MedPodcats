# 669 Med-Debrief-Pod

A serverless Telegram bot built on Google Cloud Functions (Gen 2) that receives medical debrief **PDF files**, processes them using **Gemini 2.5 Flash**, and generates a natural, two-person podcast-style audio debrief using **Gemini Multi-Speaker TTS**.

## Architecture

```
PDF → Telegram Bot → Cloud Function Webhook
         ↓
   Gemini 2.5 Flash (PDF analysis + English script generation)
         ↓
   Gemini 2.5 Flash Preview TTS (2-speaker WAV audio synthesis)
         ↓
   Google Cloud Storage (backup) + Telegram sendDocument (delivery)
```

## Prerequisites

1. **Google Cloud SDK (`gcloud`)** installed and authenticated.
2. **Telegram Bot Token** obtained from [@BotFather](https://t.me/BotFather) on Telegram.
3. **Gemini API Key** from [Google AI Studio](https://aistudio.google.com/).

## Setup

### 1. Configure Secrets

Copy the template and fill in your secrets:
```bash
cp .env.yaml .env.secret.yaml
```

Edit `.env.secret.yaml`:
```yaml
TELEGRAM_BOT_TOKEN: "your-bot-token-from-botfather"
GEMINI_API_KEY: "your-gemini-api-key"
GCS_BUCKET_NAME: "your-gcs-bucket-name"
ALLOWED_CHAT_ID: "@user1, @user2, 12345678"  # List of authorized usernames/IDs
```

### 2. Create GCS Bucket

```bash
gcloud storage buckets create gs://YOUR_UNIQUE_BUCKET_NAME --location=europe-west1

# Grant the Cloud Function's service account write access:
gcloud storage buckets add-iam-policy-binding gs://YOUR_UNIQUE_BUCKET_NAME \
  --member="serviceAccount:$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### 3. Enable Required APIs

```bash
gcloud services enable cloudfunctions.googleapis.com cloudbuild.googleapis.com \
  storage.googleapis.com run.googleapis.com --project=YOUR_PROJECT_ID
```

### 4. Deploy the Function

```bash
gcloud functions deploy telegram-webhook-669 \
  --gen2 \
  --runtime=python311 \
  --region=europe-west1 \
  --source=./src \
  --entry-point=telegram_webhook \
  --trigger-http \
  --allow-unauthenticated \
  --env-vars-file=.env.secret.yaml \
  --timeout=540 \
  --memory=1Gi \
  --cpu=1 \
  --max-instances=3 \
  --concurrency=80
```

### 5. Register the Telegram Webhook

```bash
FUNCTION_URL=$(gcloud functions describe telegram-webhook-669 \
  --region=europe-west1 --gen2 --format='value(serviceConfig.uri)')

curl -F "url=${FUNCTION_URL}" \
  https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook
```

You should see: `{"ok":true,"result":true,"description":"Webhook was set"}`

## Usage

1. Open a chat with your Telegram Bot.
2. Send a **PDF file** containing the medical debrief.
3. The bot immediately acknowledges ("🔄 Processing...").
4. Within ~30 seconds, it replies with a **`.wav` audio file** — tap to download and play.

## Technical Notes

- **Audio format**: Gemini TTS returns raw 24kHz 16-bit mono PCM. The function wraps it in a proper RIFF/WAV header before delivery.
- **No duplicate podcasts**: The webhook returns `200 OK` to Telegram immediately (via a background thread), preventing Telegram's retry mechanism from triggering multiple generations.
- **GCS backup**: Audio files are backed up to GCS. If the upload fails, the podcast is still delivered to the user.
