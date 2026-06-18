"""
ab_router.py
Pinnacle Block B — A/B Testing Router

Assigns leads to variants at intake and tracks response outcomes.
Auto-promotes winner after 50 responses per variant.

Active tests:
  TEST_DAY3_MESSAGE   — ₹99/day framing (A) vs 6% yield framing (B)
  TEST_DAY1_VIDEO     — V6 default (A) vs V7 for all leads regardless of UTM (B)
  TEST_SITE_VISIT     — in-person first (A) vs virtual first (B)

After 50 responses per variant:
  - Auto-promote winner (higher reply rate)
  - Log result to ab_log.json (still file-based — low volume, read by Campaign Intelligence)
  - Variant ASSIGNMENT now lives in lead_state (Postgres) via db.schema —
    this is the source of truth sequence_scheduler reads from.
  - record_outcome() still aggregates into ab_log.json / active_variants.json
    for the Campaign Intelligence weekly summary.
"""

import os
import json
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import db.schema as db

AB_LOG_FILE   = os.environ.get("AB_LOG_FILE",   "data/ab_log.json")
VARIANTS_FILE = os.environ.get("VARIANTS_FILE", "data/active_variants.json")

MIN_SAMPLE = 50  # minimum responses per variant before declaring winner

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── File helpers ──────────────────────────────────────────────────────────────
# Used only for the low-volume aggregate test-outcome logs (ab_log.json,
# active_variants.json) — NOT for per-lead variant assignment, which now
# lives in lead_state (Postgres). See assign_variants()/get_variant() below.

def _load(path: str) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save(path: str, data: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ── Test definitions ──────────────────────────────────────────────────────────

TESTS = {
    "TEST_DAY3_MESSAGE": {
        "description": "Day 3 message framing",
        "A": "cashflow_99_day",    # ₹99/day net outflow framing
        "B": "yield_6_percent",    # 6% contracted yield framing
    },
    "TEST_DAY1_VIDEO": {
        "description": "Day 1 intro video",
        "A": "utm_based",          # V6 or V7 based on UTM (current default)
        "B": "v7_for_all",         # V7 for all leads regardless of UTM
    },
    "TEST_SITE_VISIT": {
        "description": "Site visit push framing",
        "A": "inperson_first",     # in-person visit pushed first
        "B": "virtual_first",      # virtual walkthrough pushed first
    },
}

# ── Variant assignment ────────────────────────────────────────────────────────

def assign_variants(lead_id: str) -> dict:
    """
    Assign lead to A or B for each active test.
    Assignment is random 50/50.

    NOTE: This function only COMPUTES the assignment — it does not write
    to lead_state itself. sequence_scheduler.intake_lead() takes the returned
    dict and writes ab_variant_day3 / ab_variant_day1 / ab_variant_visit
    columns as part of the single upsert_lead_state() call, so there's one
    write path for new lead intake instead of two.

    Returns {test_id: variant_letter} dict.
    """
    lid = str(lead_id)

    # If lead already exists in lead_state, reuse its existing assignment
    # (same lead must always get the same variant).
    existing = db.get_lead_state(lid)
    if existing and existing.get("ab_variant_day3"):
        return {
            "TEST_DAY3_MESSAGE": existing.get("ab_variant_day3"),
            "TEST_DAY1_VIDEO":   existing.get("ab_variant_day1"),
            "TEST_SITE_VISIT":   existing.get("ab_variant_visit"),
        }

    assignment = {test_id: random.choice(["A", "B"]) for test_id in TESTS}
    log.info(f"AB assigned [{lid}]: {assignment}")
    return assignment


def get_variant(lead_id: str, test_id: str) -> Optional[str]:
    """Get variant letter (A or B) for a specific test, reading from lead_state."""
    lead = db.get_lead_state(str(lead_id))
    if not lead:
        return None
    col_map = {
        "TEST_DAY3_MESSAGE": "ab_variant_day3",
        "TEST_DAY1_VIDEO":   "ab_variant_day1",
        "TEST_SITE_VISIT":   "ab_variant_visit",
    }
    col = col_map.get(test_id)
    return lead.get(col) if col else None


def get_variant_value(lead_id: str, test_id: str) -> Optional[str]:
    """Get the actual variant value (e.g. 'cashflow_99_day') for a lead + test."""
    letter = get_variant(lead_id, test_id)
    if not letter:
        return None
    return TESTS.get(test_id, {}).get(letter)

# ── Outcome tracking ──────────────────────────────────────────────────────────

def record_outcome(lead_id: str, test_id: str, outcome: str):
    """
    Record an outcome for a lead's test variant.
    outcome: "replied" | "site_visit_booked" | "no_response" | "opted_out"
    Triggers auto-promote check after recording.
    """
    log_data = _load(AB_LOG_FILE)
    lid = str(lead_id)
    variant = get_variant(lid, test_id)
    if not variant:
        return

    if test_id not in log_data:
        log_data[test_id] = {
            "A": {"replied": 0, "site_visit_booked": 0, "no_response": 0, "opted_out": 0, "total": 0},
            "B": {"replied": 0, "site_visit_booked": 0, "no_response": 0, "opted_out": 0, "total": 0},
            "winner": None,
            "decided_at": None,
        }

    log_data[test_id][variant][outcome] = log_data[test_id][variant].get(outcome, 0) + 1
    log_data[test_id][variant]["total"] += 1
    _save(AB_LOG_FILE, log_data)

    log.info(f"AB outcome [{lid}] {test_id}/{variant}: {outcome} "
             f"(total A={log_data[test_id]['A']['total']} B={log_data[test_id]['B']['total']})")

    # Check if we can declare a winner
    _check_and_promote(test_id, log_data)


def _check_and_promote(test_id: str, log_data: dict):
    """Auto-promote winner if both variants have >= MIN_SAMPLE responses."""
    test = log_data.get(test_id, {})
    if test.get("winner"):
        return  # already decided

    a_total = test.get("A", {}).get("total", 0)
    b_total = test.get("B", {}).get("total", 0)

    if a_total < MIN_SAMPLE or b_total < MIN_SAMPLE:
        return  # not enough data yet

    # Compare reply rates
    a_rate = test["A"]["replied"] / a_total if a_total else 0
    b_rate = test["B"]["replied"] / b_total if b_total else 0

    winner = "A" if a_rate >= b_rate else "B"
    winner_value = TESTS[test_id][winner]

    log_data[test_id]["winner"] = winner
    log_data[test_id]["decided_at"] = datetime.now(timezone.utc).isoformat()
    log_data[test_id]["a_reply_rate"] = round(a_rate, 3)
    log_data[test_id]["b_reply_rate"] = round(b_rate, 3)
    _save(AB_LOG_FILE, log_data)

    # Update active variants file so Nurturer uses the winner
    active = _load(VARIANTS_FILE)
    active[test_id] = {
        "winner": winner,
        "value": winner_value,
        "a_rate": round(a_rate * 100, 1),
        "b_rate": round(b_rate * 100, 1),
        "decided_at": log_data[test_id]["decided_at"],
        "description": TESTS[test_id]["description"],
    }
    _save(VARIANTS_FILE, active)

    log.info(f"AB WINNER [{test_id}]: {winner} ({winner_value}) "
             f"— A={a_rate*100:.1f}% vs B={b_rate*100:.1f}% reply rate (n={a_total}/{b_total})")


def get_ab_summary() -> str:
    """
    Return a plain-English summary of all A/B test results.
    Used by Campaign Intelligence agent.
    """
    log_data  = _load(AB_LOG_FILE)
    active    = _load(VARIANTS_FILE)
    lines     = ["── A/B Test Results ──"]

    for test_id, definition in TESTS.items():
        test = log_data.get(test_id, {})
        a    = test.get("A", {})
        b    = test.get("B", {})
        a_n  = a.get("total", 0)
        b_n  = b.get("total", 0)
        a_r  = round(a.get("replied", 0) / a_n * 100, 1) if a_n else 0
        b_r  = round(b.get("replied", 0) / b_n * 100, 1) if b_n else 0

        status = "UNDECIDED"
        if test.get("winner"):
            w     = test["winner"]
            wval  = definition[w]
            status = f"WINNER: {w} ({wval}) — {a_r}% vs {b_r}% reply rate"
        else:
            needed = max(0, MIN_SAMPLE - a_n), max(0, MIN_SAMPLE - b_n)
            status = f"In progress — A: {a_n}/{MIN_SAMPLE}, B: {b_n}/{MIN_SAMPLE} responses"

        lines.append(f"\n{definition['description']}:")
        lines.append(f"  A ({definition['A']}): {a_n} responses, {a_r}% reply rate")
        lines.append(f"  B ({definition['B']}): {b_n} responses, {b_r}% reply rate")
        lines.append(f"  Status: {status}")

    return "\n".join(lines)


if __name__ == "__main__":
    # Smoke test — NOTE: assign_variants() only computes an assignment.
    # In production, sequence_scheduler.intake_lead() writes it to lead_state
    # in the same upsert as the rest of the lead row. Calling assign_variants()
    # standalone here (with no lead_state row) will just show a fresh random
    # 50/50 pick each run, not a persisted one.
    test_id = "TEST_LEAD_001"
    variants = assign_variants(test_id)
    print(f"Computed variants (not yet persisted — needs intake_lead()): {variants}")
    print(get_ab_summary())
