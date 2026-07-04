# Inbound — SignalWire Lead Console (Python / Flask)

Same tool as before, rebuilt in Python: pulls **inbound SMS only** from your SignalWire log and lets you work each number as a lead (status, market/source, notes). This version is set up to deploy straight from GitHub to Render, with Postgres so your lead data survives redeploys.

## Local setup

```bash
cd signalwire-crm-py
pip install -r requirements.txt
python3 app.py
```

Open http://localhost:4400 and enter your Space, Project ID, and Auth Token on the connect screen. Locally this uses a SQLite file at `data/contacts.db` for lead data.

## Deploying: GitHub → Render

**1. Push this folder to a GitHub repo**
```bash
git init
git add .
git commit -m "Inbound SignalWire lead console"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/signalwire-crm.git
git push -u origin main
```

**2. Create the Render Blueprint**
This repo includes `render.yaml`, which tells Render to provision both the web service and a Postgres database in one step, and wire the database's connection string in automatically.

- Go to https://dashboard.render.com/blueprints
- Click **New Blueprint Instance**
- Connect your GitHub account if you haven't, then select the repo
- Render reads `render.yaml` and shows you the plan: one **Web Service** (`signalwire-crm`) and one **Postgres** database (`signalwire-crm-db`), both on free plans
- Click **Apply**

**3. Set your SignalWire credentials**
The blueprint intentionally leaves these blank (secrets shouldn't live in a GitHub repo). After the first deploy:
- Go to the `signalwire-crm` service → **Environment**
- Add `SIGNALWIRE_SPACE`, `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_AUTH_TOKEN` with your real values
- Save — Render redeploys automatically

**4. Done**
Visit the `.onrender.com` URL Render gives the service. It should already show as connected (no need to use the in-app connect screen, since the env vars are set).

### Where to find your SignalWire credentials
In your SignalWire dashboard: **Space name** is the subdomain in your Space URL (e.g. `cruz-associates-llc` from `cruz-associates-llc.signalwire.com`). **Project ID** and **Auth Token** are under *API* in the left nav.

## Why Postgres instead of a flat file

Render's web service disk is ephemeral — anything written to disk gets wiped on every redeploy or restart. The `render.yaml` blueprint provisions a small Postgres database alongside the web service specifically so lead status/notes/tags survive redeploys. `db.py` checks for a `DATABASE_URL` env var: if it's set (which it will be on Render via the blueprint), it uses Postgres; otherwise it falls back to local SQLite for development.

## Notes on scope

- Reads only — doesn't send messages, it's for triaging what's already come in.
- Each refresh scans up to ~2,000 recent messages (20 pages of 100) from SignalWire and filters to inbound. Say so if your volume runs higher and the cap can be raised or date-windowed.
- Free-tier Render web services spin down after inactivity and take ~30–60s to wake on the next request — fine for personal use, worth knowing about if it feels slow on first load after a break.
