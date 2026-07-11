"""
Contact/lead storage, plus campaigns (batches/lists) and the deal-economics
fields needed for CPL, revenue-per-text, list fatigue, and lead-to-offer
velocity reporting.

- If DATABASE_URL is set (e.g. Render's managed Postgres add-on), use Postgres.
  This survives redeploys, since Render's web service disk is ephemeral.
- Otherwise, fall back to a local SQLite file (data/contacts.db) — fine for
  local development, but note this resets on every Render redeploy if you
  don't attach a persistent disk or Postgres.
"""
import os
import sqlite3
import json
import datetime as dt

DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "data", "contacts.db")

# Sentinel meaning "caller didn't pass this field, leave it alone" — distinct
# from None, which callers use to explicitly clear a nullable field.
_UNSET = object()

DEFAULT_RECORD = {
    "status": "new",
    "notes": "",
    "tags": [],
    "market": "",
    "campaign_id": None,
    "revenue": 0.0,
    "positive_engagement_at": None,
    "offer_sent_at": None,
    "contract_at": None,
    "skip_bad": False,
    "updated_at": None,
    "name": None,
}


def _using_postgres():
    return bool(DATABASE_URL)


def _pg_conn():
    import psycopg2
    # Render's DATABASE_URL sometimes uses postgres:// — psycopg2 accepts it directly.
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _add_column_if_missing(conn_or_cur, table, col, coltype, using_pg):
    """Best-effort ALTER TABLE ADD COLUMN, ignoring 'already exists' errors.
    Lets us evolve the schema on existing databases without a migration tool."""
    try:
        if using_pg:
            conn_or_cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}")
        else:
            conn_or_cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    except Exception:
        pass


def init_db():
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT '',
                list_source TEXT NOT NULL DEFAULT '',
                cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                phone TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'new',
                notes TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                market TEXT NOT NULL DEFAULT ''
            )
        """)
        # migrate in new columns for dbs created before this feature set existed
        _add_column_if_missing(cur, "contacts", "campaign_id", "INTEGER", True)
        _add_column_if_missing(cur, "contacts", "revenue", "DOUBLE PRECISION NOT NULL DEFAULT 0", True)
        _add_column_if_missing(cur, "contacts", "positive_engagement_at", "TEXT", True)
        _add_column_if_missing(cur, "contacts", "offer_sent_at", "TEXT", True)
        _add_column_if_missing(cur, "contacts", "contract_at", "TEXT", True)
        _add_column_if_missing(cur, "contacts", "skip_bad", "BOOLEAN NOT NULL DEFAULT FALSE", True)
        _add_column_if_missing(cur, "contacts", "updated_at", "TEXT", True)
        _add_column_if_missing(cur, "contacts", "name", "TEXT", True)
        conn.commit()
        cur.close()
        conn.close()
    else:
        os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT '',
                list_source TEXT NOT NULL DEFAULT '',
                cost REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                phone TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'new',
                notes TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                market TEXT NOT NULL DEFAULT ''
            )
        """)
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "campaign_id" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "campaign_id", "INTEGER", False)
        if "revenue" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "revenue", "REAL NOT NULL DEFAULT 0", False)
        if "positive_engagement_at" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "positive_engagement_at", "TEXT", False)
        if "offer_sent_at" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "offer_sent_at", "TEXT", False)
        if "contract_at" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "contract_at", "TEXT", False)
        if "skip_bad" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "skip_bad", "INTEGER NOT NULL DEFAULT 0", False)
        if "updated_at" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "updated_at", "TEXT", False)
        if "name" not in existing_cols:
            _add_column_if_missing(conn, "contacts", "name", "TEXT", False)
        conn.commit()
        conn.close()


# ---------- campaigns ----------
def list_campaigns():
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, name, market, list_source, cost, created_at FROM campaigns ORDER BY created_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        rows = conn.execute(
            "SELECT id, name, market, list_source, cost, created_at FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    return [
        {"id": r[0], "name": r[1], "market": r[2], "listSource": r[3], "cost": r[4], "createdAt": r[5]}
        for r in rows
    ]


def get_campaign(campaign_id):
    for c in list_campaigns():
        if str(c["id"]) == str(campaign_id):
            return c
    return None


def create_campaign(name, market="", list_source="", cost=0.0):
    created_at = now_iso()
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO campaigns (name, market, list_source, cost, created_at) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, market, list_source, cost, created_at),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.execute(
            "INSERT INTO campaigns (name, market, list_source, cost, created_at) VALUES (?,?,?,?,?)",
            (name, market, list_source, cost, created_at),
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_campaign(new_id)


def update_campaign(campaign_id, name=None, market=None, list_source=None, cost=None):
    existing = get_campaign(campaign_id)
    if not existing:
        return None
    merged = {
        "name": name if name is not None else existing["name"],
        "market": market if market is not None else existing["market"],
        "list_source": list_source if list_source is not None else existing["listSource"],
        "cost": cost if cost is not None else existing["cost"],
    }
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE campaigns SET name=%s, market=%s, list_source=%s, cost=%s WHERE id=%s",
            (merged["name"], merged["market"], merged["list_source"], merged["cost"], campaign_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute(
            "UPDATE campaigns SET name=?, market=?, list_source=?, cost=? WHERE id=?",
            (merged["name"], merged["market"], merged["list_source"], merged["cost"], campaign_id),
        )
        conn.commit()
        conn.close()
    return get_campaign(campaign_id)


# ---------- contacts ----------
SELECT_COLS = "status, notes, tags, market, campaign_id, revenue, positive_engagement_at, offer_sent_at, contract_at, skip_bad, updated_at, name"


def _row_to_record(row):
    (status, notes, tags, market, campaign_id, revenue,
     positive_engagement_at, offer_sent_at, contract_at, skip_bad, updated_at, name) = row
    return {
        "status": status,
        "notes": notes,
        "tags": json.loads(tags) if tags else [],
        "market": market,
        "campaign_id": campaign_id,
        "revenue": revenue or 0.0,
        "positive_engagement_at": positive_engagement_at,
        "offer_sent_at": offer_sent_at,
        "contract_at": contract_at,
        "skip_bad": bool(skip_bad),
        "updated_at": updated_at,
        "name": name,
    }


def get_contact(phone):
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT {SELECT_COLS} FROM contacts WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.execute(f"SELECT {SELECT_COLS} FROM contacts WHERE phone = ?", (phone,))
        row = cur.fetchone()
        conn.close()

    if not row:
        return dict(DEFAULT_RECORD)
    return _row_to_record(row)


def list_contacts():
    """All contacts on file, keyed by phone. Used for dashboard aggregation
    (campaign attribution, revenue, velocity) independent of the SignalWire
    message pull."""
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT phone, {SELECT_COLS} FROM contacts")
        rows = cur.fetchall()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        rows = conn.execute(f"SELECT phone, {SELECT_COLS} FROM contacts").fetchall()
        conn.close()
    return {r[0]: _row_to_record(r[1:]) for r in rows}


def save_contact(phone, status=None, notes=None, tags=None, market=None,
                  campaign_id=_UNSET, revenue=None, positive_engagement_at=_UNSET,
                  offer_sent_at=_UNSET, contract_at=_UNSET, skip_bad=None, name=None):
    """Partial update. Plain fields (status/notes/tags/market/revenue/skip_bad/name)
    keep their old value when left as None. The nullable timestamp/campaign
    fields use the _UNSET sentinel so callers can explicitly clear them by
    passing None or ''.

    `name` never overwrites a manually-entered name with a blank: once set,
    it only changes if a caller passes a new non-empty value.
    """
    existing = get_contact(phone)

    def pick_clearable(new, old):
        if new is _UNSET:
            return old
        if new in ("", None):
            return None
        return new

    merged = {
        "status": status if status is not None else existing["status"],
        "notes": notes if notes is not None else existing["notes"],
        "tags": tags if tags is not None else existing["tags"],
        "market": market if market is not None else existing["market"],
        "campaign_id": pick_clearable(campaign_id, existing["campaign_id"]),
        "revenue": revenue if revenue is not None else existing["revenue"],
        "positive_engagement_at": pick_clearable(positive_engagement_at, existing["positive_engagement_at"]),
        "offer_sent_at": pick_clearable(offer_sent_at, existing["offer_sent_at"]),
        "contract_at": pick_clearable(contract_at, existing["contract_at"]),
        "skip_bad": skip_bad if skip_bad is not None else existing["skip_bad"],
        "name": name if name else existing["name"],
    }
    merged["updated_at"] = now_iso()
    tags_json = json.dumps(merged["tags"])
    campaign_id_val = merged["campaign_id"]
    skip_bad_val = bool(merged["skip_bad"])
    updated_at_val = merged["updated_at"]

    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contacts (phone, status, notes, tags, market, campaign_id, revenue,
                positive_engagement_at, offer_sent_at, contract_at, skip_bad, updated_at, name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (phone) DO UPDATE SET
                status = EXCLUDED.status,
                notes = EXCLUDED.notes,
                tags = EXCLUDED.tags,
                market = EXCLUDED.market,
                campaign_id = EXCLUDED.campaign_id,
                revenue = EXCLUDED.revenue,
                positive_engagement_at = EXCLUDED.positive_engagement_at,
                offer_sent_at = EXCLUDED.offer_sent_at,
                contract_at = EXCLUDED.contract_at,
                skip_bad = EXCLUDED.skip_bad,
                updated_at = EXCLUDED.updated_at,
                name = EXCLUDED.name
        """, (phone, merged["status"], merged["notes"], tags_json, merged["market"], campaign_id_val,
              merged["revenue"], merged["positive_engagement_at"], merged["offer_sent_at"],
              merged["contract_at"], skip_bad_val, updated_at_val, merged["name"]))
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("""
            INSERT INTO contacts (phone, status, notes, tags, market, campaign_id, revenue,
                positive_engagement_at, offer_sent_at, contract_at, skip_bad, updated_at, name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (phone) DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes,
                tags = excluded.tags,
                market = excluded.market,
                campaign_id = excluded.campaign_id,
                revenue = excluded.revenue,
                positive_engagement_at = excluded.positive_engagement_at,
                offer_sent_at = excluded.offer_sent_at,
                contract_at = excluded.contract_at,
                skip_bad = excluded.skip_bad,
                updated_at = excluded.updated_at,
                name = excluded.name
        """, (phone, merged["status"], merged["notes"], tags_json, merged["market"], campaign_id_val,
              merged["revenue"], merged["positive_engagement_at"], merged["offer_sent_at"],
              merged["contract_at"], int(skip_bad_val), updated_at_val, merged["name"]))
        conn.commit()
        conn.close()

    return merged
