"""
inbound_handler.py
Pinnacle Block B — Inbound WhatsApp Message Handler

Receives inbound messages from WasenderAPI webhook.
Routes to scorer, then to Nurturer / Objection Handler / Escalator.

Handles:
  - Text messages → sentiment classify → route
  - Voice notes   → Sarvam transcription → treat as text → route
  - Images/docs   → HIGH_INTENT flag → immediate Escalator
  - Videos        → acknowledge + flag for human review

Run as Flask endpoint alongside the main poller.
WasenderAPI webhook URL: POST /webhook/inbound
"""

import os
import json
import logging
import requests
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify
from scorer import process_inbound_reply, update_score
from ab_router import record_outcome

SARVAM_KEY         = os.environ.get("SARVAM_KEY", "")
WASENDER_API_KEY   = os.environ.get("WASENDER_API_KEY", "")
WASENDER_SESSION   = os.environ.get("WASENDER_SESSION_ID", "")
WASENDER_BASE_URL  = "https://api.wasenderapi.com/api"
SALES_LINE         = os.environ.get("SALES_LINE_PHONE", "919840097140")

_DATA_DIR = os.environ.get("DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
os.makedirs(_DATA_DIR, exist_ok=True)

QUEUE_PATH         = os.environ.get("QUEUE_PATH",
    os.path.join(_DATA_DIR, "pinnacle_lead_queue.jsonl"))
INBOUND_LOG        = os.environ.get("INBOUND_LOG",
    os.path.join(_DATA_DIR, "inbound_log.jsonl"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Lead registry (phone → lead_id) ──────────────────────────────────────────

LEAD_REGISTRY_FILE = os.environ.get("LEAD_REGISTRY_FILE",
    os.path.join(_DATA_DIR, "lead_registry.json"))

def get_lead_id_from_phone(phone: str) -> str:
    """Look up lead_id from phone number."""
    try:
        registry = json.loads(Path(LEAD_REGISTRY_FILE).read_text())
        # Normalise phone
        digits = "".join(filter(str.isdigit, str(phone)))
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        return registry.get(digits, registry.get(phone, "UNKNOWN"))
    except Exception:
        return "UNKNOWN"


def register_lead(lead_id: str, phone: str):
    """Register a lead_id → phone mapping."""
    try:
        path = Path(LEAD_REGISTRY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        registry = json.loads(path.read_text()) if path.exists() else {}
        digits = "".join(filter(str.isdigit, str(phone)))
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        registry[digits] = lead_id
        path.write_text(json.dumps(registry, indent=2))
    except Exception as e:
        log.error(f"Failed to register lead: {e}")

# ── Sarvam voice transcription ────────────────────────────────────────────────

def transcribe_voice_note(audio_url: str) -> str:
    """
    Download voice note from WasenderAPI and transcribe via Sarvam Saaras v3.
    Returns transcribed text or empty string on failure.
    """
    if not SARVAM_KEY:
        log.warning("SARVAM_KEY not set — cannot transcribe voice note")
        return ""

    # Download audio
    try:
        resp = requests.get(audio_url, timeout=30)
        resp.raise_for_status()
        audio_data = resp.content
    except Exception as e:
        log.error(f"Failed to download voice note: {e}")
        return ""

    # Write to temp file
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    # Submit to Sarvam Saaras v3 Batch API
    try:
        from sarvamai import SarvamAI
        client = SarvamAI(api_subscription_key=SARVAM_KEY)

        job = client.speech_to_text_job.create_job(
            model="saaras:v3",
            mode="verbatim",
            language_code="ta-IN",  # handles both Tamil and English
            with_diarization=False,  # single speaker (lead)
            num_speakers=1,
        )
        job.upload_files(file_paths=[tmp_path])
        job.start()
        job.wait_until_complete()

        import os as _os
        output_dir = tempfile.mkdtemp()
        job.download_outputs(output_dir=output_dir)
        output_files = list(Path(output_dir).glob("*.json"))

        if not output_files:
            return ""

        result = json.loads(output_files[0].read_text())
        # Extract transcript
        transcript = result.get("transcript", "")
        if not transcript:
            entries = result.get("diarized_transcript", {}).get("entries", [])
            transcript = " ".join(e.get("transcript","") for e in entries)

        log.info(f"Transcribed voice note: '{transcript[:100]}'")
        return transcript.strip()

    except ImportError:
        log.error("sarvamai not installed. Run: pip install sarvamai")
        return ""
    except Exception as e:
        log.error(f"Sarvam transcription failed: {e}")
        return ""
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass

# ── Inbound routing ───────────────────────────────────────────────────────────

def send_whatsapp_text(phone: str, message: str):
    """Send a WhatsApp text via WasenderAPI."""
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("91") and len(digits) == 12:
        pass  # already correct
    elif len(digits) == 10:
        digits = "91" + digits

    try:
        requests.post(
            f"{WASENDER_BASE_URL}/send-message",
            headers={"Authorization": f"Bearer {WASENDER_API_KEY}"},
            json={
                "session": WASENDER_SESSION,
                "to": digits,
                "type": "text",
                "text": {"body": message},
            },
            timeout=15,
        )
    except Exception as e:
        log.error(f"Failed to send WhatsApp to {phone}: {e}")


def write_inbound_log(event: dict):
    """Append inbound event to log file."""
    path = Path(INBOUND_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_routing_queue(action: str, lead_id: str, data: dict):
    """Write a routing instruction to the main queue for agents to pick up."""
    event = {
        "source":    "inbound_handler",
        "action":    action,
        "lead_id":   lead_id,
        "data":      data,
        "ts":        __import__("datetime").datetime.utcnow().isoformat(),
    }
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook/inbound", methods=["POST"])
def inbound_webhook():
    """
    WasenderAPI inbound webhook.
    Expected payload:
    {
        "session": "...",
        "message": {
            "from": "919876543210@s.whatsapp.net",
            "type": "text" | "audio" | "image" | "document" | "video",
            "text": {"body": "..."},           // for text
            "audio": {"url": "..."},           // for voice notes
            "image": {"url": "...", "caption": "..."},
            "document": {"url": "...", "filename": "..."},
        }
    }
    """
    payload = request.get_json(silent=True) or {}
    msg     = payload.get("message", {})

    if not msg:
        return jsonify({"status": "ignored"}), 200

    # Extract sender phone
    from_jid = msg.get("from", "")
    phone    = from_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
    lead_id  = get_lead_id_from_phone(phone)
    msg_type = msg.get("type", "text")

    log.info(f"Inbound [{msg_type}] from {phone} (lead_id={lead_id})")

    event = {
        "ts":       __import__("datetime").datetime.utcnow().isoformat(),
        "phone":    phone,
        "lead_id":  lead_id,
        "type":     msg_type,
    }

    # ── Text message ──────────────────────────────────────────────────────────
    if msg_type == "text":
        reply_text = msg.get("text", {}).get("body", "").strip()
        event["text"] = reply_text

        # Score + sentiment
        result = process_inbound_reply(lead_id, reply_text)
        event["sentiment"] = result["sentiment"]
        event["action"]    = result["action"]

        # Record A/B outcome
        record_outcome(lead_id, "TEST_DAY3_MESSAGE",
                       "replied" if result["sentiment"] != "OPT_OUT" else "opted_out")

        # Route based on action
        if result["action"] == "mark_lost":
            write_routing_queue("mark_lost", lead_id, {
                "reason": "opt_out",
                "phone":  phone,
                "reply":  reply_text,
            })
        elif result["action"] == "escalate":
            write_routing_queue("escalate", lead_id, {
                "trigger":  "near_close_signal",
                "phone":    phone,
                "reply":    reply_text,
                "score":    result["score"],
            })
        elif result["action"] == "objection_handler":
            write_routing_queue("objection_handler", lead_id, {
                "phone":     phone,
                "reply":     reply_text,
                "sentiment": result["sentiment"],
            })
        elif result["action"] == "accelerate":
            write_routing_queue("accelerate", lead_id, {
                "phone":  phone,
                "score":  result["score"],
                "band":   result["band"],
            })
        # NEUTRAL / continue — no routing action needed, poller handles next step

    # ── Voice note ────────────────────────────────────────────────────────────
    elif msg_type == "audio":
        audio_url = msg.get("audio", {}).get("url", "")
        event["audio_url"] = audio_url

        # Transcribe
        transcript = transcribe_voice_note(audio_url) if audio_url else ""
        event["transcript"] = transcript

        if transcript:
            # Treat transcript as text reply
            result = process_inbound_reply(lead_id, transcript)
            event["sentiment"] = result["sentiment"]
            event["action"]    = result["action"]

            write_routing_queue("inbound_voice", lead_id, {
                "phone":      phone,
                "transcript": transcript,
                "sentiment":  result["sentiment"],
                "action":     result["action"],
            })
        else:
            # Transcription failed — flag for human review
            write_routing_queue("voice_transcription_failed", lead_id, {
                "phone":     phone,
                "audio_url": audio_url,
            })
            # Send acknowledgement to lead
            send_whatsapp_text(phone,
                "Thanks for the voice note! Give me a moment to listen — "
                "I'll get back to you shortly.")

    # ── Image or document ─────────────────────────────────────────────────────
    elif msg_type in ("image", "document"):
        file_url = msg.get(msg_type, {}).get("url", "")
        event["file_url"] = file_url
        event["action"]   = "escalate_high_intent"

        # Sending a document (bank statement, property deed, etc.) = near-close signal
        update_score(lead_id, "lead_near_close", {"trigger": f"sent_{msg_type}"})

        # Immediate escalation alert to sales line
        alert = (
            f"🚨 HIGH INTENT — Pinnacle Block B\n\n"
            f"Lead {lead_id} ({phone}) sent a {msg_type} via WhatsApp.\n"
            f"This is a near-close signal — contact immediately.\n"
            f"File: {file_url or 'see WhatsApp'}"
        )
        send_whatsapp_text(SALES_LINE, alert)

        # Acknowledge lead
        send_whatsapp_text(phone,
            "Thanks for sharing that! Our senior team will review and "
            "reach out to you shortly.")

        write_routing_queue("escalate", lead_id, {
            "trigger":  f"sent_{msg_type}",
            "phone":    phone,
            "file_url": file_url,
        })

    # ── Video ─────────────────────────────────────────────────────────────────
    elif msg_type == "video":
        event["action"] = "flag_human_review"
        write_routing_queue("human_review", lead_id, {
            "phone":  phone,
            "reason": "lead_sent_video",
        })
        send_whatsapp_text(phone,
            "Thanks for sharing! Our team will take a look and get back to you.")

    # ── Log everything ────────────────────────────────────────────────────────
    write_inbound_log(event)
    return jsonify({"status": "processed", "action": event.get("action", "none")}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
