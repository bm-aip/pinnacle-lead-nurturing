import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

SELL_DO_API_KEY      = os.environ.get("SELL_DO_API_KEY",      "22c58f46043f2d70474f2314ca72faa7")
SELL_DO_NOTE_API_KEY = os.environ.get("SELL_DO_NOTE_API_KEY", "880fd9ccb71d8b6b8f15b19f7f092936")
SELL_DO_BASE_URL     = "https://app.sell.do"

CONTACTS_FILE        = os.environ.get("CONTACTS_OVERRIDE_FILE", "lead_contacts_override.json")

# Use DATA_DIR env var on Railway, local home dir otherwise
_DATA_DIR = os.environ.get("DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
os.makedirs(_DATA_DIR, exist_ok=True)

QUEUE_PATH      = os.environ.get("QUEUE_PATH",
    os.path.join(_DATA_DIR, "pinnacle_lead_queue.jsonl"))
QUEUED_IDS_FILE = os.environ.get("QUEUED_IDS_FILE",
    os.path.join(_DATA_DIR, "pinnacle_queued_ids.json"))
POLL_INTERVAL_SECONDS = 1800
LEADS_FROM_DATE      = "2026-05-01"
ELEMENTS_UPTOWN_PROJECT_ID = "69d745cf58f1e736a162fe33"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Stage key mapping — confirmed from reporting_lead_stages table ────────────
#
# Pre-sales pipeline:
#   custom_3   = (Pre Sales) Not Connected
#   custom_4   = (Pre Sales) In Progress
#   opportunity= (Pre Sales) Interested
#   prospect   = (Pre Sales) Under Validation
#   custom_2   = (Pre Sales) Site Visit Scheduled  → SKIP
#   booked     = (Pre Sales) Booked                → SKIP
#   lost       = (Pre Sales) Lost                  → SKIP
#   incoming   = (Pre Sales) New                   → SKIP
#   unqualified= (Pre Sales) Unqualified            → SKIP
#
# Sales pipeline:
#   custom_6   = Not Connected
#   custom_7   = In Progress
#   opportunity= Interested
#   prospect   = Under Validation
#   custom_5   = Site Visit Scheduled              → SKIP
#   custom_8   = Site Visit Completed              → post-visit track
#   custom_9   = Booking Cancelled                 → SKIP
#   booked     = Booked                            → SKIP
#   lost       = Lost                              → SKIP
#   incoming   = New                               → SKIP
#   unqualified= Unqualified                       → SKIP

STAGE_TRACK_MAP = {
    # Pre-sales
    "custom_3":   "COLD",   # (Pre Sales) Not Connected
    "custom_4":   "STUCK",  # (Pre Sales) In Progress
    "opportunity":"STUCK",  # (Pre Sales) Interested
    "prospect":   "HOLD",   # (Pre Sales) Under Validation
    # Sales pipeline
    "custom_6":   "COLD",   # Not Connected
    "custom_7":   "STUCK",  # In Progress
    # opportunity already mapped above — Interested (both pipelines)
    # prospect already mapped above — Under Validation (both pipelines)
}

SKIP_STAGE_KEYS = {
    # Pre-sales
    "custom_2",     # (Pre Sales) Site Visit Scheduled
    "booked",       # (Pre Sales) Booked + Sales Booked
    "lost",         # (Pre Sales) Lost + Sales Lost
    "incoming",     # (Pre Sales) New + Sales New
    "unqualified",  # (Pre Sales) Unqualified + Sales Unqualified
    # Sales
    "custom_5",     # Sales Site Visit Scheduled
    "custom_9",     # Sales Booking Cancelled
    # Aliases
    "new", "junk", "dead", "booking_cancelled",
    "not_connected", "in_progress", "under_validation", "interested",  # old-style keys
}


# ── FIX 2: Name cleaning ──────────────────────────────────────────────────────

def clean_name(raw: str) -> str:
    if not raw:
        return "Friend"
    # Remove if it looks like an email
    if "@" in raw:
        return "Friend"
    # Strip emojis and special chars — keep letters, spaces, dots, hyphens
    cleaned = re.sub(r"[^\w\s.\-']", "", raw, flags=re.UNICODE)
    # Remove sequences that look like garbage (all caps with spaces between each letter)
    # e.g. "V C B J N O K N G" or "M A N J O Y"
    tokens = cleaned.split()
    if len(tokens) > 3 and all(len(t) <= 2 for t in tokens):
        return "Friend"
    # Trim and title-case
    cleaned = " ".join(tokens).strip()
    if len(cleaned) < 2:
        return "Friend"
    # Remove trailing junk like "N/A"
    cleaned = re.sub(r"\bN/A\b", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return "Friend"
    # Title case — but preserve all-caps names like "ILAYARAJA"
    words = cleaned.split()
    result = []
    for w in words:
        if w.isupper() and len(w) > 2:
            result.append(w.title())
        else:
            result.append(w.capitalize())
    return " ".join(result)


# ── FIX 1: Archetype classifier (rebuilt around actual campaign names) ─────────

# Tamil campaign keywords — actual campaign names from Sell.do
TAMIL_CAMPAIGN_KEYWORDS = [
    "chithirai", "thiruvizha", "tamil", "pongal", "deepavali",
    "onam", "_ta", "-ta", "ta_", "tamil_",
]

# Archetype detection based on actual sub_source naming patterns
YIELD_EDUCATED_KEYWORDS = [
    "ashwin", "creator", "video", "webinar", "learn", "education",
    "workshop", "seminar",
]

CASHFLOW_ANCHORED_KEYWORDS = [
    "99", "emi", "outflow", "cashflow", "monthly", "2999", "3000",
    "per month", "instalment",
]

YIELD_SHOPPER_KEYWORDS = [
    "6%", "yield", "return", "roi", "investment", "invest",
    "uptown investment", "6 percent", "rental",
]

def classify_archetype(utms: dict) -> str:
    # Check all UTM fields together
    all_utm = " ".join([
        str(utms.get("utm_content") or ""),
        str(utms.get("utm_campaign") or ""),
        str(utms.get("utm_source") or ""),
        str(utms.get("utm_medium") or ""),
    ]).lower()

    if any(k in all_utm for k in YIELD_EDUCATED_KEYWORDS):
        return "YIELD_EDUCATED"
    if any(k in all_utm for k in CASHFLOW_ANCHORED_KEYWORDS):
        return "CASHFLOW_ANCHORED"
    if any(k in all_utm for k in YIELD_SHOPPER_KEYWORDS):
        return "YIELD_SHOPPER"

    # IVR / phone-in leads — treat as YIELD_SHOPPER by default
    if utms.get("utm_medium") in ("VirtualNumber", "IVR", "ivr"):
        return "YIELD_SHOPPER"

    return "YIELD_SHOPPER"


# ── FIX 5: Language detection (rebuilt around actual campaign names) ───────────

def detect_language(utms: dict) -> str:
    all_utm = " ".join([
        str(utms.get("utm_content") or ""),
        str(utms.get("utm_campaign") or ""),
        str(utms.get("utm_source") or ""),
    ]).lower()
    if any(k in all_utm for k in TAMIL_CAMPAIGN_KEYWORDS):
        return "TA"
    return "EN"


def assign_intro_video(utms: dict, archetype: str) -> str:
    all_utm = " ".join([
        str(utms.get("utm_content") or ""),
        str(utms.get("utm_campaign") or ""),
    ]).lower()
    if any(k in all_utm for k in YIELD_EDUCATED_KEYWORDS):
        return "V7"
    if archetype == "YIELD_EDUCATED":
        return "V7"
    return "V6"


# ── FIX 4: Deduplication ──────────────────────────────────────────────────────

def load_queued_ids() -> set:
    path = Path(QUEUED_IDS_FILE)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_queued_id(lead_id: str):
    ids = load_queued_ids()
    ids.add(str(lead_id))
    Path(QUEUED_IDS_FILE).write_text(
        json.dumps(list(ids), indent=2), encoding="utf-8")


def is_already_queued(lead_id: str) -> bool:
    return str(lead_id) in load_queued_ids()


# ── Phone helpers ─────────────────────────────────────────────────────────────

def normalise_phone_for_api(phone: str) -> str:
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("91") and len(digits) == 12:
        return digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        return digits[1:]
    return digits


def normalise_phone_for_whatsapp(phone: str) -> str:
    digits = "".join(filter(str.isdigit, str(phone)))
    if len(digits) == 10:
        return "91" + digits
    return digits


# ── FIX 9: WhatsApp number validation ─────────────────────────────────────────

def is_valid_mobile(phone: str) -> bool:
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("91"):
        digits = digits[2:]
    # Must be 10 digits and start with 6-9 (Indian mobile)
    return len(digits) == 10 and digits[0] in "6789"


# ── Sell.do REST API ──────────────────────────────────────────────────────────

def fetch_lead_by_phone(phone: str) -> Optional[dict]:
    phone_clean = normalise_phone_for_api(phone)
    try:
        resp = requests.get(
            f"{SELL_DO_BASE_URL}/api/leads/phone/retrieve_lead",
            params={"api_key": SELL_DO_API_KEY, "value": phone_clean},
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("exists", False):
            return None
        return data.get("lead")
    except requests.RequestException as e:
        log.error(f"API error for {phone_clean}: {e}")
        return None


def add_note_to_lead(phone: str, name: str, note_content: str) -> bool:
    url = f"{SELL_DO_BASE_URL}/api/leads/create"
    payload = {
        "sell_do": {
            "form": {
                "lead": {"name": name, "phone": normalise_phone_for_api(phone)},
                "note": {"content": note_content},
            }
        },
        "api_key": SELL_DO_NOTE_API_KEY,
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Note failed for {phone}: {e}")
        return False


# ── Lead parsing helpers ──────────────────────────────────────────────────────

def get_stage_key(lead: dict) -> str:
    stage_data = lead.get("stage_data", {})
    if stage_data.get("id"):
        return str(stage_data["id"]).lower().replace(" ", "_")
    return str(lead.get("stage", "")).lower().replace(" ", "_")


def is_elements_uptown(lead: dict) -> bool:
    return any(
        "elements uptown" in (p.get("project_name") or "").lower()
        for p in lead.get("interested_projects", [])
    )


# ── FIX 10: UTM extraction scoped to Elements Uptown ─────────────────────────

def extract_utms(lead: dict) -> dict:
    campaigns = lead.get("campaigns", [])

    # First: campaigns scoped to Elements Uptown
    uptown = [c for c in campaigns
              if c.get("project_id") == ELEMENTS_UPTOWN_PROJECT_ID]

    # Use earliest Elements Uptown campaign = original entry point
    entry     = uptown[0] if uptown else {}
    generated = lead.get("generated_from", {})
    last      = lead.get("last_campaign", {})

    # If no Uptown-specific campaign, fall back to generated_from
    # (handles IVR / direct call leads)
    source   = entry.get("source")   or generated.get("source")   or last.get("source")
    medium   = entry.get("medium_type") or last.get("medium_type")
    campaign = entry.get("name")     or generated.get("name")     or last.get("name")
    content  = entry.get("sub_source") or generated.get("sub_source") or last.get("sub_source")

    return {
        "utm_source":   source,
        "utm_medium":   medium,
        "utm_campaign": campaign,
        "utm_content":  content,
    }


def get_lead_age_days(lead: dict) -> int:
    lead_age = lead.get("lead_age", {})
    if lead_age.get("unit") == "days":
        try:
            return int(lead_age["value"])
        except (ValueError, TypeError):
            pass
    created = lead.get("created_at", "")
    if not created:
        return 0
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


def was_created_from_date(lead: dict, from_date: str) -> bool:
    created = lead.get("created_at", "")
    if not created:
        return True
    try:
        return created[:10] >= from_date
    except Exception:
        return True


# ── FIX 8: Site visit routing ─────────────────────────────────────────────────

def get_site_visit_status(lead: dict) -> Optional[str]:
    for p in lead.get("interested_projects", []):
        if "elements uptown" in (p.get("project_name") or "").lower():
            return p.get("site_visit_status")
    return None


# ── FIX 7: Corrected near_escalation threshold ────────────────────────────────
# Near escalation = lead has been in pipeline 45+ days with no conversion

NEAR_ESCALATION_DAYS = 45


# ── Qualifier / brief builder ─────────────────────────────────────────────────

def build_qualifier_brief(lead: dict, phone: str) -> dict:
    stage_key        = get_stage_key(lead)
    utms             = extract_utms(lead)
    lead_age         = get_lead_age_days(lead)
    site_visit       = get_site_visit_status(lead)
    raw_name         = lead.get("name") or (
        (lead.get("first_name") or "") + " " + (lead.get("last_name") or "")).strip()

    # FIX 2: Clean the name
    name             = clean_name(raw_name)
    lead_id          = str(lead.get("id") or lead.get("_id") or lead.get("lead_id") or "")
    email            = str(lead.get("email") or "").strip()
    phone_wa         = normalise_phone_for_whatsapp(phone)

    # Skip stages
    if stage_key in SKIP_STAGE_KEYS:
        return {"lead_id": lead_id, "action": "skip", "reason": f"stage: {stage_key}"}

    # Route site visit scheduled leads to Scheduler directly
    # custom_2 = (Pre Sales) Site Visit Scheduled
    # custom_5 = Sales Site Visit Scheduled
    if stage_key in ("custom_2", "custom_5"):
        return {
            "lead_id":           lead_id,
            "name":              name,
            "phone":             phone_wa,
            "action":            "scheduler_reminders_only",
            "site_visit_status": site_visit,
        }

    # FIX 8: Route site visit completed to post-visit track
    # custom_8 = Sales Site Visit Completed
    if stage_key == "custom_8":
        return {
            "lead_id":           lead_id,
            "name":              name,
            "phone":             phone_wa,
            "action":            "post_visit_track",
            "site_visit_status": site_visit,
        }

    track = STAGE_TRACK_MAP.get(stage_key)
    if not track:
        log.warning(f"Unknown stage '{stage_key}' for {lead_id} — skipping")
        return {"lead_id": lead_id, "action": "skip", "reason": f"unknown stage: {stage_key}"}

    if lead_age >= 90:
        return {"lead_id": lead_id, "name": name, "phone": phone_wa,
                "action": "mark_lost", "reason": "90 days silence"}

    # FIX 1: Archetype from actual campaign names
    archetype = classify_archetype(utms)
    # FIX 5: Language from actual campaign names
    language  = detect_language(utms)
    video     = assign_intro_video(utms, archetype)

    return {
        "lead_id":           lead_id,
        "name":              name,
        "phone":             phone_wa,
        "email":             email,
        "track":             track,
        "archetype":         archetype,
        "intro_video":       video,
        "language":          language,
        "lead_age_days":     lead_age,
        # FIX 7: Corrected escalation threshold
        "near_escalation":   lead_age >= NEAR_ESCALATION_DAYS,
        "action":            "nurture",
        "sell_do_stage":     stage_key,
        "site_visit_status": site_visit,
        "utm_source":        utms.get("utm_source"),
        "utm_campaign":      utms.get("utm_campaign"),
        "utm_content":       utms.get("utm_content"),
        "last_contacted_at": str(lead.get("last_contacted_at") or ""),
    }


# ── Queue ─────────────────────────────────────────────────────────────────────

def write_to_queue(brief: dict):
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(brief, ensure_ascii=False) + "\n")
    site = brief.get("site_visit_status") or ""
    log.info(
        f"Queued [{brief['track']}] {brief['name']} "
        f"(stage={brief['sell_do_stage']} age={brief['lead_age_days']}d "
        f"arch={brief['archetype']} lang={brief['language']}"
        + (f" sv={site}" if site and site != "Not Scheduled" else "")
        + ")"
    )


# ── Contacts loader ───────────────────────────────────────────────────────────

def load_contacts() -> dict:
    graph_configured = all([
        os.environ.get("GRAPH_TENANT_ID"),
        os.environ.get("GRAPH_CLIENT_ID"),
        os.environ.get("GRAPH_PEM_KEY") or os.path.exists(
            "C:/Users/bharathimeraki/Downloads/PinnacleLeadPoller_key.pem"),
    ])

    if graph_configured:
        try:
            from sharepoint_reader import fetch_contacts_from_sharepoint
            log.info("Loading contacts from SharePoint...")
            contacts = fetch_contacts_from_sharepoint()
            if contacts:
                log.info(f"Loaded {len(contacts)} contacts "
                         f"({sum(1 for c in contacts.values() if c.get('phone'))} with phone)")
                return contacts
            log.warning("SharePoint returned empty — falling back to local file")
        except Exception as e:
            log.error(f"SharePoint failed: {e} — falling back to local file")

    path = Path(CONTACTS_FILE)
    if not path.exists():
        log.warning(f"No contacts available. Configure Graph API env vars "
                    f"or create {CONTACTS_FILE}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        log.error(f"Failed to load local contacts: {e}")
        return {}


# ── Main poll cycle ───────────────────────────────────────────────────────────

def run_poll_cycle():
    log.info("── Poll cycle starting ──")
    contacts = load_contacts()
    if not contacts:
        log.warning("No contacts loaded.")
        return

    log.info(f"Polling {len(contacts)} contacts...")

    # FIX 4: Load already-queued IDs
    queued_ids = load_queued_ids()
    log.info(f"Already queued from previous cycles: {len(queued_ids)}")

    queued = skipped = no_data = new_unknown_stages = mark_lost_count = 0
    already_queued = 0
    mark_lost_leads = []
    unknown_stages = set()

    for crm_id, contact in contacts.items():
        phone = contact.get("phone", "")
        name  = contact.get("name", f"Lead #{crm_id}")

        if not phone:
            no_data += 1
            continue

        # FIX 9: Validate mobile number before calling API
        if not is_valid_mobile(phone):
            log.debug(f"Invalid mobile {phone} for {name} — skipping")
            no_data += 1
            continue

        lead = fetch_lead_by_phone(phone)
        if lead is None:
            no_data += 1
            continue

        if not was_created_from_date(lead, LEADS_FROM_DATE):
            skipped += 1
            continue

        if not is_elements_uptown(lead):
            skipped += 1
            continue

        brief  = build_qualifier_brief(lead, phone)
        action = brief.get("action")

        if action == "skip":
            # Track unknown stages for reporting
            stage = brief.get("reason", "")
            if "unknown stage" in stage:
                unknown_stages.add(stage.replace("unknown stage: ", ""))
            skipped += 1
            continue

        if action == "mark_lost":
            mark_lost_leads.append(brief)
            skipped += 1
            continue

        lead_id = brief.get("lead_id", "")

        # FIX 4: Skip if already queued in a previous cycle
        if lead_id and lead_id in queued_ids:
            already_queued += 1
            continue

        write_to_queue(brief)

        # FIX 4: Mark as queued so it won't be re-added next cycle
        if lead_id:
            save_queued_id(lead_id)
            queued_ids.add(lead_id)

        queued += 1

    # Write Lost notes
    for brief in mark_lost_leads:
        if brief.get("phone"):
            add_note_to_lead(
                phone=brief["phone"], name=brief["name"],
                note_content=f"[STAGE UPDATE] New stage: Lost | Reason: {brief.get('reason')} | Auto on {datetime.now().date()}"
            )
            mark_lost_count += 1

    log.info(
        f"── Cycle done — "
        f"Queued: {queued} | "
        f"Already in pipeline: {already_queued} | "
        f"Skipped: {skipped} | "
        f"No data/invalid: {no_data} | "
        f"Lost notes: {mark_lost_count} ──"
    )
    if unknown_stages:
        log.warning(f"Unknown stages encountered (add to map): {unknown_stages}")


def main():
    log.info("Pinnacle Block B — Lead Poller starting.")
    log.info(
        f"API key: {SELL_DO_API_KEY[:8]}... | "
        f"Interval: {POLL_INTERVAL_SECONDS//60}min | "
        f"From: {LEADS_FROM_DATE} | "
        f"Queue: {QUEUE_PATH} | "
        f"Dedup file: {QUEUED_IDS_FILE}"
    )
    while True:
        run_poll_cycle()
        log.info(f"Sleeping {POLL_INTERVAL_SECONDS//60} min...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
