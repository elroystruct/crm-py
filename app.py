import os
import base64
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
}


def creds_ready():
    return bool(creds["space"] and creds["projectId"] and creds["authToken"])


def base_url():
    space = creds["space"].replace("https://", "").replace("http://", "").rstrip("/")
    host = space if space.endswith(".signalwire.com") else f"{space}.signalwire.com"
    return f"https://{host}/api/laml/2010-04-01/Accounts/{creds['projectId']}"


def auth_header():
    token = base64.b64encode(f"{creds['projectId']}:{creds['authToken']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---------- static frontend ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- connection ----------
@app.route("/api/status")
def status():
    return jsonify({"connected": creds_ready(), "space": creds["space"] or None})


@app.route("/api/connect", methods=["POST"])
def connect():
    body = request.get_json(force=True, silent=True) or {}
    space, project_id, auth_token = body.get("space"), body.get("projectId"), body.get("authToken")
    if not (space and project_id and auth_token):
        return jsonify({"error": "space, projectId, and authToken are all required."}), 400
    creds["space"], creds["projectId"], creds["authToken"] = space, project_id, auth_token
    return jsonify({"connected": True, "space": creds["space"]})


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    creds["space"] = creds["projectId"] = creds["authToken"] = ""
    return jsonify({"connected": False})


# ---------- messages ----------
@app.route("/api/messages")
def messages():
    if not creds_ready():
        return jsonify({"error": "Not connected. Set your SignalWire Space, Project ID, and Auth Token first."}), 400

    max_pages = 20
    page_size = 100
    all_messages = []

    try:
        next_url = f"{base_url()}/Messages.json?PageSize={page_size}"
        pages_fetched = 0

        while next_url and pages_fetched < max_pages:
            r = requests.get(next_url, headers=auth_header(), timeout=20)
            if not r.ok:
                return jsonify({"error": f"SignalWire API error ({r.status_code}): {r.text[:300]}"}), r.status_code
            payload = r.json()
            all_messages.extend(payload.get("messages", []))
            pages_fetched += 1

            next_page_uri = payload.get("next_page_uri")
            if next_page_uri:
                host = base_url().split("/api/laml/2010-04-01/Accounts/")[0]
                next_url = f"{host}{next_page_uri}"
            else:
                next_url = None
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach SignalWire: {e}"}), 502

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
