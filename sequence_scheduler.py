"""
sequence_scheduler.py
Pinnacle Block B — Sequence Clock

Two functions called from the poller loop:

  intake_lead(brief)  — called once per new qualifying lead
                        writes to lead_state, assigns A/B, schedules Day 1

  tick()              — called every 30-minute poll cycle
                        finds leads with next_msg_due_at <= now
                        sends the correct message via WasenderAPI
                        advances next_msg_due_at

Also handles:
  - Score-based pace adjustments (COLD band = +3 days between messages)
  - HOT band fast-track to Scheduler
  - Scheduler / Escalator handoffs
  - 90-day silence rule
"""

import os
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

import db.schema as db
from message_templates import get_message, get_next_message_number
from ab_router import assign_variants, get_variant, record_outcome

log = logging.getLogger(__name__)

WASENDER_API_KEY  = os.environ.get("WASENDER_API_KEY", "")
WASENDER_SESSION  = os.environ.get("WASENDER_SESSION_ID", "")
WASENDER_BASE     = "https://api.wasenderapi.com/api"

SALES_LINE        = os.environ.get("SALES_LINE_PHONE",  "919840097140")
SUSMIN_LINE       = os.environ.get("SUSMIN_PHONE",       "918879036002")

BROCHURE_URL      = os.environ.get("BROCHURE_URL",      "")
FLOOR_PLAN_URL    = os.environ.get("FLOOR_PLAN_URL",     "")
PRICING_URL       = os.environ.get("PRICING_URL",        "")

# Extra days added between messages when score band is COLD
COLD_BAND_EXTRA_DAYS = 3


# ── WasenderAPI helpers ───────────────────────────────────────────────────────

def _wa_send_text(phone: str, text: str) -> Optional[str]:
    """Send a plain text WhatsApp message. Returns wasender message_id or None."""
    try:
        resp = requests.post(
            f"{WASENDER_BASE}/send-message",
            headers={"Authorization": f"Bearer {WASENDER_API_KEY}"},
            json={
                "session": WASENDER_SESSION,
                "to":      phone,
                "type":    "text",
                "text":    {"body": text},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message_id") or data.get("id")
    except Exception as e:
        log.error(f"WasenderAPI text send failed to {phone}: {e}")
        return None


def _wa_send_video(phone: str, video_url: str, caption: str) -> Optional[str]:
    """Send a video message with caption."""
    if not video_url:
        log.warning(f"Video URL empty for {phone} — skipping video, sending caption as text")
        return _wa_send_text(phone, caption)
    try:
        resp = requests.post(
            f"{WASENDER_BASE}/send-message",
            headers={"Authorization": f"Bearer {WASENDER_API_KEY}"},
            json={
                "session": WASENDER_SESSION,
                "to":      phone,
                "type":    "video",
                "video":   {"url": video_url, "caption": caption},
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message_id") or data.get("id")
    except Exception as e:
        log.error(f"WasenderAPI video send failed to {phone}: {e}")
        # Fallback: send caption as text
        return _wa_send_text(phone, caption)


def _wa_send_doc(phone: str, doc_url: str, filename: str, caption: str = "") -> Optional[str]:
    """Send a document (PDF)."""
    if not doc_url:
        return None
    try:
        resp = requests.post(
            f"{WASENDER_BASE}/send-message",
            headers={"Authorization": f"Bearer {WASENDER_API_KEY}"},
            json={
                "session":  WASENDER_SESSION,
                "to":       phone,
                "type":     "document",
                "document": {"url": doc_url, "filename": filename, "caption": caption},
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message_id") or data.get("id")
    except Exception as e:
        log.error(f"WasenderAPI doc send failed to {phone}: {e}")
        return None


def _send_docs(phone: str):
    """Send brochure, floor plans, and pricing sheet."""
    if BROCHURE_URL:
        _wa_send_doc(phone, BROCHURE_URL,   "Pinnacle_Block_B_Brochure.pdf",    "Brochure")
    if FLOOR_PLAN_URL:
        _wa_send_doc(phone, FLOOR_PLAN_URL, "Pinnacle_Block_B_Floor_Plans.pdf", "Floor Plans")
    if PRICING_URL:
        _wa_send_doc(phone, PRICING_URL,    "Pinnacle_Block_B_Pricing.pdf",     "Pricing Sheet")


# ── intake_lead ───────────────────────────────────────────────────────────────

def intake_lead(brief: dict) -> bool:
    """
    Called by the poller when a new lead qualifies.

    Writes a row to lead_state.
    Assigns A/B variants.
    Sets next_msg_due_at = NOW (Day 1 fires on the very next tick).

    Returns True if lead was new and written, False if already exists.
    """
    lead_id = str(brief.get("lead_id", ""))
    if not lead_id:
        log.warning("intake_lead: no lead_id in brief — skipping")
        return False

    # Check if already in lead_state
    existing = db.get_lead_state(lead_id)
    if existing:
        log.debug(f"intake_lead: {lead_id} already in lead_state — skipping")
        return False

    # Assign A/B variants
    variants = assign_variants(lead_id)

    data = {
        "lead_id":          lead_id,
        "phone":            brief.get("phone", ""),
        "name":             brief.get("name", "Friend"),
        "track":            brief.get("track", "COLD"),
        "archetype":        brief.get("archetype", "YIELD_SHOPPER"),
        "intro_video":      brief.get("intro_video", "V6"),
        "language":         brief.get("language", "EN"),
        "form_position":    brief.get("form_position"),
        "sell_do_stage":    brief.get("sell_do_stage"),
        "lead_age_days":    brief.get("lead_age_days", 0),
        "near_escalation":  brief.get("near_escalation", False),
        "utm_source":       brief.get("utm_source"),
        "utm_campaign":     brief.get("utm_campaign"),
        "utm_content":      brief.get("utm_content"),
        "ab_variant_day3":  variants.get("TEST_DAY3_MESSAGE"),
        "ab_variant_day1":  variants.get("TEST_DAY1_VIDEO"),
        "ab_variant_visit": variants.get("TEST_SITE_VISIT"),
    }

    db.upsert_lead_state(data)
    log.info(
        f"intake_lead: {lead_id} ({data['name']}) | "
        f"track={data['track']} arch={data['archetype']} "
        f"ab_d3={data['ab_variant_day3']} ab_d1={data['ab_variant_day1']}"
    )
    return True


# ── _compute_next_due ─────────────────────────────────────────────────────────

def _compute_next_due(
    lead: dict,
    next_msg_number: int,
    base_offset_days: int,
) -> datetime:
    """
    Compute when the next message should be sent.
    Adds COLD_BAND_EXTRA_DAYS if the lead's score band is COLD.
    """
    extra = COLD_BAND_EXTRA_DAYS if lead.get("score_band") == "COLD" else 0
    total_offset = base_offset_days + extra
    return datetime.now(timezone.utc) + timedelta(days=total_offset)


# ── _handle_send_result ───────────────────────────────────────────────────────

def _handle_send_result(lead: dict, payload: dict, wasender_id: Optional[str]):
    """
    After a message send:
    - Write to message_log
    - Advance next_msg_number and next_msg_due_at in lead_state
    - Update score (message_delivered event)
    """
    from scorer import update_score   # import here to avoid circular

    lead_id   = lead["lead_id"]
    track     = lead["track"]
    archetype = lead["archetype"]
    msg_num   = lead["next_msg_number"]
    phase     = payload.get("phase", 1)

    # Score event
    score_event = "video_sent" if payload.get("video_tag") else "message_delivered"
    update_score(lead_id, score_event)

    # A/B outcome logging
    ab_test = payload.get("ab_test")
    if ab_test:
        variant = get_variant(lead_id, ab_test)
        if variant:
            record_outcome(lead_id, ab_test, "delivered")

    # Determine next message number
    next_msg = get_next_message_number(track, msg_num, phase)

    # If phase 1 exhausted, roll into phase 2
    if next_msg == 0 and phase == 1:
        next_msg = 31
        phase = 2

    # Log to message_log
    db.log_message(
        lead_id=lead_id,
        phone=lead["phone"],
        message_number=msg_num,
        phase=payload.get("phase", 1),
        track=track,
        archetype=archetype,
        text_body=payload.get("text", ""),
        video_tag=payload.get("video_tag"),
        video_url=payload.get("video_url"),
        has_docs=payload.get("docs", False),
        ab_test_id=ab_test,
        ab_variant=get_variant(lead_id, ab_test) if ab_test else None,
        wasender_message_id=wasender_id,
    )

    if next_msg == 0:
        # Sequence complete — set status to hold, no more sends
        db.update_lead_state(lead_id, {
            "status":         "hold",
            "last_msg_number": msg_num,
            "last_msg_sent_at": "NOW()",
            "next_msg_number": 0,
        })
        log.info(f"tick: {lead_id} sequence complete after msg {msg_num}")
        return

    # Compute next send time
    next_due = _compute_next_due(lead, next_msg, payload.get("next_day_offset", 3))

    db.update_lead_state(lead_id, {
        "last_msg_number":  msg_num,
        "last_msg_sent_at": "NOW()",
        "next_msg_number":  next_msg,
        "next_msg_due_at":  next_due.isoformat(),
        "current_day":      (datetime.now(timezone.utc) - lead["entry_date"]).days + 1
                            if lead.get("entry_date") else 1,
    })

    log.info(
        f"tick: {lead_id} sent msg {msg_num} → "
        f"next={next_msg} due={next_due.strftime('%Y-%m-%d %H:%M')} UTC"
    )


# ── _handle_scheduler_handoff ─────────────────────────────────────────────────

def _handle_scheduler_handoff(lead: dict, note: str):
    """
    Pass a lead to the Scheduler agent.
    Updates lead_state status to 'scheduler'.
    Logs the handoff.
    """
    lead_id = lead["lead_id"]
    db.update_lead_state(lead_id, {
        "status":          "scheduler",
        "last_msg_number": lead.get("next_msg_number", 0),
        "last_msg_sent_at": "NOW()",
    })
    log.info(f"tick: {lead_id} → SCHEDULER | {note}")


# ── _handle_escalator_handoff ─────────────────────────────────────────────────

def _handle_escalator_handoff(lead: dict, note: str):
    """
    Pass a lead to the Escalator agent.
    Updates lead_state status to 'escalated'.
    """
    lead_id = lead["lead_id"]
    db.update_lead_state(lead_id, {
        "status":          "escalated",
        "last_msg_number": lead.get("next_msg_number", 0),
        "last_msg_sent_at": "NOW()",
    })
    log.info(f"tick: {lead_id} → ESCALATOR | {note}")


# ── _check_90_day_silence ─────────────────────────────────────────────────────

def _check_90_day_silence(lead: dict) -> bool:
    """
    Returns True if the lead has not replied in 90+ days.
    If so, marks them Lost and stops outreach.
    """
    last_reply = lead.get("last_reply_at")
    entry      = lead.get("entry_date")

    # Use last reply if available, otherwise entry date
    reference = last_reply or entry
    if not reference:
        return False

    if isinstance(reference, str):
        try:
            reference = datetime.fromisoformat(reference.replace("Z", "+00:00"))
        except Exception:
            return False

    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    age_days = (datetime.now(timezone.utc) - reference).days
    if age_days >= 90:
        db.update_lead_state(lead["lead_id"], {
            "status": "lost",
        })
        log.info(f"tick: {lead['lead_id']} marked Lost — 90-day silence")
        return True

    return False


# ── tick ──────────────────────────────────────────────────────────────────────

def tick():
    """
    Main sequence clock. Called every 30-minute poll cycle.

    Scans lead_state for leads with next_msg_due_at <= NOW and status = active.
    For each due lead:
      1. Check 90-day silence rule
      2. HOT band → fast-track to Scheduler
      3. Fetch message template
      4. If action=pass_to_scheduler → hand off
      5. If action=pass_to_escalator → hand off
      6. If action=send → send via WasenderAPI, advance sequence
    """
    if not WASENDER_API_KEY:
        log.warning("tick: WASENDER_API_KEY not set — skipping")
        return

    try:
        conn = db.get_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT * FROM lead_state
            WHERE status = 'active'
              AND next_msg_due_at <= NOW()
            ORDER BY next_msg_due_at ASC
            LIMIT 50
        """)
        due_leads = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        log.error(f"tick: DB query failed: {e}")
        return

    if not due_leads:
        log.debug("tick: no leads due")
        return

    log.info(f"tick: {len(due_leads)} lead(s) due for next message")

    for lead in due_leads:
        lead_id   = lead["lead_id"]
        name      = lead.get("name", "Friend")
        phone     = lead.get("phone", "")
        track     = lead.get("track", "COLD")
        archetype = lead.get("archetype", "YIELD_SHOPPER")
        msg_num   = lead.get("next_msg_number", 1)
        score_band = lead.get("score_band", "COLD")

        if not phone:
            log.warning(f"tick: {lead_id} has no phone — skipping")
            continue

        # ── 90-day silence check ───────────────────────────────────────────
        if _check_90_day_silence(lead):
            continue

        # ── HOT fast-track ─────────────────────────────────────────────────
        if score_band == "HOT":
            _handle_scheduler_handoff(
                lead,
                note=f"HOT band (score={lead.get('score')}) — fast-track to scheduler"
            )
            continue

        # ── Fetch template ─────────────────────────────────────────────────
        payload = get_message(
            track=track,
            archetype=archetype,
            message_number=msg_num,
            name=name,
            intro_video=lead.get("intro_video", "V6"),
            language=lead.get("language", "EN"),
            ab_variant_day3=lead.get("ab_variant_day3"),
            ab_variant_day1=lead.get("ab_variant_day1"),
        )

        if payload is None:
            log.warning(
                f"tick: {lead_id} no template for "
                f"track={track} arch={archetype} msg={msg_num} — marking hold"
            )
            db.update_lead_state(lead_id, {"status": "hold"})
            continue

        action = payload.get("action", "send")

        # ── Scheduler handoff ──────────────────────────────────────────────
        if action == "pass_to_scheduler":
            _handle_scheduler_handoff(lead, payload.get("action_note", ""))
            continue

        # ── Escalator handoff ──────────────────────────────────────────────
        if action == "pass_to_escalator":
            _handle_escalator_handoff(lead, payload.get("action_note", ""))
            continue

        # ── Send message ───────────────────────────────────────────────────
        text      = payload.get("text", "")
        video_tag = payload.get("video_tag")
        video_url = payload.get("video_url", "")
        has_docs  = payload.get("docs", False)

        wasender_id = None

        if video_tag and text:
            # Send video with caption
            wasender_id = _wa_send_video(phone, video_url, text)
        elif text:
            wasender_id = _wa_send_text(phone, text)

        if has_docs:
            _send_docs(phone)

        _handle_send_result(lead, payload, wasender_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    log.info("Running tick() manually...")
    tick()
