import os
import base64
import json
import bisect
import datetime as dt
from email.utils import parsedate_to_datetime
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

import db

load_dotenv()

app = Flask(__name__, static_folder="public", static_url_path="")

db.init_db()

# ---------- credential state (in-memory only, per process) ----------
creds = {
    "space": os.environ.get("SIGNALWIRE_SPACE", ""),
    "projectId": os.environ.get("SIGNALWIRE_PROJECT_ID", ""),
    "authToken": os.environ.get("SIGNALWIRE_AUTH_TOKEN", ""),
    "fromNumber": os.environ.get("SIGNALWIRE_FROM_NUMBER", ""),
}


def creds_ready():
    return bool(creds["space"] and creds["projectId"] and creds["authToken"])


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

    enriched = []
    for m in inbound:
        phone = m.get("from")
        crm = db.get_contact(phone)
        enriched.append({
            "sid": m.get("sid"),
            "from": m.get("from"),
            "to": m.get("to"),
            "body": m.get("body"),
            "dateSent": m.get("date_sent") or m.get("date_created"),
            "numMedia": int(m.get("num_media") or 0),
            "status": crm["status"],
            "notes": crm["notes"],
            "tags": crm["tags"],
            "market": crm["market"],
        })

    enriched.sort(key=lambda m: m["dateSent"] or "", reverse=True)
    return jsonify({"count": len(enriched), "messages": enriched})


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


# ---------- daily ops dashboard ----------
CARRIER_ERROR_LABELS = {
    "30003": "Unreachable handset",
    "30004": "Message blocked by carrier",
    "30005": "Unknown destination handset",
    "30006": "Landline or unreachable carrier",
    "30007": "Carrier content filtering",
    "30008": "Unknown carrier error",
}
OPT_OUT_KEYWORDS = {"stop", "end", "unsubscribe", "quit", "cancel", "remove", "stopall", "revoke"}


@app.route("/api/ops")
def ops():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    try:
        all_messages, err = _fetch_all_messages()
        if err:
            return err
    except Exception as e:
        return jsonify({"error": f"Unexpected error pulling SignalWire log: {e}"}), 500

    today = dt.datetime.now(dt.timezone.utc).date()

    # Build a full outbound-by-phone index (not just today's) so we can tell whether
    # a message received today has already been answered, even if the reply itself
    # lands after midnight.
    outbound_by_phone = {}
    for m in all_messages:
        if (m.get("direction") or "").startswith("inbound"):
            continue
        ts = _parse_ts(m.get("date_sent") or m.get("date_created"))
        if ts is None:
            continue
        outbound_by_phone.setdefault(m.get("to"), []).append(ts)
    for k in outbound_by_phone:
        outbound_by_phone[k].sort()

    outbound_today = []
    inbound_today = []
    for m in all_messages:
        ts = _parse_ts(m.get("date_sent") or m.get("date_created"))
        if ts is None or ts.date() != today:
            continue
        direction = m.get("direction") or ""
        if direction.startswith("inbound"):
            inbound_today.append({"phone": m.get("from"), "ts": ts, "body": (m.get("body") or "").strip().lower()})
        else:
            outbound_today.append({"status": m.get("status"), "error_code": m.get("error_code")})

    total_sent_today = len(outbound_today)
    delivered_today = sum(1 for m in outbound_today if m["status"] == "delivered")
    delivery_rate_today = round(delivered_today / total_sent_today * 100, 1) if total_sent_today else 0.0

    error_counts = {}
    for m in outbound_today:
        if m["status"] in ("failed", "undelivered") and m["error_code"]:
            code = str(m["error_code"])
            error_counts[code] = error_counts.get(code, 0) + 1
    error_breakdown = [
        {"code": code, "label": CARRIER_ERROR_LABELS.get(code, "Carrier error"), "count": count}
        for code, count in sorted(error_counts.items(), key=lambda kv: -kv[1])
    ]

    total_replies_today = len(inbound_today)
    opt_outs_today = sum(1 for m in inbound_today if m["body"] in OPT_OUT_KEYWORDS)
    opt_out_rate_today = round(opt_outs_today / total_replies_today * 100, 1) if total_replies_today else 0.0

    active_conversations = 0
    seen_active_phones = set()
    response_seconds_today = []
    for m in inbound_today:
        phone, ts = m["phone"], m["ts"]
        candidates = outbound_by_phone.get(phone, [])
        pos = bisect.bisect_right(candidates, ts)
        if pos < len(candidates):
            delta = (candidates[pos] - ts).total_seconds()
            if delta >= 0:
                response_seconds_today.append(delta)
        elif phone not in seen_active_phones:
            active_conversations += 1
            seen_active_phones.add(phone)

    avg_speed_to_lead_today = (
        sum(response_seconds_today) / len(response_seconds_today) if response_seconds_today else None
    )

    contacts = db.list_contacts()
    leads_today = sum(
        1 for c in contacts
        if c.get("status") == "qualified" and (c.get("statusUpdatedAt") or "").startswith(today.isoformat())
    )

    return jsonify({
        "date": today.isoformat(),
        "totalSentToday": total_sent_today,
        "deliveredToday": delivered_today,
        "deliveryRateToday": delivery_rate_today,
        "errorBreakdown": error_breakdown,
        "totalRepliesToday": total_replies_today,
        "optOutsToday": opt_outs_today,
        "optOutRateToday": opt_out_rate_today,
        "activeConversations": active_conversations,
        "avgSpeedToLeadToday": avg_speed_to_lead_today,
        "leadsToday": leads_today,
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
@app.route("/api/contacts/<path:phone>", methods=["GET"])
def get_contact_route(phone):
    return jsonify(db.get_contact(phone))


@app.route("/api/contacts/<path:phone>", methods=["PUT"])
def put_contact_route(phone):
    body = request.get_json(force=True, silent=True) or {}
    record = db.save_contact(
        phone,
        status=body.get("status"),
        notes=body.get("notes"),
        tags=body.get("tags"),
        market=body.get("market"),
    )
    return jsonify(record)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4400))
    app.run(host="0.0.0.0", port=port, debug=False)
