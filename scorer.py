"""
scorer.py
Pinnacle Block B — Lead Scoring + Sentiment Detection

Maintains a score per lead in lead_state (Postgres), not a JSON file.
Called by sequence_scheduler after every outbound send, and by
inbound_handler after every inbound WhatsApp event.

Score bands:
  >= 60  HOT    — accelerate sequence, pass to Scheduler sooner
  30-59  WARM   — standard sequence
  < 30   COLD   — slow down, longer gaps between messages

Sentiment states (returned by classify_sentiment):
  WARM          — positive signal, accelerate
  NEUTRAL       — no signal, continue
  COLD_OBJECTION — hesitation/objection, pass to Objection Handler
  OPT_OUT       — stop all outreach, mark Lost
  NEAR_CLOSE    — token/unit/loan question, immediate Escalator
"""

import os
import logging
import requests
from typing import Optional

import db.schema as db

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Score events ───────────────────────────────────────────────────────────────

SCORE_EVENTS = {
    "message_delivered":       5,
    "lead_replied":           15,
    "lead_asked_question":    20,
    "lead_near_close":        30,   # asked about token/unit/loan
    "site_visit_booked":      15,
    "site_visit_completed":   25,
    "video_sent":              3,
    "document_sent":           2,
    "no_response_7_days":    -10,
    "no_response_14_days":   -20,
    "objection_raised":       -5,
    "reschedule_1":           -5,
    "reschedule_2":          -10,
    "reschedule_3":          -20,
}

# ── Score store (backed by lead_state in Postgres) ────────────────────────────

def get_score(lead_id: str) -> int:
    """Return current score for a lead. Default 10 for new/unknown leads."""
    lead = db.get_lead_state(str(lead_id))
    return lead.get("score", 10) if lead else 10


def get_score_band(lead_id: str) -> str:
    lead = db.get_lead_state(str(lead_id))
    if lead and lead.get("score_band"):
        return lead["score_band"]
    score = get_score(lead_id)
    if score >= 60: return "HOT"
    if score >= 30: return "WARM"
    return "COLD"


def update_score(lead_id: str, event: str, meta: Optional[dict] = None) -> dict:
    """
    Apply a scoring event to a lead. Reads current score from lead_state,
    applies the delta, writes the new score + band back.

    Returns {"score": int, "band": str} for the updated lead.
    If the lead doesn't exist in lead_state yet (e.g. inbound before intake
    completed), this is a no-op and returns the default record — there's
    nothing to update until intake_lead() has created the row.
    """
    lid  = str(lead_id)
    lead = db.get_lead_state(lid)

    if not lead:
        log.warning(f"update_score: {lid} not found in lead_state — skipping")
        return {"score": 10, "band": "COLD"}

    delta = SCORE_EVENTS.get(event, 0)
    new_score = max(0, lead.get("score", 10) + delta)
    new_band  = (
        "HOT"  if new_score >= 60 else
        "WARM" if new_score >= 30 else
        "COLD"
    )

    db.update_lead_state(lid, {
        "score":      new_score,
        "score_band": new_band,
    })

    log.info(f"Score [{lid}] {event} → {delta:+d} = {new_score} ({new_band})")
    return {"score": new_score, "band": new_band}


def get_full_score_record(lead_id: str) -> dict:
    """Compatibility shim — returns score + band in the old dict shape."""
    lead = db.get_lead_state(str(lead_id))
    if not lead:
        return {"score": 10, "band": "COLD"}
    return {"score": lead.get("score", 10), "band": lead.get("score_band", "COLD")}

# ── Sentiment detection ───────────────────────────────────────────────────────

SENTIMENT_PROMPT = """You are analysing a WhatsApp reply from a lead who was contacted about a senior living investment property in Chennai (GTB Pinnacle Block B, ₹37-43L, 6% contracted yield).

Classify the reply into exactly one of these 5 states:

WARM         — positive signal: interested, asking for more, agreeing to visit, says "ok" or "yes" or "looks good" or "tell me more"
NEUTRAL      — no clear signal: "ok", "noted", "will check", "let me know", "I'll think about it" with no positive qualifier
COLD_OBJECTION — hesitation or objection: "too expensive", "too far", "not sure", "EMI is high", "need to discuss with family", "not the right time"
OPT_OUT      — wants to stop: "not interested", "please don't call", "remove me", "stop", "no thanks"
NEAR_CLOSE   — high intent signal: asking about token amount, unit number, sale agreement, loan process, when they can visit, specific floor preference

Reply to classify:
"{reply}"

Respond with ONLY the state name. No explanation. No punctuation. Just the word."""


def classify_sentiment(reply_text: str, lead_id: str = "") -> str:
    """
    Classify inbound WhatsApp reply into sentiment state.
    Uses Claude via Anthropic API.
    Falls back to keyword matching if API unavailable.
    """
    if not reply_text or not reply_text.strip():
        return "NEUTRAL"

    # Try Claude classification
    if ANTHROPIC_API_KEY:
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 20,
                    "messages": [{
                        "role": "user",
                        "content": SENTIMENT_PROMPT.format(reply=reply_text[:500])
                    }]
                },
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()["content"][0]["text"].strip().upper()
                valid  = {"WARM","NEUTRAL","COLD_OBJECTION","OPT_OUT","NEAR_CLOSE"}
                if result in valid:
                    log.info(f"Sentiment [{lead_id}]: {result} — '{reply_text[:60]}'")
                    return result
        except Exception as e:
            log.warning(f"Claude sentiment failed: {e} — falling back to keywords")

    # Keyword fallback
    text = reply_text.lower()
    if any(k in text for k in ["not interested","stop","remove","don't call","do not call"]):
        return "OPT_OUT"
    if any(k in text for k in ["token","unit number","sale agreement","loan","when can i visit","book"]):
        return "NEAR_CLOSE"
    if any(k in text for k in ["too far","expensive","high emi","not sure","family","later","some time"]):
        return "COLD_OBJECTION"
    if any(k in text for k in ["yes","ok","interested","good","tell me","share","send","when","sure"]):
        return "WARM"
    return "NEUTRAL"


def process_inbound_reply(lead_id: str, reply_text: str) -> dict:
    """
    Full pipeline for an inbound reply:
    1. Classify sentiment
    2. Update score
    3. Write last_reply_text / last_reply_at / last_sentiment to lead_state
    4. Return routing instruction

    Returns:
    {
        "sentiment":    "WARM" | "NEUTRAL" | "COLD_OBJECTION" | "OPT_OUT" | "NEAR_CLOSE",
        "score":        int,
        "score_before": int,
        "band":         "HOT" | "WARM" | "COLD",
        "action":       "continue" | "accelerate" | "objection_handler" | "escalate" | "mark_lost",
        "lead_id":      str,
    }

    NOTE: does not write to inbound_log itself — the caller (inbound_handler)
    owns that write since it also has the raw message_type / phone context.
    """
    score_before = get_score(lead_id)
    sentiment    = classify_sentiment(reply_text, lead_id)

    # Map sentiment → score event
    event_map = {
        "WARM":           "lead_replied",
        "NEUTRAL":        "lead_replied",
        "COLD_OBJECTION": "objection_raised",
        "OPT_OUT":        "objection_raised",
        "NEAR_CLOSE":     "lead_near_close",
    }
    # If it's a question, boost further
    if "?" in reply_text and sentiment in ("WARM", "NEAR_CLOSE"):
        update_score(lead_id, "lead_asked_question")
    else:
        update_score(lead_id, event_map.get(sentiment, "lead_replied"))

    record = get_full_score_record(lead_id)

    # Persist the reply itself + sentiment onto lead_state
    db.update_lead_state(str(lead_id), {
        "last_reply_text": reply_text,
        "last_reply_at":   "NOW()",
        "last_sentiment":  sentiment,
    })

    # Determine routing action
    action_map = {
        "WARM":           "accelerate"        if record["score"] >= 30 else "continue",
        "NEUTRAL":        "continue",
        "COLD_OBJECTION": "objection_handler",
        "OPT_OUT":        "mark_lost",
        "NEAR_CLOSE":     "escalate",
    }

    result = {
        "sentiment":    sentiment,
        "score":        record["score"],
        "score_before": score_before,
        "band":         record["band"],
        "action":       action_map.get(sentiment, "continue"),
        "lead_id":      str(lead_id),
    }

    log.info(f"Reply processed [{lead_id}]: sentiment={sentiment} "
             f"score={score_before}→{record['score']} action={result['action']}")
    return result


if __name__ == "__main__":
    # Quick test — NOTE: update_score() and process_inbound_reply() now read/write
    # lead_state in Postgres. If lead_id "TEST_001" doesn't already exist there
    # (i.e. intake_lead() was never called for it), update_score() will log a
    # warning and no-op, and score/band will just stay at the defaults (10/COLD).
    # This still exercises classify_sentiment() and the routing logic correctly —
    # it just won't show real score deltas without a real lead_state row.
    test_replies = [
        "Ok looks interesting, tell me more about the yield",
        "Need to think about it, EMI seems high",
        "Not interested please don't contact me again",
        "What is the token amount and which unit is available?",
        "ok",
    ]
    for reply in test_replies:
        result = process_inbound_reply("TEST_001", reply)
        print(f"'{reply[:50]}' → {result['sentiment']} | {result['action']}")
