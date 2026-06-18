"""
message_templates.py
Pinnacle Block B — Message Template Lookup

Maps (track, archetype, message_number) to a message payload dict.

Returns:
{
    "text":             str,           # WhatsApp message body (EN)
    "text_ta":          str | None,    # Tamil version (optional)
    "video_tag":        str | None,    # V1-V7 or None
    "docs":             bool,          # whether to attach brochure/floor/pricing PDFs
    "next_day_offset":  int,           # days until next message from today
    "action":           str,           # "send" | "pass_to_scheduler" | "pass_to_escalator"
    "action_note":      str | None,    # note to attach when routing to another agent
    "phase":            int,           # 1 or 2
    "ab_test":          str | None,    # which A/B test applies (if any)
}

Video file paths (update when Cloudflare R2 URLs are ready):
"""

import os

# ── Video URLs ─────────────────────────────────────────────────────────────────
# Update these when V1-V7 are uploaded to Cloudflare R2.
# Set as env vars for easy updates without code changes.

VIDEO_URLS = {
    "V1": os.environ.get("VIDEO_URL_V1", ""),   # FD vs stocks vs senior living
    "V2": os.environ.get("VIDEO_URL_V2", ""),   # Real net yield on Chennai flat
    "V3": os.environ.get("VIDEO_URL_V3", ""),   # Who backs the 6% promise
    "V4": os.environ.get("VIDEO_URL_V4", ""),   # 6-point checklist
    "V5": os.environ.get("VIDEO_URL_V5", ""),   # Kandigai corridor
    "V6": os.environ.get("VIDEO_URL_V6", ""),   # Ashwin project intro
    "V7": os.environ.get("VIDEO_URL_V7", ""),   # Investment video
}

DOCS_AVAILABLE = os.environ.get("DOCS_AVAILABLE", "false").lower() == "true"

RERA_NUMBER = "TN/35/BUILDING/0565/2024"


# ── Template helpers ──────────────────────────────────────────────────────────

def _send(text, text_ta=None, video=None, docs=False, offset=0,
          phase=1, ab_test=None):
    return {
        "text":            text,
        "text_ta":         text_ta,
        "video_tag":       video,
        "video_url":       VIDEO_URLS.get(video) if video else None,
        "docs":            docs,
        "next_day_offset": offset,
        "action":          "send",
        "action_note":     None,
        "phase":           phase,
        "ab_test":         ab_test,
    }


def _scheduler(note, offset=0, phase=1):
    return {
        "text":            None,
        "text_ta":         None,
        "video_tag":       None,
        "video_url":       None,
        "docs":            False,
        "next_day_offset": offset,
        "action":          "pass_to_scheduler",
        "action_note":     note,
        "phase":           phase,
        "ab_test":         None,
    }


def _escalator(note, offset=0, phase=1):
    return {
        "text":            None,
        "text_ta":         None,
        "video_tag":       None,
        "video_url":       None,
        "docs":            False,
        "next_day_offset": offset,
        "action":          "pass_to_escalator",
        "action_note":     note,
        "phase":           phase,
        "ab_test":         None,
    }


# ── COLD track templates ──────────────────────────────────────────────────────

_COLD_SHARED = {
    # Day 1 Msg 1 — intent question
    1: lambda name, **_: _send(
        text=(
            f"Hi {name}, thanks for your interest in GTB Pinnacle Block B at Elements Uptown. "
            f"Before I share anything, one quick question — when you submitted the form, "
            f"were you looking at this for yourself, for a family member, or purely as an investment? "
            f"Helps me share the right details."
        ),
        offset=0
    ),

    # Day 1 Msg 2 — intro video (archetype-specific, handled by scheduler)
    2: lambda name, intro_video="V6", **_: _send(
        text=(
            f"A quick 60-second overview of what Pinnacle Block B is and why it exists. "
            f"Worth a watch, {name} Sir/Madam."
            if intro_video == "V6" else
            f"A 60-second breakdown of the investment case for Block B — "
            f"the numbers, the structure, the why. Have a look, {name} Sir/Madam."
        ),
        video=intro_video,
        offset=0,
        ab_test="TEST_DAY1_VIDEO"
    ),
}

_COLD_YIELD_EDUCATED = {
    **_COLD_SHARED,
    # Day 3 — archetype hook
    3: lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — Block B numbers in brief: ₹43.41 lakh all-in for Type-II. "
            f"₹21,500 contracted rental every month from Month 3. "
            f"Net outflow after EMI offset: ₹2,999/month. "
            f"6% p.a., written into the sale agreement, not market-linked. "
            f"10% rental step-up at Year 3 built in. "
            f"Would a 30-minute virtual walkthrough help you get to a clear yes or no faster?"
        ),
        offset=3,
        ab_test="TEST_DAY3_MESSAGE"
    ),
    6:  lambda name, **_: _send(text=f"{name} Sir/Madam — this 60-second video runs the FD vs stocks vs senior living comparison at the ₹43L ticket size. Worth watching if you're weighing this against other options.", video="V1", offset=3),
    9:  lambda name, **_: _send(text=f"{name} Sir/Madam — a lot of projects promise 6%. This video explains exactly who is making the promise at Pinnacle Block B and what the legal structure behind it looks like. 60 seconds.", video="V3", offset=3),
    13: lambda name, **_: _send(text=f"{name} Sir/Madam — the corridor context for Kandigai. Capital appreciation is not contracted or guaranteed — but the infrastructure story behind this location is worth understanding before you decide.", video="V5", offset=3),
    17: lambda name, **_: _send(text=f"{name} Sir/Madam — six things to verify before paying for any senior living investment. Worth running this checklist on Block B and on any other project you're comparing.", video="V4", offset=4),
    21: lambda name, **_: _send(text=f"{name} Sir/Madam — if you're comparing this with a regular Chennai flat for rental income, this 60-second video is directly relevant. Real net yield on a typical flat after costs is closer to 1.66%, not 3%.", video="V2", offset=4),
    25: lambda name, **_: _scheduler("COLD lead, Day 25. First formal site visit push. Offer in-person and virtual.", offset=0),
    28: lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — sharing the full project documents for your reference. "
            f"RERA: {RERA_NUMBER}. "
            f"If there's one specific thing you'd like clarity on before deciding whether to visit — "
            f"yield mechanics, location, operator, loan — tell me and I'll answer it directly."
        ),
        docs=True,
        offset=2
    ),
}

_COLD_YIELD_SHOPPER = {
    **_COLD_SHARED,
    3: lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — one number worth sitting with: 6% p.a. contracted rental yield, "
            f"monthly disbursement, for 5 years. Not projected. Not market-linked. "
            f"Written into the sale agreement between you, GTB, and Elements. "
            f"For context, a typical Chennai South flat at the same ticket size yields 1.66% net after real costs. "
            f"Would it help to see a side-by-side on WhatsApp?"
        ),
        offset=3,
        ab_test="TEST_DAY3_MESSAGE"
    ),
    6:  lambda name, **_: _send(text=f"{name} Sir/Madam — this 60-second video runs the FD vs stocks vs senior living comparison at the ₹43L ticket size. Worth watching if you're weighing this against other options.", video="V1", offset=3),
    9:  lambda name, **_: _send(text=f"{name} Sir/Madam — a lot of projects promise 6%. This video explains exactly who is making the promise at Pinnacle Block B and what the legal structure behind it looks like. 60 seconds.", video="V3", offset=3),
    13: lambda name, **_: _send(text=f"{name} Sir/Madam — the corridor context for Kandigai. Capital appreciation is not contracted or guaranteed — but the infrastructure story behind this location is worth understanding before you decide.", video="V5", offset=4),
    17: lambda name, **_: _send(text=f"{name} Sir/Madam — six things to verify before paying for any senior living investment. Worth running this checklist on Block B and on any other project you're comparing.", video="V4", offset=4),
    21: lambda name, **_: _send(text=f"{name} Sir/Madam — if you're comparing this with a regular Chennai flat for rental income, this 60-second video is directly relevant. Real net yield on a typical flat after costs is closer to 1.66%, not 3%.", video="V2", offset=4),
    25: lambda name, **_: _scheduler("COLD lead, Day 25. First formal site visit push. Offer in-person and virtual.", offset=0),
    28: lambda name, **_: _send(text=f"{name} Sir/Madam — sharing the full project documents for your reference. RERA: {RERA_NUMBER}. If there's one specific thing you'd like clarity on before deciding whether to visit — yield mechanics, location, operator, loan — tell me and I'll answer it directly.", docs=True, offset=2),
}

_COLD_CASHFLOW_ANCHORED = {
    **_COLD_SHARED,
    3: lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — the number that matters most here isn't the gross EMI of ₹24,499. "
            f"It's ₹2,999 — your net monthly outflow after the ₹21,500 contracted rental offsets the EMI. "
            f"That's ₹99 a day. "
            f"You're building ownership of a rent-yielding, RERA-registered asset for less than a daily coffee. "
            f"Want me to share a one-page breakdown of how this works?"
        ),
        offset=3,
        ab_test="TEST_DAY3_MESSAGE"
    ),
    6:  lambda name, **_: _send(text=f"{name} Sir/Madam — this 60-second video runs the FD vs stocks vs senior living comparison at the ₹43L ticket size. The ₹99/day net outflow framing is in here — worth a watch.", video="V1", offset=3),
    9:  lambda name, **_: _send(text=f"{name} Sir/Madam — who exactly is making the 6% promise at Block B, and what backs it legally. 60 seconds.", video="V3", offset=3),
    13: lambda name, **_: _send(text=f"{name} Sir/Madam — the corridor context for Kandigai. Capital appreciation is not contracted or guaranteed — but the infrastructure story is real.", video="V5", offset=4),
    17: lambda name, **_: _send(text=f"{name} Sir/Madam — six things to verify before paying for any senior living investment. Worth running this checklist on Block B and on anything else you're comparing.", video="V4", offset=4),
    21: lambda name, **_: _send(text=f"{name} Sir/Madam — real net yield on a typical Chennai flat after costs. If you've been comparing Block B's net outflow against self-managed rental income, this 60-second video gives you the honest comparison.", video="V2", offset=4),
    25: lambda name, **_: _scheduler("COLD lead, Day 25. First formal site visit push. Offer in-person and virtual.", offset=0),
    28: lambda name, **_: _send(text=f"{name} Sir/Madam — sharing the full project documents for your reference. RERA: {RERA_NUMBER}. If there's one specific blocker — tell me and I'll answer it directly.", docs=True, offset=2),
}

COLD_TEMPLATES = {
    "YIELD_EDUCATED":    _COLD_YIELD_EDUCATED,
    "YIELD_SHOPPER":     _COLD_YIELD_SHOPPER,
    "CASHFLOW_ANCHORED": _COLD_CASHFLOW_ANCHORED,
}


# ── STUCK track templates ─────────────────────────────────────────────────────

_STUCK_DAY1_BY_OBJECTION = {
    "thinking_about_it": lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — completely understand, this is a real-estate decision, not an impulse. "
            f"Two practical suggestions: (a) a 30-minute virtual walkthrough gives your family actual facts "
            f"to discuss rather than just a brochure; (b) I can share a one-pager formatted for forwarding "
            f"to your spouse or children. Which would be more useful right now?"
        ),
        offset=0
    ),
    "send_details": lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — sending the documents now. "
            f"One thing I'd ask: most leads who say 'I'll revert' don't — simply because the brochure "
            f"doesn't answer the question that's actually on their mind. "
            f"So along with these, tell me the one specific thing you want to be sure about — "
            f"yield, location, operator, loan — and I'll answer it directly."
        ),
        docs=True,
        offset=0
    ),
    "emi_concern": lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — on the EMI: the gross number is ₹24,499 (Type-II). "
            f"But the contracted ₹21,500 monthly rental from Elements offsets this from Month 3. "
            f"Net outflow: ₹2,999/month — ₹99 a day. "
            f"If even that doesn't fit, two options: (a) larger down payment to reduce EMI, "
            f"(b) wait until bandwidth allows. Subject to bank's eligibility check. "
            f"Would a 30-minute virtual walkthrough help you evaluate the full picture?"
        ),
        offset=0
    ),
    "lock_in_concern": lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — worth clarifying: the '5-year lock-in' is not a restriction on "
            f"your money or your right to sell. It is Elements' commitment to pay you rent for 5 years. "
            f"You can resell any time post-handover — no conditions, no transfer fee. "
            f"The new owner inherits the remaining rental contract. "
            f"The 5 years is the yield contract, not a liquidity restriction."
        ),
        offset=0
    ),
    "location_concern": lambda name, **_: _send(
        text=(
            f"{name} Sir/Madam — two things on location. First, a city property at this ticket size "
            f"with a 6% contracted yield does not exist in Chennai. The ₹37L entry point exists "
            f"because Kandigai is on the next growth corridor. Second, the senior-living model needs "
            f"space — quiet, medical on-site, walking distances — which city centres can't offer. "
            f"Capital appreciation is not contracted or guaranteed, but the infrastructure story "
            f"is worth seeing firsthand. Would a virtual walkthrough change the picture?"
        ),
        offset=0
    ),
    "default": lambda name, intro_video="V6", **_: _send(
        text=f"{name} Sir/Madam — a quick re-share of the Block B overview. Happy to pick up wherever we left off — just let me know what's on your mind.",
        video=intro_video,
        offset=0
    ),
}

_STUCK_BASE = {
    3:  lambda name, **_: _COLD_YIELD_EDUCATED[3](name, **_),   # archetype hook reused
    6:  lambda name, **_: _send(text=f"{name} Sir/Madam — FD vs stocks vs senior living at the same ticket size. Worth a look if you're comparing options.", video="V1", offset=3),
    9:  lambda name, **_: _send(text=f"{name} Sir/Madam — who exactly is making the 6% promise at Block B, and what backs it legally. 60 seconds.", video="V3", offset=3),
    13: lambda name, **_: _scheduler("STUCK lead, Day 13. Site visit push. Offer specific tentative slot.", offset=0),
    17: lambda name, **_: _send(text=f"{name} Sir/Madam — six-point checklist for evaluating any senior living investment. Block B passes all six.", video="V4", offset=4),
    21: lambda name, **_: _send(text=f"{name} Sir/Madam — real net yield on a typical Chennai flat after costs.", video="V2", offset=4),
    25: lambda name, **_: _scheduler("STUCK lead, Day 25. Second site visit push. Virtual walkthrough preferred.", offset=0),
    28: lambda name, **_: _send(text=f"{name} Sir/Madam — sharing the complete project documents. RERA: {RERA_NUMBER}. If there's one specific blocker — yield, location, operator, loan — tell me and I'll answer it directly.", docs=True, offset=2),
}

STUCK_TEMPLATES = {
    archetype: {1: _STUCK_DAY1_BY_OBJECTION["default"], **_STUCK_BASE}
    for archetype in ["YIELD_EDUCATED", "YIELD_SHOPPER", "CASHFLOW_ANCHORED"]
}


# ── HOLD track templates ──────────────────────────────────────────────────────

_HOLD_BASE = {
    3:  lambda name, **_: _send(text=f"{name} Sir/Madam — following up as promised. Whenever you have 10 minutes, happy to connect on Pinnacle Block B. No rush — just want to make sure you have everything you need when you're ready to look at this properly.", offset=4),
    7:  lambda name, intro_video="V6", **_: _send(text=f"{name} Sir/Madam — a quick 60-second overview whenever you get a moment.", video=intro_video, offset=6),
    13: lambda name, **_: _scheduler("HOLD lead, Day 13. Offer virtual walkthrough as low-friction entry.", offset=0),
    17: lambda name, **_: _send(text=f"{name} Sir/Madam — a 60-second comparison of where ₹43L works hardest over 10 years.", video="V1", offset=4),
    21: lambda name, **_: _send(text=f"{name} Sir/Madam — the legal structure behind the 6% at Block B. 60 seconds.", video="V3", offset=4),
    25: lambda name, **_: _scheduler("HOLD lead, Day 25. Second walkthrough push.", offset=0),
    28: lambda name, **_: _send(text=f"{name} Sir/Madam — the Kandigai corridor. Capital appreciation is not contracted or guaranteed — but the infrastructure is real.", video="V5", offset=2),
}

HOLD_TEMPLATES = {
    archetype: dict(_HOLD_BASE)
    for archetype in ["YIELD_EDUCATED", "YIELD_SHOPPER", "CASHFLOW_ANCHORED"]
}


# ── Phase 2 templates (Day 31–52, all tracks) ─────────────────────────────────

PHASE2_TEMPLATES = {
    "YIELD_EDUCATED": {
        31: lambda name, **_: _send(text=f"{name} Sir/Madam — revisiting the 6% structure at Block B. The tripartite agreement between you, GTB, and Elements is the mechanism that backs the contracted rental. Worth a second look if this has been on your mind.", video="V3", offset=7, phase=2),
        38: lambda name, **_: _send(text=f"{name} Sir/Madam — the 10-year investment comparison at ₹43L. Sharing again as this is the clearest side-by-side of contracted yield versus FD and market-linked options.", video="V1", offset=7, phase=2),
        45: lambda name, **_: _send(text=f"{name} Sir/Madam — the Kandigai corridor update. Metro construction is progressing. Capital appreciation is not contracted or guaranteed — but the infrastructure is moving on schedule.", video="V5", offset=7, phase=2),
        52: lambda name, **_: _send(text=f"{name} Sir/Madam — the real net yield on a Chennai flat after costs. If you're still weighing this against a regular rental property, this is the most relevant 60 seconds.", video="V2", offset=0, phase=2),
    },
    "YIELD_SHOPPER": {
        31: lambda name, **_: _send(text=f"{name} Sir/Madam — the 10-year investment math at ₹43L. FD, index funds, and contracted senior living yield side by side. Sharing again as this is the clearest framing of why 6% contracted is a different product from 6% projected.", video="V1", offset=7, phase=2),
        38: lambda name, **_: _send(text=f"{name} Sir/Madam — who backs the 6% promise at Block B and what the legal structure looks like. The tripartite agreement is what separates a brochure promise from a contractual one.", video="V3", offset=7, phase=2),
        45: lambda name, **_: _send(text=f"{name} Sir/Madam — real net yield on a regular Chennai rental flat after broker fees, vacancy, maintenance, tax: closer to 1.66%. Directly relevant if you're comparing against a self-managed property.", video="V2", offset=7, phase=2),
        52: lambda name, **_: _send(text=f"{name} Sir/Madam — the Kandigai corridor in context. Capital appreciation is not contracted or guaranteed — but the infrastructure story behind this location is real.", video="V5", offset=0, phase=2),
    },
    "CASHFLOW_ANCHORED": {
        31: lambda name, **_: _send(text=f"{name} Sir/Madam — the 10-year comparison at ₹43L. The ₹99/day net outflow framing is in here — worth a second watch if the monthly number has been a factor.", video="V1", offset=7, phase=2),
        38: lambda name, **_: _send(text=f"{name} Sir/Madam — real net yield on a regular Chennai flat. If you've been comparing Block B's net outflow against self-managed rental income, this 60-second video gives you the honest comparison.", video="V2", offset=7, phase=2),
        45: lambda name, **_: _send(text=f"{name} Sir/Madam — the legal structure behind the ₹21,500 monthly rental at Block B. This is what makes it contracted and not projected.", video="V3", offset=7, phase=2),
        52: lambda name, **_: _send(text=f"{name} Sir/Madam — six-point checklist for evaluating any senior living investment. Worth running through this for Block B and any other project you're considering.", video="V4", offset=0, phase=2),
    },
}


# ── Main lookup function ──────────────────────────────────────────────────────

def get_message(
    track: str,
    archetype: str,
    message_number: int,
    name: str = "Friend",
    intro_video: str = "V6",
    language: str = "EN",
    ab_variant_day3: str = None,
    ab_variant_day1: str = None,
    last_objection_type: str = None,
) -> dict:
    """
    Return a message payload for the given lead context.

    For STUCK track Day 1, if last_objection_type is provided,
    returns the objection-specific refframe message.

    Returns None if no template is defined for this combination.
    """
    kwargs = {
        "name":        name,
        "intro_video": intro_video,
        "language":    language,
    }

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    if message_number >= 31:
        phase2 = PHASE2_TEMPLATES.get(archetype, {})
        fn = phase2.get(message_number)
        return fn(**kwargs) if fn else None

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    if track == "COLD":
        track_map = COLD_TEMPLATES.get(archetype, COLD_TEMPLATES["YIELD_SHOPPER"])

    elif track == "STUCK":
        if message_number == 1:
            obj_key = last_objection_type or "default"
            fn = _STUCK_DAY1_BY_OBJECTION.get(obj_key, _STUCK_DAY1_BY_OBJECTION["default"])
            return fn(**kwargs)
        track_map = STUCK_TEMPLATES.get(archetype, STUCK_TEMPLATES["YIELD_SHOPPER"])

    elif track == "HOLD":
        track_map = HOLD_TEMPLATES.get(archetype, HOLD_TEMPLATES["YIELD_SHOPPER"])

    else:
        return None

    fn = track_map.get(message_number)
    if not fn:
        return None

    payload = fn(**kwargs)

    # Apply A/B variant overrides
    if message_number == 3 and ab_variant_day3 == "A" and archetype != "CASHFLOW_ANCHORED":
        # Override to cashflow framing for Variant A
        cashflow_fn = COLD_TEMPLATES["CASHFLOW_ANCHORED"].get(3)
        if cashflow_fn:
            payload = cashflow_fn(**kwargs)

    if message_number == 2 and ab_variant_day1 == "B":
        # Variant B: always use V7 regardless of UTM
        payload = _send(
            text=(
                f"A 60-second breakdown of the investment case for Block B — "
                f"the numbers, the structure, the why. Have a look, {name} Sir/Madam."
            ),
            video="V7",
            offset=0,
            ab_test="TEST_DAY1_VIDEO"
        )

    return payload


def get_next_message_number(track: str, current_msg: int, phase: int) -> int:
    """
    Given the current message number, return the next one.
    Returns 0 when the sequence is complete (no more messages).
    """
    if phase == 1:
        if track == "COLD":
            sequence = [1, 2, 3, 6, 9, 13, 17, 21, 25, 28]
        elif track == "STUCK":
            sequence = [1, 3, 6, 9, 13, 17, 21, 25, 28]
        elif track == "HOLD":
            sequence = [3, 7, 13, 17, 21, 25, 28]
        else:
            sequence = []
    else:
        sequence = [31, 38, 45, 52]

    try:
        idx = sequence.index(current_msg)
        return sequence[idx + 1] if idx + 1 < len(sequence) else 0
    except ValueError:
        # current_msg not in sequence — return first message after current
        for n in sequence:
            if n > current_msg:
                return n
        return 0


if __name__ == "__main__":
    # Quick smoke test
    tests = [
        ("COLD",  "YIELD_SHOPPER",     1,  "Ramesh"),
        ("COLD",  "CASHFLOW_ANCHORED", 3,  "Priya"),
        ("COLD",  "YIELD_EDUCATED",    25, "Sathish"),
        ("STUCK", "YIELD_SHOPPER",     1,  "Anand"),
        ("HOLD",  "YIELD_EDUCATED",    13, "Kavitha"),
        ("COLD",  "YIELD_SHOPPER",     38, "Kumar"),
    ]
    for track, arch, msg_num, name in tests:
        r = get_message(track, arch, msg_num, name=name)
        if r:
            action = r["action"]
            preview = (r["text"] or "")[:80] if r["text"] else f"→ {r['action_note']}"
            print(f"[{track}/{arch}/msg{msg_num}] {action}: {preview}")
        else:
            print(f"[{track}/{arch}/msg{msg_num}] None")
