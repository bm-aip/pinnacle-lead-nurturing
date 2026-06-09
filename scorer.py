"""
scorer.py
Pinnacle Block B — Lead Scoring + Sentiment Detection

Maintains a score per lead in lead_scores.json.
Called by the poller after every inbound WhatsApp event and outbound send.

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
import json
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCORES_FILE       = os.environ.get("SCORES_FILE", "data/lead_scores.json")
AB_LOG_FILE       = os.environ.get("AB_LOG_FILE", "data/ab_log.json")

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

# ── Score store ───────────────────────────────────────────────────────────────

def _load_scores() -> dict:
    path = Path(SCORES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_scores(scores: dict):
    path = Path(SCORES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scores, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def get_score(lead_id: str) -> int:
    """Return current score for a lead. Default 10 for new leads."""
    return _load_scores().get(str(lead_id), {}).get("score", 10)


def get_score_band(lead_id: str) -> str:
    score = get_score(lead_id)
    if score >= 60: return "HOT"
    if score >= 30: return "WARM"
    return "COLD"


def update_score(lead_id: str, event: str, meta: Optional[dict] = None) -> dict:
    """
    Apply a scoring event to a lead.
    Returns updated score record.
    """
    scores = _load_scores()
    lid    = str(lead_id)

    if lid not in scores:
        scores[lid] = {
            "score":      10,
            "band":       "COLD",
            "events":     [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    delta = SCORE_EVENTS.get(event, 0)
    scores[lid]["score"]  = max(0, scores[lid]["score"] + delta)
    scores[lid]["band"]   = (
        "HOT"  if scores[lid]["score"] >= 60 else
        "WARM" if scores[lid]["score"] >= 30 else
        "COLD"
    )
    scores[lid]["events"].append({
        "event":     event,
        "delta":     delta,
        "score_now": scores[lid]["score"],
        "ts":        datetime.now(timezone.utc).isoformat(),
        "meta":      meta or {},
    })
    scores[lid]["updated_at"] = datetime.now(timezone.utc).isoformat()

    _save_scores(scores)
    log.info(f"Score [{lid}] {event} → {delta:+d} = {scores[lid]['score']} ({scores[lid]['band']})")
    return scores[lid]


def get_full_score_record(lead_id: str) -> dict:
    return _load_scores().get(str(lead_id), {
        "score": 10, "band": "COLD", "events": []
    })

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
    3. Return routing instruction

    Returns:
    {
        "sentiment":   "WARM" | "NEUTRAL" | "COLD_OBJECTION" | "OPT_OUT" | "NEAR_CLOSE",
        "score":       int,
        "band":        "HOT" | "WARM" | "COLD",
        "action":      "continue" | "accelerate" | "objection_handler" | "escalate" | "mark_lost",
        "lead_id":     str,
    }
    """
    sentiment = classify_sentiment(reply_text, lead_id)

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

    # Determine routing action
    action_map = {
        "WARM":           "accelerate"        if record["score"] >= 30 else "continue",
        "NEUTRAL":        "continue",
        "COLD_OBJECTION": "objection_handler",
        "OPT_OUT":        "mark_lost",
        "NEAR_CLOSE":     "escalate",
    }

    result = {
        "sentiment": sentiment,
        "score":     record["score"],
        "band":      record["band"],
        "action":    action_map.get(sentiment, "continue"),
        "lead_id":   str(lead_id),
    }

    log.info(f"Reply processed [{lead_id}]: sentiment={sentiment} "
             f"score={record['score']} action={result['action']}")
    return result


if __name__ == "__main__":
    # Quick test
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
