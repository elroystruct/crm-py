"""
Contact/lead storage.

- If DATABASE_URL is set (e.g. Render's managed Postgres add-on), use Postgres.
  This survives redeploys, since Render's web service disk is ephemeral.
- Otherwise, fall back to a local SQLite file (data/contacts.db), fine for
  local development, but note this resets on every Render redeploy if you
  don't attach a persistent disk or Postgres.
"""
import os
import sqlite3
import json
import datetime as dt

DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "data", "contacts.db")

DEFAULT_RECORD = {"status": "new", "notes": "", "tags": [], "market": "", "statusUpdatedAt": ""}

_pg_pool = None


def _using_postgres():
    return bool(DATABASE_URL)


def _pg_conn():
    import psycopg2
    global _pg_pool
    # Render's DATABASE_URL sometimes uses postgres://, psycopg2 accepts it directly.
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def init_db():
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                phone TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'new',
                notes TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                market TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migration for tables created before status_updated_at existed.
        cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS status_updated_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
        cur.close()
        conn.close()
    else:
        os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                phone TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'new',
                notes TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                market TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migration for databases created before status_updated_at existed.
        # SQLite has no "ADD COLUMN IF NOT EXISTS", so check first.
        cols = [row[1] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()]
        if "status_updated_at" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN status_updated_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()


def get_contact(phone):
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT status, notes, tags, market, status_updated_at FROM contacts WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.execute("SELECT status, notes, tags, market, status_updated_at FROM contacts WHERE phone = ?", (phone,))
        row = cur.fetchone()
        conn.close()

    if not row:
        return dict(DEFAULT_RECORD)
    status, notes, tags, market, status_updated_at = row
    return {
        "status": status,
        "notes": notes,
        "tags": json.loads(tags) if tags else [],
        "market": market,
        "statusUpdatedAt": status_updated_at or "",
    }


def list_contacts():
    """Returns all contact records, used for lead-count and status-history rollups."""
    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT phone, status, notes, tags, market, status_updated_at FROM contacts")
        rows = cur.fetchall()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        rows = conn.execute("SELECT phone, status, notes, tags, market, status_updated_at FROM contacts").fetchall()
        conn.close()

    return [
        {
            "phone": phone,
            "status": status,
            "notes": notes,
            "tags": json.loads(tags) if tags else [],
            "market": market,
            "statusUpdatedAt": status_updated_at or "",
        }
        for phone, status, notes, tags, market, status_updated_at in rows
    ]


def save_contact(phone, status=None, notes=None, tags=None, market=None):
    existing = get_contact(phone)
    status_changed = status is not None and status != existing["status"]
    merged = {
        "status": status if status is not None else existing["status"],
        "notes": notes if notes is not None else existing["notes"],
        "tags": tags if tags is not None else existing["tags"],
        "market": market if market is not None else existing["market"],
        "statusUpdatedAt": _now_iso() if status_changed else (existing["statusUpdatedAt"] or _now_iso()),
    }
    tags_json = json.dumps(merged["tags"])

    if _using_postgres():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contacts (phone, status, notes, tags, market, status_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                status = EXCLUDED.status,
                notes = EXCLUDED.notes,
                tags = EXCLUDED.tags,
                market = EXCLUDED.market,
                status_updated_at = EXCLUDED.status_updated_at
        """, (phone, merged["status"], merged["notes"], tags_json, merged["market"], merged["statusUpdatedAt"]))
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("""
            INSERT INTO contacts (phone, status, notes, tags, market, status_updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (phone) DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes,
                tags = excluded.tags,
                market = excluded.market,
                status_updated_at = excluded.status_updated_at
        """, (phone, merged["status"], merged["notes"], tags_json, merged["market"], merged["statusUpdatedAt"]))
        conn.commit()
        conn.close()

    return merged
