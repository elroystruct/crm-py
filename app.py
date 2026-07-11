import os
import re
import base64
import json
import bisect
import secrets
import datetime as dt
from email.utils import parsedate_to_datetime
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from dotenv import load_dotenv

import db

STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out", "remove"}

# The PIN Elroy hands out to anyone he wants to be able to create an account.
# Not a secret meant to be rotated dynamically -- it's a one-time gate on
# signup, checked alongside email/password.
SIGNUP_PIN = "6146"

load_dotenv()

app = Flask(__name__, static_folder="public", static_url_path="")
# SECRET_KEY should be set in the environment for production so sessions
# survive a restart/redeploy. Falls back to a random key (sessions reset on
# every restart) so local dev doesn't need any extra setup.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

db.init_db()


@app.errorhandler(Exception)
def handle_uncaught_error(e):
    """Without this, an unhandled exception falls through to Flask's default
    HTML error page. The frontend does response.json() on every fetch, so an
    HTML page shows up client-side as a cryptic 'Unexpected token <' instead
    of the actual problem. Always return JSON so the real error is visible."""
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"Server error: {e}"}), 500


# ---------- auth gate ----------
# Every /api/* route requires a signed-in user except the auth routes
# themselves. Login happens before the SignalWire connect step, not instead
# of it -- signing in gets you into the app shell, connecting SignalWire is
# a separate step after that.
_PUBLIC_API_PATHS = {
    "/api/auth/signup",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
}


@app.before_request
def require_login():
    path = request.path
    if not path.startswith("/api/"):
        return None
    if path in _PUBLIC_API_PATHS:
        return None
    if not session.get("user_id"):
        return jsonify({"error": "Not signed in."}), 401
    return None


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.get_user_by_id(uid)


def public_user(u):
    if not u:
        return None
    return {"id": u["id"], "email": u["email"], "name": u["name"], "avatar": u["avatar"]}


# ---------- credential state (in-memory only, per process) ----------
creds = {
    "space": os.environ.get("SIGNALWIRE_SPACE", ""),
    "projectId": os.environ.get("SIGNALWIRE_PROJECT_ID", ""),
    "authToken": os.environ.get("SIGNALWIRE_AUTH_TOKEN", ""),
    "fromNumber": os.environ.get("SIGNALWIRE_FROM_NUMBER", ""),
}


def creds_ready():
    return bool(creds["space"] and creds["projectId"] and creds["authToken"])


# ---------- auth routes ----------
def _valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    pin = (body.get("pin") or "").strip()

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are all required."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    if pin != SIGNUP_PIN:
        return jsonify({"error": "That sign-up PIN isn't right. Request one from Elroy."}), 403
    if db.get_user_by_email(email):
        return jsonify({"error": "An account with that email already exists. Sign in instead."}), 409

    user = db.create_user(email=email, password_hash=generate_password_hash(password), name=name)
    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)}), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    user = db.get_user_by_email(email)
    if not user or not check_password_hash(user["passwordHash"], password):
        return jsonify({"error": "Incorrect email or password."}), 401

    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"loggedOut": True})


@app.route("/api/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return jsonify({"user": None}), 200
    return jsonify({"user": public_user(user)})


@app.route("/api/auth/profile", methods=["PUT"])
def auth_update_profile():
    user = current_user()
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name")
    avatar = db._UNSET if "avatar" not in body else body.get("avatar")
    # A data-URL image is plenty for a small profile picture; cap it so a huge
    # upload can't bloat the users table.
    if avatar not in (db._UNSET, None) and len(avatar) > 700_000:
        return jsonify({"error": "Image is too large. Try a smaller picture."}), 400
    updated = db.update_user(user["id"], name=(name.strip() if name else None), avatar=avatar)
    return jsonify({"user": public_user(updated)})


@app.route("/api/auth/account", methods=["DELETE"])
def auth_delete_account():
    user = current_user()
    db.delete_user(user["id"])
    session.clear()
    return jsonify({"deleted": True})




def base_url():
    space = creds["space"].strip().replace("https://", "").replace("http://", "").rstrip("/")
    host = space if space.endswith(".signalwire.com") else f"{space}.signalwire.com"
    return f"https://{host}/api/laml/2010-04-01/Accounts/{creds['projectId'].strip()}"


def auth_header():
    token = base64.b64encode(f"{creds['projectId'].strip()}:{creds['authToken'].strip()}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---------- static frontend ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- connection ----------
@app.route("/api/status")
def status():
    return jsonify({
        "connected": creds_ready(),
        "space": creds["space"] or None,
        "fromNumber": creds["fromNumber"] or None,
    })


@app.route("/api/connect", methods=["POST"])
def connect():
    body = request.get_json(force=True, silent=True) or {}
    space, project_id, auth_token = body.get("space"), body.get("projectId"), body.get("authToken")
    from_number = body.get("fromNumber", "")
    if not (space and project_id and auth_token):
        return jsonify({"error": "space, projectId, and authToken are all required."}), 400
    creds["space"], creds["projectId"], creds["authToken"] = space, project_id, auth_token
    creds["fromNumber"] = from_number
    return jsonify({"connected": True, "space": creds["space"], "fromNumber": creds["fromNumber"] or None})


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    creds["space"] = creds["projectId"] = creds["authToken"] = creds["fromNumber"] = ""
    return jsonify({"connected": False})


# ---------- shared message pull ----------
def _fetch_all_messages(max_pages=20, page_size=100):
    """Pulls the full paginated message log from SignalWire.

    Returns (messages, None) on success, or (None, (json_body, status_code)) on failure,
    so callers can `return err` directly from a Flask route.
    """
    all_messages = []
    next_url = f"{base_url()}/Messages.json?PageSize={page_size}"
    pages_fetched = 0

    while next_url and pages_fetched < max_pages:
        try:
            r = requests.get(next_url, headers=auth_header(), timeout=20)
        except requests.RequestException as e:
            return None, (jsonify({"error": f"Could not reach SignalWire: {e}"}), 502)

        if not r.ok:
            return None, (jsonify({
                "error": f"SignalWire API error ({r.status_code}) calling {next_url}: {r.text[:400]}"
            }), r.status_code)

        try:
            payload = r.json()
        except ValueError:
            # Response was 2xx but not JSON, surface exactly what came back so it's debuggable,
            # instead of a bare "Expecting value" parser error.
            return None, (jsonify({
                "error": (
                    f"SignalWire returned a non-JSON response (status {r.status_code}) for {next_url}. "
                    f"This usually means the Space name or Project ID is malformed. "
                    f"Raw response start: {r.text[:200]!r}"
                )
            }), 502)

        all_messages.extend(payload.get("messages", []))
        pages_fetched += 1

        next_page_uri = payload.get("next_page_uri")
        if next_page_uri:
            host = base_url().split("/api/laml/2010-04-01/Accounts/")[0]
            next_url = f"{host}{next_page_uri}"
        else:
            next_url = None

    return all_messages, None


def _parse_ts(raw):
    """Parses SignalWire's RFC2822-style date_sent/date_created strings into aware UTC datetimes."""
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        except Exception:
            return None


# ---------- messages ----------
@app.route("/api/messages")
def messages():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    inbound = [m for m in all_messages if (m.get("direction") or "").startswith("inbound")]

    # Seller first name isn't stored anywhere; pull it out of the "Hey, {firstName}
    # I'm Elroy..." opener wherever it shows up in the log (normally an outbound
    # message we sent, but scan every message regardless of direction so a
    # forwarded/relogged/test copy of that text still gets picked up).
    name_by_phone = {}
    for m in all_messages:
        is_inbound = (m.get("direction") or "").startswith("inbound")
        lead_number = m.get("from") if is_inbound else m.get("to")
        phone_key = _norm_phone(lead_number)
        if not phone_key or phone_key in name_by_phone:
            continue
        name = _extract_first_name(m.get("body"))
        if name:
            name_by_phone[phone_key] = name

    enriched = []
    skipped = 0
    contacts_cache = db.list_contacts()  # one query instead of one per message
    for m in inbound:
        try:
            phone = m.get("from")
            crm = contacts_cache.get(phone) or dict(db.DEFAULT_RECORD)
            if _is_stop_message(m.get("body")):
                # STOP/END-type reply -> disqualify the lead. Still shown if the
                # "dead" status filter checkbox is checked in the UI.
                if crm["status"] != "dead" or not crm["skip_bad"]:
                    crm = db.save_contact(phone, status="dead", skip_bad=True)
                    contacts_cache[phone] = crm
            pulled_name = name_by_phone.get(_norm_phone(phone))
            if pulled_name and not crm.get("name"):
                crm = db.save_contact(phone, name=pulled_name)
                contacts_cache[phone] = crm
            raw_date = m.get("date_sent") or m.get("date_created")
            enriched.append({
                "sid": m.get("sid"),
                "from": phone,
                "to": m.get("to"),
                "name": crm.get("name") or pulled_name,
                "body": m.get("body"),
                "dateSent": raw_date,
                "_sortTs": _parse_ts(raw_date),
                "numMedia": int(m.get("num_media") or 0),
                "status": crm["status"],
                "notes": crm["notes"],
                "tags": crm["tags"],
                "market": crm["market"],
            })
        except Exception as e:
            # One malformed record (odd MMS shape, missing field, etc.) should never take
            # down the whole inbox. Skip it and keep going instead of 500ing the entire list.
            skipped += 1
            app.logger.warning(f"Skipped malformed message {m.get('sid')}: {e}")
            continue

    # Sort by the parsed datetime, not the raw string. SignalWire's RFC2822-style dates
    # ("Thu, 9 Jul 2026 22:07:30 +0000") don't sort correctly as plain text, since month
    # names aren't in calendar order and single-digit days get inconsistent spacing.
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    enriched.sort(key=lambda m: m["_sortTs"] or epoch, reverse=True)
    for m in enriched:
        del m["_sortTs"]

    return jsonify({"count": len(enriched), "messages": enriched, "skipped": skipped})


# ---------- single-lead conversation (chat view) ----------
@app.route("/api/conversation/<path:phone>")
def conversation(phone):
    """Full two-way message history with one lead, sorted oldest -> newest,
    for the chat-style lead detail view."""
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    target = _norm_phone(phone)
    thread = []
    for m in all_messages:
        direction = m.get("direction") or ""
        is_inbound = direction.startswith("inbound")
        other_party = m.get("from") if is_inbound else m.get("to")
        if _norm_phone(other_party) != target:
            continue
        raw_date = m.get("date_sent") or m.get("date_created")
        thread.append({
            "sid": m.get("sid"),
            "direction": "inbound" if is_inbound else "outbound",
            "from": m.get("from"),
            "to": m.get("to"),
            "body": m.get("body"),
            "status": (m.get("status") or "").lower(),
            "errorCode": m.get("error_code"),
            "dateSent": raw_date,
            "numMedia": int(m.get("num_media") or 0),
            "_sortTs": _parse_ts(raw_date),
        })

    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    thread.sort(key=lambda m: m["_sortTs"] or epoch)
    for m in thread:
        del m["_sortTs"]

    return jsonify({"phone": phone, "count": len(thread), "messages": thread})


# ---------- analytics ----------
@app.route("/api/analytics")
def analytics():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    inbound = []
    outbound = []
    for m in all_messages:
        ts = _parse_ts(m.get("date_sent") or m.get("date_created"))
        if ts is None:
            continue
        direction = m.get("direction") or ""
        if direction.startswith("inbound"):
            inbound.append({"phone": m.get("from"), "ts": ts})
        else:
            outbound.append({"phone": m.get("to"), "ts": ts})

    outbound_by_phone = {}
    for o in outbound:
        outbound_by_phone.setdefault(o["phone"], []).append(o["ts"])
    for k in outbound_by_phone:
        outbound_by_phone[k].sort()

    inbound_by_phone = {}
    for i in inbound:
        inbound_by_phone.setdefault(i["phone"], []).append(i["ts"])
    for k in inbound_by_phone:
        inbound_by_phone[k].sort()

    hourly = [{"hour": h, "inbound": 0, "responded": 0} for h in range(24)]
    daily = {}
    response_seconds = []
    responded_count = 0

    for msg in inbound:
        phone, ts = msg["phone"], msg["ts"]
        hourly[ts.hour]["inbound"] += 1

        date_key = ts.date().isoformat()
        daily.setdefault(date_key, {"inbound": 0, "outbound": 0})
        daily[date_key]["inbound"] += 1

        # Bound the "did this get a reply" window at the next inbound message from the
        # same number (so a reply to a later text doesn't get credited to an earlier one),
        # capped at 48 hours out if there's no next inbound message.
        same_phone_inbound = inbound_by_phone.get(phone, [])
        idx = bisect.bisect_right(same_phone_inbound, ts)
        window_end = same_phone_inbound[idx] if idx < len(same_phone_inbound) else ts + dt.timedelta(hours=48)

        candidates = outbound_by_phone.get(phone, [])
        pos = bisect.bisect_right(candidates, ts)
        if pos < len(candidates) and candidates[pos] <= window_end:
            delta = (candidates[pos] - ts).total_seconds()
            if delta >= 0:
                response_seconds.append(delta)
                responded_count += 1
                hourly[ts.hour]["responded"] += 1

    for msg in outbound:
        date_key = msg["ts"].date().isoformat()
        daily.setdefault(date_key, {"inbound": 0, "outbound": 0})
        daily[date_key]["outbound"] += 1

    total_inbound = len(inbound)
    total_outbound = len(outbound)
    response_rate = round((responded_count / total_inbound * 100), 1) if total_inbound else 0.0
    avg_response = (sum(response_seconds) / len(response_seconds)) if response_seconds else None

    median_response = None
    if response_seconds:
        s = sorted(response_seconds)
        n = len(s)
        median_response = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    hourly_out = []
    for h in hourly:
        rate = round((h["responded"] / h["inbound"] * 100), 1) if h["inbound"] else 0.0
        hourly_out.append({"hour": h["hour"], "inbound": h["inbound"], "responded": h["responded"], "rate": rate})

    eligible = [h for h in hourly_out if h["inbound"] >= 2]
    pool = eligible if eligible else [h for h in hourly_out if h["inbound"] > 0]
    best_hour = max(pool, key=lambda h: h["rate"]) if pool else None

    bucket_defs = [
        ("Under 5 min", 0, 300),
        ("5 to 30 min", 300, 1800),
        ("30 min to 2 hr", 1800, 7200),
        ("2 to 24 hr", 7200, 86400),
        ("Over 24 hr", 86400, float("inf")),
    ]
    response_buckets = [
        {"label": label, "count": sum(1 for s in response_seconds if lo <= s < hi)}
        for label, lo, hi in bucket_defs
    ]
    response_buckets.append({"label": "No reply yet", "count": total_inbound - responded_count})

    daily_sorted = sorted(daily.items())[-30:]
    daily_out = [{"date": d, "inbound": v["inbound"], "outbound": v["outbound"]} for d, v in daily_sorted]

    return jsonify({
        "totalInbound": total_inbound,
        "totalOutbound": total_outbound,
        "respondedCount": responded_count,
        "responseRate": response_rate,
        "avgResponseSeconds": avg_response,
        "medianResponseSeconds": median_response,
        "hourly": hourly_out,
        "bestHour": best_hour,
        "responseBuckets": response_buckets,
        "dailyVolume": daily_out,
    })


# ---------- send a message ----------
@app.route("/api/send", methods=["POST"])
def send_message():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire credentials first."}), 400

    body = request.get_json(force=True, silent=True) or {}
    to_number = (body.get("to") or "").strip()
    message_body = (body.get("body") or "").strip()
    from_number = (body.get("from") or creds["fromNumber"] or "").strip()

    if not to_number or not message_body:
        return jsonify({"error": "Both 'to' and 'body' are required."}), 400
    if not from_number:
        return jsonify({"error": "No 'From' number set. Add one on the connect screen or pass it explicitly."}), 400

    try:
        r = requests.post(
            f"{base_url()}/Messages.json",
            headers=auth_header(),
            data={"To": to_number, "From": from_number, "Body": message_body},
            timeout=20,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach SignalWire: {e}"}), 502

    if not r.ok:
        return jsonify({"error": f"SignalWire API error ({r.status_code}): {r.text[:400]}"}), r.status_code

    try:
        payload = r.json()
    except ValueError:
        return jsonify({"error": f"Non-JSON response sending message: {r.text[:200]!r}"}), 502

    return jsonify({
        "sid": payload.get("sid"),
        "status": payload.get("status"),
        "to": payload.get("to"),
        "from": payload.get("from"),
        "body": payload.get("body"),
        "dateSent": payload.get("date_sent") or payload.get("date_created"),
    })


# ---------- contacts / leads ----------
_MISSING = object()  # distinguishes "key not in JSON body" from "key explicitly set to null"


def _from_body(body, key):
    return body[key] if key in body else _MISSING


@app.route("/api/contacts/<path:phone>", methods=["GET"])
def get_contact_route(phone):
    return jsonify(db.get_contact(phone))


@app.route("/api/contacts/<path:phone>", methods=["PUT"])
def put_contact_route(phone):
    body = request.get_json(force=True, silent=True) or {}

    kwargs = dict(
        status=body.get("status"),
        notes=body.get("notes"),
        tags=body.get("tags"),
        market=body.get("market"),
        revenue=body.get("revenue"),
        skip_bad=body.get("skipBad"),
    )
    # Only pass the clearable fields through if the client actually sent them,
    # so a PUT that only touches "notes" doesn't accidentally wipe a timestamp.
    for json_key, kw in [
        ("campaignId", "campaign_id"),
        ("positiveEngagementAt", "positive_engagement_at"),
        ("offerSentAt", "offer_sent_at"),
        ("contractAt", "contract_at"),
    ]:
        val = _from_body(body, json_key)
        if val is not _MISSING:
            kwargs[kw] = val

    record = db.save_contact(phone, **kwargs)
    return jsonify(record)


# ---------- campaigns ----------
@app.route("/api/campaigns", methods=["GET"])
def list_campaigns_route():
    return jsonify({"campaigns": db.list_campaigns()})


@app.route("/api/campaigns", methods=["POST"])
def create_campaign_route():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Campaign name is required."}), 400
    try:
        cost = float(body.get("cost") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Cost must be a number."}), 400
    campaign = db.create_campaign(
        name=name,
        market=(body.get("market") or "").strip(),
        list_source=(body.get("listSource") or "").strip(),
        cost=cost,
    )
    return jsonify(campaign), 201


@app.route("/api/campaigns/<int:campaign_id>", methods=["PUT"])
def update_campaign_route(campaign_id):
    body = request.get_json(force=True, silent=True) or {}
    cost = body.get("cost")
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return jsonify({"error": "Cost must be a number."}), 400
    campaign = db.update_campaign(
        campaign_id,
        name=body.get("name"),
        market=body.get("market"),
        list_source=body.get("listSource"),
        cost=cost,
    )
    if not campaign:
        return jsonify({"error": "Campaign not found."}), 404
    return jsonify(campaign)


# ---------- settings (SignalWire per-message SMS cost rates) ----------
@app.route("/api/settings", methods=["GET"])
def get_settings_route():
    return jsonify(db.get_settings())


@app.route("/api/settings", methods=["PUT"])
def put_settings_route():
    body = request.get_json(force=True, silent=True) or {}
    try:
        outbound = float(body["smsCostOutbound"]) if "smsCostOutbound" in body else None
        inbound = float(body["smsCostInbound"]) if "smsCostInbound" in body else None
    except (TypeError, ValueError):
        return jsonify({"error": "SMS costs must be numbers."}), 400
    return jsonify(db.save_settings(sms_cost_outbound=outbound, sms_cost_inbound=inbound))


# ---------- opt-outs (STOP replies) ----------
@app.route("/api/opt-outs")
def opt_outs():
    """Numbers that have ever sent a STOP-type reply, pulled straight from the
    SignalWire log (source of truth) rather than the local 'dead' status,
    since a lead can be marked dead for reasons other than opting out."""
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    seen = {}
    for m in all_messages:
        if not (m.get("direction") or "").startswith("inbound"):
            continue
        if not _is_stop_message(m.get("body")):
            continue
        phone = m.get("from")
        key = _norm_phone(phone)
        if not key:
            continue
        ts = _parse_ts(m.get("date_sent") or m.get("date_created"))
        if key not in seen or (ts and (seen[key][1] is None or ts > seen[key][1])):
            seen[key] = (phone, ts)

    phones = sorted(seen.values(), key=lambda x: (x[1] is None, x[1]), reverse=True)
    return jsonify({
        "count": len(phones),
        "phones": [{"phone": p, "dateSent": ts.isoformat() if ts else None} for p, ts in phones],
    })


@app.route("/api/opt-outs/export")
def opt_outs_export():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    seen = {}
    for m in all_messages:
        if not (m.get("direction") or "").startswith("inbound"):
            continue
        if not _is_stop_message(m.get("body")):
            continue
        phone = m.get("from")
        key = _norm_phone(phone)
        if key:
            seen[key] = phone

    lines = sorted(seen.values())
    body = "\n".join(lines) + ("\n" if lines else "")
    filename = f"opt_outs_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
    return app.response_class(
        body,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------- dashboard (daily / weekly / monthly / all-time) ----------
def _parse_iso(raw):
    if not raw:
        return None
    try:
        d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _day_bounds(now):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def _week_bounds(now, weeks_ago=0):
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today_start - dt.timedelta(days=today_start.weekday())
    start = monday - dt.timedelta(weeks=weeks_ago)
    return start, start + dt.timedelta(days=7)


def _month_bounds(now):
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _is_stop_message(body):
    text = (body or "").strip().lower().strip(".!")
    return text in STOP_KEYWORDS


# Matches the opener of our outbound cold-text template:
#   "Hey, " + firstName + " I'm Elroy with Zocalo, ..."
# Anchor = "Hey, " up to the next space; that's the name.
# Matches the opener of our outbound cold-text template:
#   "Hey, " + firstName + " I'm Elroy with Zocalo, ..."
# Anchors: starts right after "Hey," and ends right before " I'm" (handles curly apostrophe too).
_NAME_RE = re.compile(r"Hey,\s*([A-Za-z'\-]+)\s+I['\u2019]?m\b", re.IGNORECASE)


def _extract_first_name(body):
    m = _NAME_RE.search(body or "")
    return m.group(1) if m else None


def _norm_phone(phone):
    """Digits-only so '+1 (555) 123-4567' and '15551234567' compare equal."""
    return re.sub(r"\D", "", phone or "")


# GSM-7 charset covers standard SMS characters; anything outside it forces
# UCS-2 encoding, which halves the per-segment character budget. This is an
# approximation of SignalWire/carrier segmentation, close enough for cost
# estimates without needing a full GSM-7 table.
_GSM7_RE = re.compile(
    r"^[@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,\-./0-9:;<=>?"
    r"A-Z¡ÄÖÑÜ§¿a-zäöñüà\^{}\\\[~\]|€]*$"
)


def _sms_segments(body):
    text = body or ""
    length = len(text)
    if length == 0:
        return 1
    is_gsm7 = bool(_GSM7_RE.match(text))
    single_limit, multi_limit = (160, 153) if is_gsm7 else (70, 67)
    if length <= single_limit:
        return 1
    return -(-length // multi_limit)  # ceil division


@app.route("/api/dashboard")
def dashboard():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    contacts = db.list_contacts()
    campaigns = {c["id"]: c for c in db.list_campaigns()}
    sms_rates = db.get_settings()
    now = dt.datetime.now(dt.timezone.utc)

    # ---- normalize the SignalWire log ----
    inbound, outbound = [], []
    for m in all_messages:
        ts = _parse_ts(m.get("date_sent") or m.get("date_created"))
        if ts is None:
            continue
        direction = m.get("direction") or ""
        row = {
            "phone": m.get("from") if direction.startswith("inbound") else m.get("to"),
            "ts": ts,
            "body": m.get("body"),
            "status": (m.get("status") or "").lower(),
            "errorCode": m.get("error_code"),
            "segments": _sms_segments(m.get("body")),
        }
        (inbound if direction.startswith("inbound") else outbound).append(row)

    def messaging_cost(rows, rate, w_start=None, w_end=None):
        """Real SignalWire 10DLC messaging spend: segments x per-segment rate,
        optionally windowed to a date range."""
        return round(sum(
            r["segments"] * rate for r in rows if not w_start or w_start <= r["ts"] < w_end
        ), 2)


    outbound_by_phone = {}
    for o in outbound:
        outbound_by_phone.setdefault(o["phone"], []).append(o["ts"])
    for k in outbound_by_phone:
        outbound_by_phone[k].sort()

    inbound_by_phone = {}
    for i in inbound:
        inbound_by_phone.setdefault(i["phone"], []).append(i["ts"])
    for k in inbound_by_phone:
        inbound_by_phone[k].sort()

    def responded(phone, ts):
        """Same "did this inbound get a reply" logic as /api/analytics."""
        same_phone_inbound = inbound_by_phone.get(phone, [])
        idx = bisect.bisect_right(same_phone_inbound, ts)
        window_end = same_phone_inbound[idx] if idx < len(same_phone_inbound) else ts + dt.timedelta(hours=48)
        candidates = outbound_by_phone.get(phone, [])
        pos = bisect.bisect_right(candidates, ts)
        return pos < len(candidates) and candidates[pos] <= window_end

    # ================= DAILY =================
    day_start, day_end = _day_bounds(now)
    today_all = [m for m in (inbound + outbound) if day_start <= m["ts"] < day_end]
    today_inbound = [m for m in inbound if day_start <= m["ts"] < day_end]

    error_counts = {}
    for m in today_all:
        code = m.get("errorCode")
        if code:
            error_counts[str(code)] = error_counts.get(str(code), 0) + 1
    carrier_errors = sorted(
        [{"code": k, "count": v} for k, v in error_counts.items()], key=lambda x: -x["count"]
    )

    opt_out_count = sum(1 for m in today_inbound if _is_stop_message(m["body"]))
    opt_out_ratio = round((opt_out_count / len(today_inbound) * 100), 1) if today_inbound else 0.0

    def lead_to_offer_velocity(window_start=None, window_end=None):
        hours = []
        for phone, c in contacts.items():
            pe, os_ = _parse_iso(c["positive_engagement_at"]), _parse_iso(c["offer_sent_at"])
            if not pe or not os_ or os_ < pe:
                continue
            if window_start and not (window_start <= os_ < window_end):
                continue
            hours.append((os_ - pe).total_seconds() / 3600.0)
        if not hours:
            return {"avgHours": None, "medianHours": None, "count": 0}
        s = sorted(hours)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {"avgHours": round(sum(hours) / n, 1), "medianHours": round(median, 1), "count": n}

    daily = {
        "carrierErrors": carrier_errors,
        "totalMessagesToday": len(today_all),
        "optOutRatio": opt_out_ratio,
        "optOutCount": opt_out_count,
        "totalInboundToday": len(today_inbound),
        "leadToOfferVelocity": lead_to_offer_velocity(day_start, day_end),
        "leadToOfferVelocityAllTime": lead_to_offer_velocity(),
    }

    # ================= WEEKLY =================
    phone_campaign = {phone: c.get("campaign_id") for phone, c in contacts.items()}

    def campaign_response_rate(campaign_id, w_start, w_end):
        phones = {p for p, cid in phone_campaign.items() if cid == campaign_id}
        window_inbound = [m for m in inbound if m["phone"] in phones and w_start <= m["ts"] < w_end]
        if not window_inbound:
            return None, 0
        got_reply = sum(1 for m in window_inbound if responded(m["phone"], m["ts"]))
        return round(got_reply / len(window_inbound) * 100, 1), len(window_inbound)

    this_week_start, this_week_end = _week_bounds(now, 0)
    last_week_start, last_week_end = _week_bounds(now, 1)

    list_fatigue = []
    for cid, camp in campaigns.items():
        this_rate, this_n = campaign_response_rate(cid, this_week_start, this_week_end)
        last_rate, last_n = campaign_response_rate(cid, last_week_start, last_week_end)
        if this_rate is None and last_rate is None:
            continue
        delta = None
        fatigued = False
        if this_rate is not None and last_rate is not None:
            delta = round(this_rate - last_rate, 1)
            fatigued = delta <= -15 and last_n >= 3
        list_fatigue.append({
            "campaignId": cid,
            "name": camp["name"],
            "market": camp["market"],
            "thisWeekRate": this_rate,
            "thisWeekInbound": this_n,
            "lastWeekRate": last_rate,
            "lastWeekInbound": last_n,
            "delta": delta,
            "fatigued": fatigued,
        })
    list_fatigue.sort(key=lambda x: (x["delta"] is None, x["delta"] if x["delta"] is not None else 0))

    weekly = {"listFatigue": list_fatigue}

    # ================= MONTHLY / ALL-TIME =================
    month_start, month_end = _month_bounds(now)

    def skip_trace_accuracy(w_start=None, w_end=None):
        determined = [c for c in contacts.values() if c.get("campaign_id") is not None]
        if w_start:
            determined = [c for c in determined if (lambda ts: ts and w_start <= ts < w_end)(_parse_iso(c.get("updated_at")))]
        if not determined:
            return None, 0, 0
        bad = sum(1 for c in determined if c["skip_bad"])
        good = len(determined) - bad
        return round(good / len(determined) * 100, 1), good, len(determined)

    def cpl_by_source(w_start=None, w_end=None):
        out = []
        for cid, camp in campaigns.items():
            leads = 0
            phones_in_campaign = set()
            for phone, c in contacts.items():
                if c.get("campaign_id") != cid:
                    continue
                phones_in_campaign.add(phone)
                pe = _parse_iso(c["positive_engagement_at"])
                if not pe:
                    continue
                if w_start and not (w_start <= pe < w_end):
                    continue
                leads += 1
            camp_outbound = [m for m in outbound if m["phone"] in phones_in_campaign]
            camp_inbound = [m for m in inbound if m["phone"] in phones_in_campaign]
            msg_cost = (
                messaging_cost(camp_outbound, sms_rates["sms_cost_outbound"], w_start, w_end) +
                messaging_cost(camp_inbound, sms_rates["sms_cost_inbound"], w_start, w_end)
            )
            total_cost = round((camp["cost"] or 0) + msg_cost, 2)
            cpl = round(total_cost / leads, 2) if leads else None
            out.append({
                "campaignId": cid, "name": camp["name"], "market": camp["market"],
                "listCost": camp["cost"], "messagingCost": msg_cost, "cost": total_cost,
                "leads": leads, "cpl": cpl,
            })
        out.sort(key=lambda x: (x["cpl"] is None, x["cpl"] if x["cpl"] is not None else 0))
        return out

    def revenue_per_delivered_text(w_start=None, w_end=None):
        total_revenue = 0.0
        for c in contacts.values():
            contract_ts = _parse_iso(c["contract_at"])
            if not contract_ts:
                continue
            if w_start and not (w_start <= contract_ts < w_end):
                continue
            total_revenue += c.get("revenue") or 0.0

        delivered = [m for m in outbound if m["status"] == "delivered"]
        if w_start:
            delivered = [m for m in delivered if w_start <= m["ts"] < w_end]
        delivered_count = len(delivered)
        per_text = round(total_revenue / delivered_count, 2) if delivered_count else None
        return {"totalRevenue": round(total_revenue, 2), "deliveredCount": delivered_count, "revenuePerText": per_text}

    monthly_accuracy_pct, monthly_accurate_count, monthly_determined_count = skip_trace_accuracy(month_start, month_end)
    alltime_accuracy_pct, alltime_accurate_count, alltime_determined_count = skip_trace_accuracy()

    monthly = {
        "skipTraceAccuracy": monthly_accuracy_pct,
        "skipTraceAccurateCount": monthly_accurate_count,
        "skipTraceDeterminedCount": monthly_determined_count,
        "cplBySource": cpl_by_source(month_start, month_end),
        "revenuePerDeliveredText": revenue_per_delivered_text(month_start, month_end),
    }
    all_time = {
        "skipTraceAccuracy": alltime_accuracy_pct,
        "skipTraceAccurateCount": alltime_accurate_count,
        "skipTraceDeterminedCount": alltime_determined_count,
        "cplBySource": cpl_by_source(),
        "revenuePerDeliveredText": revenue_per_delivered_text(),
    }

    # ================= KPIs =================
    def count_dated_field(field, w_start=None, w_end=None):
        n = 0
        for c in contacts.values():
            ts = _parse_iso(c.get(field))
            if not ts:
                continue
            if w_start and not (w_start <= ts < w_end):
                continue
            n += 1
        return n

    def kpi_block(w_start=None, w_end=None):
        texts_sent = len([m for m in outbound if not w_start or (w_start <= m["ts"] < w_end)])
        responses_received = len([m for m in inbound if not w_start or (w_start <= m["ts"] < w_end)])
        delivered_count = len([m for m in outbound if m["status"] == "delivered" and (not w_start or w_start <= m["ts"] < w_end)])
        leads = count_dated_field("positive_engagement_at", w_start, w_end)  # "$/lead" = fully-loaded cost per qualified lead
        offers = count_dated_field("offer_sent_at", w_start, w_end)
        contracts_n = count_dated_field("contract_at", w_start, w_end)
        list_cost = sum(camp["cost"] or 0 for camp in campaigns.values())  # not time-sliced; campaign spend has no per-day breakdown
        outbound_msg_cost = messaging_cost(outbound, sms_rates["sms_cost_outbound"], w_start, w_end)
        inbound_msg_cost = messaging_cost(inbound, sms_rates["sms_cost_inbound"], w_start, w_end)
        messaging_total = round(outbound_msg_cost + inbound_msg_cost, 2)
        total_cost = round(list_cost + messaging_total, 2)

        response_rate = round(responses_received / texts_sent * 100, 1) if texts_sent else 0.0
        return {
            "textsSent": texts_sent,
            "responsesReceived": responses_received,
            "responseRate": response_rate,
            "responseRateIsGood": response_rate >= 8.0,
            "deliverabilityRate": round(delivered_count / texts_sent * 100, 1) if texts_sent else 0.0,
            "positiveResponseRate": round(leads / responses_received * 100, 1) if responses_received else 0.0,
            "leads": leads,
            "offers": offers,
            "contracts": contracts_n,
            "listCost": round(list_cost, 2),
            "messagingCost": messaging_total,
            "outboundMessagingCost": round(outbound_msg_cost, 2),
            "inboundMessagingCost": round(inbound_msg_cost, 2),
            "totalCost": total_cost,
            "pricePerLead": round(total_cost / leads, 2) if leads else None,
            "pricePerContract": round(total_cost / contracts_n, 2) if contracts_n else None,
            "costPerContract": round(total_cost / contracts_n, 2) if contracts_n else None,
            # No appointment field exists in the schema yet; "qualified" status is the closest
            # proxy for "booked an appointment" until a dedicated timestamp is added.
            "appointmentsProxyQualifiedCount": sum(1 for c in contacts.values() if c["status"] == "qualified"),
        }

    kpis = {
        "monthly": kpi_block(month_start, month_end),
        "allTime": kpi_block(),
    }

    return jsonify({
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "allTime": all_time,
        "kpis": kpis,
        "campaigns": db.list_campaigns(),
        "smsRates": sms_rates,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4400))
    app.run(host="0.0.0.0", port=port, debug=False)
