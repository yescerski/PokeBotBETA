from flask import Flask, request, jsonify, Response
from pathlib import Path
import json, os, re, datetime, base64, html, time, threading

app = Flask(__name__)

DECISIONS_DIR = Path(os.getenv("DECISIONS_DIR", "decisions"))
PURCHASES_DIR = Path(os.getenv("PURCHASES_DIR", "purchases"))
LOGS_DIR = Path(os.getenv("LOGS_DIR", "logs"))
for d in (DECISIONS_DIR, PURCHASES_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

ADMIN_USER = os.getenv("ADMIN_USER", "")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

TOKEN_RE = re.compile(r"(?i)token\s*:\s*([a-f0-9]{6,32})")

_metrics_lock = threading.Lock()
_metrics = {
    "http_requests_total": {},
    "decisions_total": 0,
    "purchases_total": 0,
    "purchases_amount_usd": 0.0
}
def _inc_http(method, path, status):
    with _metrics_lock:
        key = (method, path, int(status))
        _metrics["http_requests_total"][key] = _metrics["http_requests_total"].get(key, 0) + 1
def _inc_decision():
    with _metrics_lock:
        _metrics["decisions_total"] += 1
def _inc_purchase(amount):
    with _metrics_lock:
        _metrics["purchases_total"] += 1
        try:
            _metrics["purchases_amount_usd"] += float(amount or 0.0)
        except Exception:
            pass

@app.after_request
def _after(resp):
    try:
        _inc_http(request.method, request.path, resp.status_code)
    finally:
        return resp

def _prometheus_exposition():
    lines = []
    with _metrics_lock:
        lines.append("# HELP pokebot_decisions_total Total number of decisions stored")
        lines.append("# TYPE pokebot_decisions_total counter")
        lines.append(f"pokebot_decisions_total {_metrics['decisions_total']}")
        lines.append("# HELP pokebot_purchases_total Total number of purchases stored")
        lines.append("# TYPE pokebot_purchases_total counter")
        lines.append(f"pokebot_purchases_total {_metrics['purchases_total']}")
        lines.append("# HELP pokebot_purchases_amount_usd Sum of purchase amounts in USD")
        lines.append("# TYPE pokebot_purchases_amount_usd counter")
        lines.append(f"pokebot_purchases_amount_usd {_metrics['purchases_amount_usd']:.2f}")
        lines.append("# HELP pokebot_http_requests_total HTTP requests by method, path and status")
        lines.append("# TYPE pokebot_http_requests_total counter")
        for (method, path, status), count in sorted(_metrics["http_requests_total"].items()):
            plabel = path.replace('\\', '\\\\').replace('"', '\"')
            lines.append(f'pokebot_http_requests_total{{method="{method}",path="{plabel}",status="{status}"}} {count}')
    return "\n".join(lines) + "\n"

def _now():
    return datetime.datetime.utcnow().isoformat() + "Z"

def _log_json(line: dict, file_name="server.log"):
    p = LOGS_DIR / file_name
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _list_json(dirpath: Path, limit=200):
    files = sorted(dirpath.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out

def _check_basic_auth(auth_header: str) -> bool:
    if not ADMIN_USER or not ADMIN_PASS:
        return True
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        b64 = auth_header.split(" ", 1)[1].strip()
        userpass = base64.b64decode(b64).decode("utf-8")
        user, pw = userpass.split(":", 1)
        return user == ADMIN_USER and pw == ADMIN_PASS
    except Exception:
        return False

def _tail_file(path: Path, n=200):
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = -1
            data = []
            step = 1024
            while len(data) <= n and f.tell() > 0:
                f.seek(max(size + block*step, 0))
                chunk = f.read(step)
                data.extend(chunk.splitlines())
                block -= 1
            lines = data[-n:] if data else []
            return [l.decode("utf-8", errors="replace") for l in lines]
    except FileNotFoundError:
        return []
    except Exception:
        return []

@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "msg": "SendGrid inbound receiver alive"}), 200

@app.route("/healthz", methods=["GET"])
def healthz():
    decs = sorted(DECISIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    purs = sorted(PURCHASES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest_dec = decs[0].stat().st_mtime if decs else 0
    latest_pur = purs[0].stat().st_mtime if purs else 0
    return jsonify({
        "ok": True,
        "decisions_count": len(decs),
        "purchases_count": len(purs),
        "latest_decision_ts": datetime.datetime.utcfromtimestamp(latest_dec).isoformat() + "Z" if latest_dec else None,
        "latest_purchase_ts": datetime.datetime.utcfromtimestamp(latest_pur).isoformat() + "Z" if latest_pur else None
    }), 200

@app.route("/metrics", methods=["GET"])
def metrics():
    text = _prometheus_exposition()
    return Response(text, mimetype="text/plain; version=0.0.4; charset=utf-8")

@app.route("/admin/logs", methods=["GET"])
def admin_logs():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    try:
        n = int(request.args.get("n", "200"))
    except Exception:
        n = 200
    n = max(1, min(n, 5000))
    fmt = (request.args.get("format", "jsonl") or "jsonl").lower()
    lines = _tail_file(LOGS_DIR / "server.log", n=n)
    if fmt == "txt":
        body = "\n".join(lines) + ("\n" if lines else "")
        return Response(body, mimetype="text/plain; charset=utf-8")
    return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="application/x-ndjson")

@app.route("/inbound", methods=["POST"])
def inbound():
    frm = request.form.get("from", "")
    to = request.form.get("to", "")
    subject = request.form.get("subject", "")
    text = request.form.get("text", "") or ""
    html_body = request.form.get("html", "") or ""

    token_match = TOKEN_RE.search(text) or TOKEN_RE.search(html_body)
    if not token_match:
        _log_json({"ts": _now(), "type": "inbound", "ok": False, "reason": "no_token"})
        return jsonify({"ok": False, "error": "TOKEN not found in message body"}), 200

    token = token_match.group(1)
    decision = None
    body = text.strip() or html_body
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "1":
            decision = "1"; break
        if stripped == "2":
            decision = "2"; break
    if decision is None:
        if "1" in body and "2" not in body:
            decision = "1"
        elif "2" in body and "1" not in body:
            decision = "2"

    if decision not in ("1", "2"):
        _log_json({"ts": _now(), "type": "inbound", "ok": False, "token": token, "reason": "no_decision"})
        return jsonify({"ok": False, "error": "Decision (1/2) not found"}), 200

    record = {"token": token, "decision": decision, "from": frm, "to": to, "subject": subject, "ts": _now()}
    (DECISIONS_DIR / f"{token}.json").write_text(json.dumps(record, indent=2))
    _inc_decision()
    _log_json({"ts": _now(), "type": "decision_store", "ok": True, "token": token, "decision": decision})
    return jsonify({"ok": True, "stored": record}), 200

@app.route("/decision/<token>", methods=["GET"])
def get_decision(token: str):
    p = DECISIONS_DIR / f"{token}.json"
    if not p.exists():
        return jsonify({"ok": False, "status": "pending"}), 200
    try:
        data = json.loads(p.read_text())
        return jsonify({"ok": True, "status": "found", "data": data}), 200
    except Exception as e:
        return jsonify({"ok": False, "status": "error", "error": str(e)}), 500

@app.route("/decisions.json", methods=["GET"])
def decisions_json():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    items = _list_json(DECISIONS_DIR, 50)
    return jsonify({"ok": True, "items": items}), 200

@app.route("/admin", methods=["GET"])
def admin():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    items = _list_json(DECISIONS_DIR, 50)
    rows = []
    for it in items:
        rows.append(
            f"<tr>"
            f"<td>{html.escape(it.get('ts',''))}</td>"
            f"<td><code>{html.escape(it.get('token',''))}</code></td>"
            f"<td>{html.escape(str(it.get('decision','')))}</td>"
            f"<td>{html.escape(it.get('from',''))}</td>"
            f"<td>{html.escape(it.get('to',''))}</td>"
            f"<td>{html.escape(it.get('subject',''))}</td>"
            f"</tr>"
        )
    table = "\n".join(rows) or "<tr><td colspan='6'>No decisions yet</td></tr>"
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PokeBot Decisions</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; padding: 24px; }}
    h1 {{ margin-top: 0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 14px; }}
    th {{ background: #fafafa; }}
    code {{ background: #f4f4f4; padding: 2px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>🧾 PokeBot — Recent Decisions</h1>
  <p>Showing the 50 most recent approvals/denials captured via SendGrid Inbound Parse.</p>
  <table>
    <thead>
      <tr><th>Timestamp (UTC)</th><th>Token</th><th>Decision</th><th>From</th><th>To</th><th>Subject</th></tr>
    </thead>
    <tbody>
      {table}
    </tbody>
  </table>
  <p style="margin-top:24px;">JSON: <a href="/decisions.json">/decisions.json</a></p>
  <p style="margin-top:8px;">Live view: <a href="/admin/live">/admin/live</a></p>
  <p style="margin-top:8px;">Purchases: <a href="/admin/purchases">/admin/purchases</a> | <a href="/admin/purchases/live">/admin/purchases/live</a></p>
</body>
</html>"""
    return Response(html_doc, mimetype="text/html")

@app.route("/admin/live", methods=["GET"])
def admin_live():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    html_doc = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PokeBot Decisions — Live</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; padding: 24px; }
    h1 { margin-top: 0; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 14px; }
    th { background: #fafafa; }
    code { background: #f4f4f4; padding: 2px 4px; border-radius: 4px; }
    .new { background: #e6ffed; }
  </style>
</head>
<body>
  <h1>🛰️ PokeBot — Live Decisions</h1>
  <p>Auto-refreshing view (every 4s). New rows flash green briefly.</p>
  <table id="tbl">
    <thead>
      <tr><th>Timestamp (UTC)</th><th>Token</th><th>Decision</th><th>From</th><th>To</th><th>Subject</th></tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="6">Loading…</td></tr>
    </tbody>
  </table>
<script>
let known = new Set();
function esc(s){return (s||'').toString().replace(/[&<>]/g, t => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[t]));}
function render(items){
  const tb = document.getElementById('tbody');
  tb.innerHTML='';
  for(const it of items){
    const tr = document.createElement('tr');
    const isNew = !known.has(it.token);
    if(isNew){ tr.classList.add('new'); setTimeout(()=>tr.classList.remove('new'), 1200); }
    known.add(it.token);
    tr.innerHTML = `<td>${esc(it.ts)}</td>
                    <td><code>${esc(it.token)}</code></td>
                    <td>${esc(it.decision)}</td>
                    <td>${esc(it.from)}</td>
                    <td>${esc(it.to)}</td>
                    <td>${esc(it.subject)}</td>`;
    tb.appendChild(tr);
  }
}
async function tick(){
  try{
    const r = await fetch('/decisions.json');
    if(!r.ok){ return; }
    const j = await r.json();
    if(j && j.ok && Array.isArray(j.items)){
      render(j.items);
    }
  }catch(e){}
}
tick();
setInterval(tick, 4000);
</script>
</body>
</html>"""
    return Response(html_doc, mimetype="text/html")

def _purchases_total_and_rows(limit=200):
    items = _list_json(PURCHASES_DIR, limit)
    total = 0.0
    rows = []
    for it in items:
        amt = float(it.get("amount", 0.0))
        total += amt
        rows.append(
            f"<tr>"
            f"<td>{html.escape(it.get('ts',''))}</td>"
            f"<td>{html.escape(it.get('site',''))}</td>"
            f"<td>{html.escape(it.get('order',''))}</td>"
            f"<td style='text-align:right;'>${amt:,.2f}</td>"
            f"<td>{html.escape(json.dumps(it.get('items', []))[:120])}</td>"
            f"</tr>"
        )
    return total, "\n".join(rows) or "<tr><td colspan='5'>No purchases yet</td></tr>", items

@app.route("/event", methods=["POST"])
def event():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        payload = {}
    kind = payload.get("event")
    if kind == "purchase":
        payload["ts"] = _now()
        amount = payload.get("amount", 0)
        fn = f"purchase_{int(time.time()*1000)}.json"
        (PURCHASES_DIR / fn).write_text(json.dumps(payload, indent=2))
        _inc_purchase(amount)
        _log_json({"ts": _now(), "type": "purchase_store", "ok": True, "order": payload.get("order"), "amount": amount})
        return jsonify({"ok": True, "stored": fn}), 200
    _log_json({"ts": _now(), "type": "event_ignored", "event": kind})
    return jsonify({"ok": False, "error": "unsupported_event"}), 400

@app.route("/purchases.json", methods=["GET"])
def purchases_json():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    total, _, items = _purchases_total_and_rows()
    return jsonify({"ok": True, "total": total, "items": items}), 200

@app.route("/admin/purchases", methods=["GET"])
def admin_purchases():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    total, rows, _ = _purchases_total_and_rows()
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PokeBot Purchases</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; padding: 24px; }}
    h1 {{ margin-top: 0; }}
    .total {{ font-size: 18px; margin: 8px 0 16px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 14px; }}
    th {{ background: #fafafa; }}
    td:nth-child(4) {{ text-align: right; }}
  </style>
</head>
<body>
  <h1>💳 PokeBot — Purchases</h1>
  <div class="total"><strong>Total USD:</strong> ${total:,.2f}</div>
  <table>
    <thead>
      <tr><th>Timestamp (UTC)</th><th>Site</th><th>Order ID</th><th>Amount</th><th>Items</th></tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <p style="margin-top:24px;">JSON: <a href="/purchases.json">/purchases.json</a> | Live: <a href="/admin/purchases/live">/admin/purchases/live</a></p>
</body>
</html>"""
    return Response(html_doc, mimetype="text/html")

@app.route("/admin/purchases/live", methods=["GET"])
def admin_purchases_live():
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": "Basic realm='PokeBot Admin'"})
    html_doc = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PokeBot Purchases — Live</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; padding: 24px; }
    h1 { margin-top: 0; }
    .total { font-size: 18px; margin: 8px 0 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 14px; }
    th { background: #fafafa; }
    td:nth-child(4) { text-align: right; }
    .new { background: #e6ffed; }
  </style>
</head>
<body>
  <h1>📡 PokeBot — Purchases (Live)</h1>
  <div class="total"><strong>Total USD:</strong> <span id="total">$0.00</span></div>
  <table id="tbl">
    <thead>
      <tr><th>Timestamp (UTC)</th><th>Site</th><th>Order ID</th><th>Amount</th><th>Items</th></tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="5">Loading…</td></tr>
    </tbody>
  </table>
<script>
let known = new Set();
function esc(s){return (s||'').toString().replace(/[&<>]/g, t => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[t]));}
function fmt(n){try{return new Intl.NumberFormat(undefined,{style:'currency',currency:'USD'}).format(n)}catch(e){return '$'+Number(n).toFixed(2)}}
function render(items, total){
  const tb = document.getElementById('tbody');
  const totalEl = document.getElementById('total');
  totalEl.textContent = fmt(total);
  tb.innerHTML='';
  for(const it of items){
    const tr = document.createElement('tr');
    const key = (it.order||'') + ':' + (it.ts||'');
    const isNew = !known.has(key);
    if(isNew){ tr.classList.add('new'); setTimeout(()=>tr.classList.remove('new'), 1200); }
    known.add(key);
    tr.innerHTML = `<td>${esc(it.ts)}</td>
                    <td>${esc(it.site)}</td>
                    <td>${esc(it.order||'')}</td>
                    <td style="text-align:right;">${fmt(+it.amount||0)}</td>
                    <td>${esc(JSON.stringify(it.items||[])).slice(0,120)}</td>`;
    tb.appendChild(tr);
  }
}
async function tick(){
  try{
    const r = await fetch('/purchases.json');
    if(!r.ok){ return; }
    const j = await r.json();
    if(j && j.ok && Array.isArray(j.items)){
      render(j.items, j.total||0);
    }
  }catch(e){}
}
tick();
setInterval(tick, 4000);
</script>
</body>
</html>"""
    return Response(html_doc, mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
