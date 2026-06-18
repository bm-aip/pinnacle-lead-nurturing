"""
db/schema.py
Pinnacle Block B — Database Schema

Creates three tables in the poller's Postgres instance (idempotent):

  lead_state    — one row per lead; owns the sequence clock
  message_log   — every outbound message sent (text, video, doc)
  inbound_log   — every inbound reply with sentiment + routing decision

Run on startup via init() — safe to call on every deploy.
"""

import os
import logging
import urllib.parse
import pg8000.dbapi

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _parse_db_url(url: str) -> dict:
    """Parse a postgres:// or postgresql:// URL into pg8000.connect kwargs."""
    # Railway sometimes uses postgres:// prefix
    url = url.replace("postgres://", "postgresql://", 1)
    p = urllib.parse.urlparse(url)
    return {
        "host":     p.hostname,
        "port":     p.port or 5432,
        "database": p.path.lstrip("/"),
        "user":     p.username,
        "password": p.password,
        "ssl_context": True,   # Railway Postgres requires SSL
    }


def get_conn():
    """Return a pg8000 DBAPI connection."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL env var not set")
    kwargs = _parse_db_url(DATABASE_URL)
    return pg8000.dbapi.connect(**kwargs)


def _to_dict(cursor, row) -> dict:
    """Convert a pg8000 tuple row to a dict using cursor.description."""
    if row is None:
        return {}
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _fetchone_dict(cursor) -> dict:
    row = cursor.fetchone()
    return _to_dict(cursor, row) if row else {}


def init():
    """
    Create all tables if they don't exist.
    Idempotent — safe to call on every startup.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — skipping schema init")
        return

    try:
        conn = get_conn()
        cur  = conn.cursor()

        # ── lead_state ────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_state (
                lead_id              TEXT PRIMARY KEY,
                phone                TEXT NOT NULL,
                name                 TEXT,

                -- Qualifier output
                track                TEXT NOT NULL,         -- COLD / STUCK / HOLD
                archetype            TEXT NOT NULL,         -- YIELD_EDUCATED / YIELD_SHOPPER / CASHFLOW_ANCHORED
                intro_video          TEXT,                  -- V6 / V7
                language             TEXT DEFAULT 'EN',     -- EN / TA
                form_position        TEXT,                  -- top / bottom / sticky
                sell_do_stage        TEXT,
                lead_age_days        INTEGER DEFAULT 0,
                near_escalation      BOOLEAN DEFAULT FALSE,

                -- UTM signals
                utm_source           TEXT,
                utm_campaign         TEXT,
                utm_content          TEXT,

                -- A/B variants
                ab_variant_day3      TEXT,                  -- A / B
                ab_variant_day1      TEXT,                  -- A / B
                ab_variant_visit     TEXT,                  -- A / B

                -- Sequence clock
                entry_date           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                current_day          INTEGER DEFAULT 1,
                last_msg_number      INTEGER DEFAULT 0,
                last_msg_sent_at     TIMESTAMPTZ,
                next_msg_number      INTEGER DEFAULT 1,
                next_msg_due_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                -- Score
                score                INTEGER DEFAULT 10,
                score_band           TEXT DEFAULT 'COLD',

                -- Status
                -- active / scheduler / objection_handler / escalated /
                -- lost / visited / opted_out / post_visit / hold
                status               TEXT DEFAULT 'active',

                -- Site visit
                site_visit_booked    BOOLEAN DEFAULT FALSE,
                site_visit_date      TIMESTAMPTZ,
                site_visit_type      TEXT,                  -- in-person / virtual
                reschedule_count     INTEGER DEFAULT 0,
                site_visit_completed BOOLEAN DEFAULT FALSE,

                -- Referral
                referral_sent        BOOLEAN DEFAULT FALSE,

                -- Interest alert (fired once when lead first shows WARM/NEAR_CLOSE signal)
                interest_alert_sent_at TIMESTAMPTZ,

                -- Last inbound
                last_reply_text      TEXT,
                last_reply_at        TIMESTAMPTZ,
                last_sentiment       TEXT,

                -- Timestamps
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                updated_at           TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Index for the tick() query — leads due for next message
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lead_state_tick
            ON lead_state (next_msg_due_at, status)
            WHERE status = 'active'
        """)

        # Index for phone lookup (inbound handler)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lead_state_phone
            ON lead_state (phone)
        """)

        # ── message_log ───────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS message_log (
                id                   SERIAL PRIMARY KEY,
                lead_id              TEXT NOT NULL REFERENCES lead_state(lead_id),
                phone                TEXT NOT NULL,
                message_number       INTEGER NOT NULL,
                phase                INTEGER NOT NULL,       -- 1 or 2
                track                TEXT,
                archetype            TEXT,
                text_body            TEXT,
                video_tag            TEXT,                   -- V1-V7 or NULL
                video_url            TEXT,
                has_docs             BOOLEAN DEFAULT FALSE,
                ab_test_id           TEXT,
                ab_variant           TEXT,
                wasender_message_id  TEXT,
                delivery_status      TEXT DEFAULT 'sent',    -- sent / delivered / failed
                sent_at              TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_log_lead
            ON message_log (lead_id, sent_at DESC)
        """)

        # ── inbound_log ───────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS inbound_log (
                id                   SERIAL PRIMARY KEY,
                lead_id              TEXT NOT NULL,
                phone                TEXT NOT NULL,
                message_type         TEXT DEFAULT 'text',    -- text / audio / image / document / video
                reply_text           TEXT,
                audio_transcript     TEXT,
                sentiment            TEXT,
                routing_decision     TEXT,                   -- continue / accelerate / objection_handler / escalate / mark_lost
                score_before         INTEGER,
                score_after          INTEGER,
                score_delta          INTEGER,
                received_at          TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_inbound_log_lead
            ON inbound_log (lead_id, received_at DESC)
        """)

        conn.commit()
        cur.close()
        conn.close()
        log.info("DB schema initialised (lead_state, message_log, inbound_log)")

    except Exception as e:
        log.error(f"Schema init failed: {e}")
        raise


# ── Convenience helpers ───────────────────────────────────────────────────────

def upsert_lead_state(data: dict):
    """
    Insert or update a lead_state row.
    data must contain lead_id, phone, track, archetype at minimum.
    """
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO lead_state (
            lead_id, phone, name, track, archetype, intro_video, language,
            form_position, sell_do_stage, lead_age_days, near_escalation,
            utm_source, utm_campaign, utm_content,
            ab_variant_day3, ab_variant_day1, ab_variant_visit,
            entry_date, next_msg_due_at,
            score, score_band, status
        ) VALUES (
            %(lead_id)s, %(phone)s, %(name)s, %(track)s, %(archetype)s,
            %(intro_video)s, %(language)s, %(form_position)s, %(sell_do_stage)s,
            %(lead_age_days)s, %(near_escalation)s,
            %(utm_source)s, %(utm_campaign)s, %(utm_content)s,
            %(ab_variant_day3)s, %(ab_variant_day1)s, %(ab_variant_visit)s,
            NOW(), NOW(),
            10, 'COLD', 'active'
        )
        ON CONFLICT (lead_id) DO NOTHING
    """, {
        "lead_id":        data.get("lead_id"),
        "phone":          data.get("phone"),
        "name":           data.get("name", "Friend"),
        "track":          data.get("track"),
        "archetype":      data.get("archetype"),
        "intro_video":    data.get("intro_video"),
        "language":       data.get("language", "EN"),
        "form_position":  data.get("form_position"),
        "sell_do_stage":  data.get("sell_do_stage"),
        "lead_age_days":  data.get("lead_age_days", 0),
        "near_escalation":data.get("near_escalation", False),
        "utm_source":     data.get("utm_source"),
        "utm_campaign":   data.get("utm_campaign"),
        "utm_content":    data.get("utm_content"),
        "ab_variant_day3":data.get("ab_variant_day3"),
        "ab_variant_day1":data.get("ab_variant_day1"),
        "ab_variant_visit":data.get("ab_variant_visit"),
    })
    conn.commit()
    cur.close()
    conn.close()


def get_lead_state(lead_id: str) -> dict:
    """Fetch a single lead_state row by lead_id."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM lead_state WHERE lead_id = %s", (lead_id,))
    result = _fetchone_dict(cur)
    cur.close()
    conn.close()
    return result


def get_lead_by_phone(phone: str) -> dict:
    """Fetch lead_state by phone (10-digit or 91-prefixed)."""
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT * FROM lead_state
        WHERE phone = %s OR phone = %s
        LIMIT 1
    """, (digits, "91" + digits))
    result = _fetchone_dict(cur)
    cur.close()
    conn.close()
    return result


def update_lead_state(lead_id: str, updates: dict):
    """
    Patch specific fields on a lead_state row.
    updates: dict of column→value pairs.
    """
    if not updates:
        return
    updates["updated_at"] = "NOW()"
    set_clause = ", ".join(
        f"{col} = NOW()" if val == "NOW()" else f"{col} = %({col})s"
        for col, val in updates.items()
    )
    params = {k: v for k, v in updates.items() if v != "NOW()"}
    params["lead_id"] = lead_id

    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(f"""
        UPDATE lead_state SET {set_clause}
        WHERE lead_id = %(lead_id)s
    """, params)
    conn.commit()
    cur.close()
    conn.close()


def log_message(
    lead_id: str,
    phone: str,
    message_number: int,
    phase: int,
    track: str,
    archetype: str,
    text_body: str,
    video_tag: str = None,
    video_url: str = None,
    has_docs: bool = False,
    ab_test_id: str = None,
    ab_variant: str = None,
    wasender_message_id: str = None,
):
    """Append a row to message_log."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO message_log (
            lead_id, phone, message_number, phase, track, archetype,
            text_body, video_tag, video_url, has_docs,
            ab_test_id, ab_variant, wasender_message_id
        ) VALUES (
            %(lead_id)s, %(phone)s, %(msg_num)s, %(phase)s, %(track)s, %(archetype)s,
            %(text_body)s, %(video_tag)s, %(video_url)s, %(has_docs)s,
            %(ab_test_id)s, %(ab_variant)s, %(wasender_id)s
        )
    """, {
        "lead_id":     lead_id,
        "phone":       phone,
        "msg_num":     message_number,
        "phase":       phase,
        "track":       track,
        "archetype":   archetype,
        "text_body":   text_body,
        "video_tag":   video_tag,
        "video_url":   video_url,
        "has_docs":    has_docs,
        "ab_test_id":  ab_test_id,
        "ab_variant":  ab_variant,
        "wasender_id": wasender_message_id,
    })
    conn.commit()
    cur.close()
    conn.close()


def log_inbound(
    lead_id: str,
    phone: str,
    message_type: str,
    reply_text: str,
    sentiment: str,
    routing_decision: str,
    score_before: int,
    score_after: int,
    audio_transcript: str = None,
):
    """Append a row to inbound_log."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO inbound_log (
            lead_id, phone, message_type, reply_text, audio_transcript,
            sentiment, routing_decision, score_before, score_after, score_delta
        ) VALUES (
            %(lead_id)s, %(phone)s, %(msg_type)s, %(reply)s, %(transcript)s,
            %(sentiment)s, %(routing)s, %(before)s, %(after)s, %(delta)s
        )
    """, {
        "lead_id":    lead_id,
        "phone":      phone,
        "msg_type":   message_type,
        "reply":      reply_text,
        "transcript": audio_transcript,
        "sentiment":  sentiment,
        "routing":    routing_decision,
        "before":     score_before,
        "after":      score_after,
        "delta":      (score_after or 0) - (score_before or 0),
    })
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init()
    print("Schema OK")
