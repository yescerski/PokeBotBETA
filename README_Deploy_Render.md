# PokeBot Receiver — Deploy to Render (Step‑by‑Step)

## Files
- `webhook_receiver_sendgrid.py`
- `requirements.txt`
- `Procfile`
- `render.yaml`

## Deploy (GitHub)
1) Push these files to a repo.
2) Render → New → Web Service → Connect repo.
3) Build: `pip install -r requirements.txt`
4) Start: `gunicorn webhook_receiver_sendgrid:app --bind 0.0.0.0:$PORT`
5) Env Vars: `ADMIN_USER`, `ADMIN_PASS`, `DECISIONS_DIR=decisions`, `PURCHASES_DIR=purchases`, `LOGS_DIR=logs`

## Verify
- `/healthz`
- `POST /event` with purchase JSON
- `/purchases.json` (Basic Auth if set)
- `/admin/purchases`, `/admin/purchases/live`
- `/metrics`
- `/admin/logs?n=200&format=txt`

## Local
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_USER=pokebot
export ADMIN_PASS=strong-password
python webhook_receiver_sendgrid.py
```
