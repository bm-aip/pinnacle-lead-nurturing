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

Lead lookup is now by phone via lead_state (Postgres) — db.get_lead_by_phone() —
instead of the old JSON lead_registry.json file. Every inbound event is logged
to inbound_log, and lead_state.status is updated according to the routing
decision so the sequence clock (sequence_scheduler.tick()) picks up the
correct next action on its next cycle.

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

import db.schema as db
from scorer import process_inbound_reply, update_score
from ab_router import record_outcome

SARVAM_KEY         = os.environ.get("SARVAM_KEY", "")
WASENDER_API_KEY   = os.environ.get("WASENDER_API_KEY", "")
WASENDER_SESSION   = os.environ.get("WASENDER_SESSION_ID", "")
WASENDER_BASE_URL  = "https://api.wasenderapi.com/api"
SALES_LINE         = os.environ.get("SALES_LINE_PHONE", "919840097140")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Lead lookup (phone → lead_state row, via Postgres) ───────────────────────
# Replaces the old JSON lead_registry.json file. A lead only exists here once
# sequence_scheduler.intake_lead() has run for it.

def get_lead_from_phone(phone: str) -> dict:
    """Look up the full lead_state row from phone number. Returns {} if not found."""
    return db.get_lead_by_phone(phone)

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


def _log_and_route(
    lead: dict,
    phone: str,
    msg_type: str,
    reply_text: str,
    result: dict,
    audio_transcript: str = None,
):
    """
    Single place that:
      1. Writes the inbound event to inbound_log
      2. Applies the lead_state status transition for the routing decision

    `result` is the dict returned by scorer.process_inbound_reply():
      {sentiment, score, score_before, band, action, lead_id}

    Routing → lead_state.status mapping:
      continue           → leave status as "active" (sequence clock keeps going)
      accelerate         → status stays "active"; next_msg_due_at is NOT
                            pulled forward here — the next tick() will see
                            the now-higher score_band and may fast-track if HOT
      objection_handler   → status = "objection_handler" (Nurturer's normal
                            sequence pauses; objection-handling agent works
                            the lead in Paperclip, then a human/agent flips
                            status back to "active" once resolved)
      escalate            → status = "escalated" (Escalator owns it now)
      mark_lost           → status = "lost" (sequence clock stops)
    """
    lead_id = lead.get("lead_id", "UNKNOWN")

    db.log_inbound(
        lead_id=lead_id,
        phone=phone,
        message_type=msg_type,
        reply_text=reply_text,
        sentiment=result.get("sentiment"),
        routing_decision=result.get("action"),
        score_before=result.get("score_before"),
        score_after=result.get("score"),
        audio_transcript=audio_transcript,
    )

    action = result.get("action")

    if not lead_id or lead_id == "UNKNOWN":
        log.warning(f"_log_and_route: no matching lead_state row for phone {phone} — logged but not routed")
        return

    if action == "mark_lost":
        db.update_lead_state(lead_id, {"status": "lost"})
    elif action == "escalate":
        db.update_lead_state(lead_id, {"status": "escalated"})
    elif action == "objection_handler":
        db.update_lead_state(lead_id, {"status": "objection_handler"})
    elif action == "accelerate":
        # Status stays active — score_band is already updated by process_inbound_reply().
        # The next tick() picks up the new band (HOT fast-tracks to Scheduler).
        pass
    # "continue" — no status change needed

    log.info(f"Routed [{lead_id}] action={action} → lead_state.status updated accordingly")


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
    lead     = get_lead_from_phone(phone)
    lead_id  = lead.get("lead_id", "UNKNOWN")
    msg_type = msg.get("type", "text")

    log.info(f"Inbound [{msg_type}] from {phone} (lead_id={lead_id})")

    final_action = "none"

    # ── Text message ──────────────────────────────────────────────────────────
    if msg_type == "text":
        reply_text = msg.get("text", {}).get("body", "").strip()

        if lead_id == "UNKNOWN":
            # No matching lead_state row for this phone — log raw and stop.
            # (Most likely: inbound arrived before intake_lead() ran for this
            # lead, or the number isn't in our pipeline at all.)
            final_action = "unmatched_phone"
            db.log_inbound(
                lead_id=lead_id, phone=phone, message_type="text",
                reply_text=reply_text, sentiment=None,
                routing_decision="unmatched_phone",
                score_before=None, score_after=None,
            )
            log.warning(f"Inbound text from unmatched phone {phone} — no lead_state row")
        else:
            # Score + sentiment + lead_state update (last_reply_text/at/sentiment)
            result = process_inbound_reply(lead_id, reply_text)
            final_action = result["action"]

            # Record A/B outcome
            record_outcome(lead_id, "TEST_DAY3_MESSAGE",
                           "replied" if result["sentiment"] != "OPT_OUT" else "opted_out")

            _log_and_route(lead, phone, "text", reply_text, result)

    # ── Voice note ────────────────────────────────────────────────────────────
    elif msg_type == "audio":
        audio_url = msg.get("audio", {}).get("url", "")

        # Transcribe
        transcript = transcribe_voice_note(audio_url) if audio_url else ""

        if transcript:
            if lead_id == "UNKNOWN":
                final_action = "unmatched_phone"
                db.log_inbound(
                    lead_id=lead_id, phone=phone, message_type="audio",
                    reply_text="", sentiment=None,
                    routing_decision="unmatched_phone",
                    score_before=None, score_after=None,
                    audio_transcript=transcript,
                )
                log.warning(f"Inbound audio from unmatched phone {phone} — no lead_state row")
            else:
                # Treat transcript as text reply
                result = process_inbound_reply(lead_id, transcript)
                final_action = result["action"]
                _log_and_route(lead, phone, "audio", transcript, result,
                                audio_transcript=transcript)
        else:
            # Transcription failed — flag for human review, log with no routing
            final_action = "voice_transcription_failed"
            db.log_inbound(
                lead_id=lead_id, phone=phone, message_type="audio",
                reply_text="", sentiment=None,
                routing_decision="voice_transcription_failed",
                score_before=None, score_after=None,
            )
            if lead_id != "UNKNOWN":
                db.update_lead_state(lead_id, {"status": "human_review"})
            # Send acknowledgement to lead
            send_whatsapp_text(phone,
                "Thanks for the voice note! Give me a moment to listen — "
                "I'll get back to you shortly.")

    # ── Image or document ─────────────────────────────────────────────────────
    elif msg_type in ("image", "document"):
        file_url = msg.get(msg_type, {}).get("url", "")
        final_action = "escalate_high_intent"

        # Sending a document (bank statement, property deed, etc.) = near-close signal
        update_score(lead_id, "lead_near_close", {"trigger": f"sent_{msg_type}"})

        db.log_inbound(
            lead_id=lead_id, phone=phone, message_type=msg_type,
            reply_text=f"[sent {msg_type}: {file_url or 'see WhatsApp'}]",
            sentiment="NEAR_CLOSE", routing_decision="escalate",
            score_before=None, score_after=None,
        )
        if lead_id != "UNKNOWN":
            db.update_lead_state(lead_id, {"status": "escalated"})

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

    # ── Video ─────────────────────────────────────────────────────────────────
    elif msg_type == "video":
        final_action = "flag_human_review"
        db.log_inbound(
            lead_id=lead_id, phone=phone, message_type="video",
            reply_text="[sent video]", sentiment=None,
            routing_decision="human_review",
            score_before=None, score_after=None,
        )
        if lead_id != "UNKNOWN":
            db.update_lead_state(lead_id, {"status": "human_review"})
        send_whatsapp_text(phone,
            "Thanks for sharing! Our team will take a look and get back to you.")

    return jsonify({"status": "processed", "action": final_action}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
