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

STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out", "remove"}

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
    skipped = 0
    for m in inbound:
        try:
            phone = m.get("from")
            crm = db.get_contact(phone)
            raw_date = m.get("date_sent") or m.get("date_created")
            enriched.append({
                "sid": m.get("sid"),
                "from": phone,
                "to": m.get("to"),
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
        }
        (inbound if direction.startswith("inbound") else outbound).append(row)

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
            for phone, c in contacts.items():
                if c.get("campaign_id") != cid:
                    continue
                pe = _parse_iso(c["positive_engagement_at"])
                if not pe:
                    continue
                if w_start and not (w_start <= pe < w_end):
                    continue
                leads += 1
            cpl = round(camp["cost"] / leads, 2) if leads else None
            out.append({
                "campaignId": cid, "name": camp["name"], "market": camp["market"],
                "cost": camp["cost"], "leads": leads, "cpl": cpl,
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

    return jsonify({
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "allTime": all_time,
        "campaigns": db.list_campaigns(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4400))
    app.run(host="0.0.0.0", port=port, debug=False)
