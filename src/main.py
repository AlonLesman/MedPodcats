import os
import uuid
import threading
import json
import requests
import functions_framework
from google import genai
from google.genai import types
from google.cloud import storage, texttospeech
from flask import Request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")

# Fixed Strings
PODCAST_INTRO = "צוותי רפואה יקרים, פודקאסט זה נוצר על בסיס הלקחים שנלמדו והופקו בשבוע האחרון. לקראת הכניסה לכוננות, הקשיבו היטב ללקחים המבצעיים ותיישמו. "
PODCAST_OUTRO = " עד כאן הלקחים מהקופה האחרונה. אם משהו לא מובן צרו עם פלגת רפואה קשר. שתהיה לכם כוננות מוצלחת!"
GUIDELINES_QUESTION = (
    "האם יש הנחיות נוספות לפודקאסט? משהו שחשוב לך להדגיש?\n"
    "אם כן, שלח עכשיו. אם לא, רשום 'no' (או 'לא')."
)

# Initialize global clients
genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
tts_client = texttospeech.TextToSpeechClient()

try:
    storage_client = storage.Client()
except Exception:
    storage_client = None

# A simple in-memory session cache for non-duplicate executions.
PROCESSED_UPDATES = set()

# State Management (GCS)
def set_user_state(chat_id: int, data: dict):
    """Save the pending file_id and user session to GCS."""
    if not storage_client or not GCS_BUCKET_NAME:
        return
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"state/{chat_id}.json")
        blob.upload_from_string(json.dumps(data), content_type="application/json")
    except Exception as e:
        print(f"Failed to set state for {chat_id}: {e}")

def get_user_state(chat_id: int) -> dict:
    """Retrieve the pending session for the user."""
    if not storage_client or not GCS_BUCKET_NAME:
        return None
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"state/{chat_id}.json")
        if blob.exists():
            return json.loads(blob.download_as_string())
    except Exception as e:
        print(f"Failed to get state for {chat_id}: {e}")
    return None

def clear_user_state(chat_id: int):
    """Clear the session after processing."""
    if not storage_client or not GCS_BUCKET_NAME:
        return
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"state/{chat_id}.json")
        if blob.exists():
            blob.delete()
    except Exception as e:
        print(f"Failed to clear state for {chat_id}: {e}")

def download_telegram_file(file_id: str) -> bytes:
    """Download the PDF from Telegram."""
    get_file_resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=30
    )
    get_file_resp.raise_for_status()
    file_path = get_file_resp.json()["result"]["file_path"]
    download_resp = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
        timeout=60
    )
    download_resp.raise_for_status()
    return download_resp.content

def upload_to_gcs(audio_bytes: bytes, filename: str) -> str:
    """Upload to GCS as a backup (MP3)."""
    if not storage_client or not GCS_BUCKET_NAME:
        return None
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
        return f"gs://{GCS_BUCKET_NAME}/{filename}"
    except Exception as e:
        print(f"GCS backup failed: {e}")
        return None

def send_telegram_audio(chat_id: int, audio_bytes: bytes, filename: str, caption: str):
    """Send audio back using sendAudio (native music player)."""
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio",
        data={"chat_id": chat_id, "caption": caption, "performer": "669 Paramedics (HE)", "title": "Tactical Summary"},
        files={"audio": (filename, audio_bytes, "audio/mpeg")},
        timeout=120
    )
    resp.raise_for_status()

def send_telegram_message(chat_id: int, text: str):
    """Notify the user."""
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10
    )

def synthesize_summary(text: str):
    """Converts the tactical summary into an MP3 byte stream, chunking to avoid 5000-byte limits."""
    # Hebrew characters use 2 bytes, so 1500 chars is ~3000 bytes (safe within 5000)
    chunk_size = 1500
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    
    combined_audio = b""
    voice = texttospeech.VoiceSelectionParams(
        language_code="he-IL",
        name="he-IL-Wavenet-D"
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        pitch=0.0,
        speaking_rate=1.05
    )
    
    for chunk in chunks:
        if not chunk.strip():
            continue
        input_text = texttospeech.SynthesisInput(text=chunk)
        response = tts_client.synthesize_speech(
            request={"input": input_text, "voice": voice, "audio_config": audio_config}
        )
        combined_audio += response.audio_content
        
    return combined_audio

def process_debrief(chat_id: int, file_id: str, guidelines: str, update_id: int):
    """PDF analysis followed by Hebrew TTS synthesis of a single-speaker summary."""
    try:
        # Step 1: Download PDF
        pdf_bytes = download_telegram_file(file_id)
        
        # Step 2: Use Gemini to analyze the medical PDF
        pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        
        # Incorporate guidelines if provided
        guidelines_instruction = ""
        if guidelines and guidelines.lower() not in ["no", "לא"]:
            guidelines_instruction = f"\nAdditional Guidelines from User: {guidelines}"

        full_prompt = (
            "You are a Commanding Officer in 669. "
            "Analyze this medical debrief PDF and provide a comprehensive tactical summary in Hebrew. "
            "The tone should be highly professional, direct, and tactical. "
            "IMPORTANT: Avoid using slang or informal language. The entire summary MUST be written in HEBREW. "
            "Provide only the monologue text, do not include any prefixes or speaker names. "
            "Keep the final output under 800 words."
            f"{guidelines_instruction}"
        )

        script_res = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[pdf_part, full_prompt]
        )
        
        hebrew_summary = script_res.text.replace("**", "").replace("*", "")
        if not hebrew_summary:
            raise Exception("Failed to generate Hebrew summary from Gemini.")
            
        # Prepend Intro and Append Outro
        full_audio_text = f"{PODCAST_INTRO} {hebrew_summary} {PODCAST_OUTRO}"
            
        # Step 3: Synthesis
        final_mp3 = synthesize_summary(full_audio_text)
        mp3_filename = f"669_he_summary_{uuid.uuid4().hex[:8]}.mp3"
        
        # Step 4: Backup & Deliver
        gcs_uri = upload_to_gcs(final_mp3, mp3_filename)
        caption = "🎧 סיכום טקטי 669 מוכן (פודקאסט בעברית)"
        if gcs_uri:
            caption += f"\n📦 גיבוי: {gcs_uri}"
            
        send_telegram_audio(chat_id, final_mp3, mp3_filename, caption)

    except Exception as e:
        print(f"Error on update {update_id}: {e}")
        send_telegram_message(chat_id, f"שגיאה בעת יצירת התדריך:\n{str(e)}")

@functions_framework.http
def telegram_webhook(request: Request):
    """Entry point for Telegram Webhooks."""
    if request.method != "POST":
        return "Only POST allowed", 405

    try:
        update = request.get_json()
    except Exception:
        return "Invalid JSON", 400

    if not update or "message" not in update:
        return "OK", 200

    update_id = update.get("update_id", 0)
    if update_id in PROCESSED_UPDATES:
        return "OK", 200
    PROCESSED_UPDATES.add(update_id)
    if len(PROCESSED_UPDATES) > 1000:
        PROCESSED_UPDATES.pop()

    message = update["message"]
    chat_id = message["chat"]["id"]

    # Simple security check (list-based)
    if ALLOWED_CHAT_ID:
        allowed_ids = [id.strip().lstrip("@").lower() for id in str(ALLOWED_CHAT_ID).split(",")]
        username = message["chat"].get("username", "").lower().lstrip("@")
        if str(chat_id) not in allowed_ids and username not in allowed_ids:
            unauthorized_msg = (
                f"מצטערים, אין לך הרשאה להשתמש בבוט זה (מזהה: {chat_id}).\n"
                "פנה למנהל לצורך קבלת גישה: https://t.me/alonlesman"
            )
            send_telegram_message(chat_id, unauthorized_msg)
            return "OK", 200

    # User sent a PDF
    if "document" in message and message["document"].get("mime_type") == "application/pdf":
        current_state = get_user_state(chat_id)
        msg_prefix = ""
        if current_state:
            msg_prefix = "(שימו לב: הקובץ הקודם ששלחתם הוחלף בקובץ החדש)\n\n"
        
        set_user_state(chat_id, {"file_id": message["document"]["file_id"]})
        send_telegram_message(chat_id, f"{msg_prefix}{GUIDELINES_QUESTION}")
        return "OK", 200

    # User sent text
    if "text" in message:
        state = get_user_state(chat_id)
        if state and "file_id" in state:
            guidelines = message["text"]
            file_id = state["file_id"]
            
            # Clear state so we don't double-process
            clear_user_state(chat_id)
            
            # Start background processing
            notify_msg = "🔄 מעבד את התדריך הרפואי... יוצר סיכום טקטי בעברית (~30 שניות)"
            send_telegram_message(chat_id, notify_msg)
            
            threading.Thread(
                target=process_debrief, 
                args=(chat_id, file_id, guidelines, update_id), 
                daemon=True
            ).start()
            return "OK", 200

    # Catch-all
    if "document" not in message and "text" not in message:
        send_telegram_message(chat_id, "אנא שלח תדריך רפואי בפורמט PDF.")
    elif "text" in message and not get_user_state(chat_id):
        send_telegram_message(chat_id, "אנא שלח קובץ PDF תחילה כדי שאוכל ליצור פודקאסט.")

    return "OK", 200
