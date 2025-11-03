import os
import json
import uuid
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

# ---------- Config Flask ----------
app = Flask(__name__)
CORS(app)

# ---------- Google creds (env) ----------
creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_env:
    raise Exception("GOOGLE_APPLICATION_CREDENTIALS_JSON missing")

try:
    creds_dict = json.loads(creds_env)
except Exception as e:
    raise Exception("Errore parsing GOOGLE_APPLICATION_CREDENTIALS_JSON: " + str(e))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
SHEET_ID = "16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI"
sheet = gc.open_by_key(SHEET_ID).sheet1

# ---------- secrets / debug ----------
WC_SECRET = os.environ.get("WC_WEBHOOK_SECRET", "")
DEBUG_SIG = os.environ.get("DEBUG_WC_SIG", "").lower() in ("1", "true", "yes")

# ---------- Helpers ----------
def now_utc():
    return datetime.utcnow()

def parse_datetime_iso(s):
    if not s:
        return None
    try:
        s2 = s.rstrip("Z")
        return datetime.fromisoformat(s2)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None

def make_tracking_link(uid):
    return "https://tracking-backend-tb40.onrender.com/track/{}".format(uid)

def compute_sigs(secret, payload_bytes):
    computed = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    b64 = base64.b64encode(computed).decode()
    hexs = computed.hex()
    return b64, hexs

def verify_sig(secret, payload_bytes, header_sig):
    if not secret:
        return True
    try:
        computed_b64, computed_hex = compute_sigs(secret, payload_bytes)
        if header_sig == computed_b64 or header_sig == computed_hex:
            return True
        if header_sig and header_sig.startswith("sha256="):
            if header_sig == "sha256=" + computed_hex:
                return True
        return False
    except Exception:
        return False

EVENTS_TEMPLATE = [
    {"title": "Etichetta creata", "day": 0, "loc": ""},
    {"title": "Presso magazzino", "day": 2, "loc": ""},
    {"title": "In transito", "day": 5, "loc": "Los Angeles, US"},
    {"title": "Arrivato in Italia", "day": 10, "loc": "Milan, IT"},
    {"title": "Dogana", "day": 14, "loc": "Malpensa, IT"},
    {"title": "In consegna", "day": 18, "loc": "IT"},
    {"title": "CONSEGNATO", "day": 21, "loc": ""}
]

# ---------- Webhook endpoint (con debug payload) ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    # raw bytes (per signature e debug)
    payload_bytes = request.get_data()

    # debug: stampa preview del payload (non stampare secret)
    if DEBUG_SIG:
        try:
            preview = payload_bytes[:2000].decode("utf-8", errors="replace")
        except Exception:
            preview = str(payload_bytes[:2000])
        print("DEBUG: content-type:", request.headers.get("Content-Type"))
        print("DEBUG: payload_length:", len(payload_bytes))
        print("DEBUG: payload_preview:", preview)

    # verifica signature se WC_SECRET settato
    header_sig = request.headers.get("X-WC-Webhook-Signature", "")
    if WC_SECRET:
        if not header_sig:
            print("WARN: signature header missing")
            return jsonify({"error": "Missing X-WC-Webhook-Signature header"}), 401
        if not verify_sig(WC_SECRET, payload_bytes, header_sig):
            print("WARN: signature mismatch")
            return jsonify({"error": "Invalid webhook signature"}), 401

    # parsing robusto: proviamo più strategie
    data = None

    # 1) Flask JSON parser
    if request.is_json:
        try:
            data = request.get_json()
        except Exception:
            data = None

    # 2) form data
    if data is None:
        try:
            form = request.form.to_dict()
        except Exception:
            form = {}
        if form:
            if len(form) == 1:
                v = next(iter(form.values()))
                try:
                    data = json.loads(v)
                except Exception:
                    data = form
            else:
                data = form

    # 3) raw JSON parse fallback
    if data is None:
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            data = None

    # 4) if still none -> return 400 with hint
    if data is None:
        print("ERROR: Empty or unparseable payload; content-type:", request.headers.get("Content-Type"))
        return jsonify({"error": "Empty or unparseable payload", "hint": "WooCommerce might be sending data as form-encoded or proxy may alter the body. Enable DEBUG_WC_SIG to see payload preview in logs."}), 400

    # normalize types: if list with dicts, take first element
    if not isinstance(data, dict):
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            data = data[0]
        else:
            print("ERROR: Unexpected payload type:", type(data))
            return jsonify({"error": "Unexpected payload type", "type": str(type(data))}), 400

    # ora sicuro di avere dict
    status = (data.get("status") or "").lower()
    if status not in ["processing", "completed", "on-hold", "paid"]:
        return jsonify({"status": "ignored"}), 200

    order_id = str(data.get("id") or data.get("number") or data.get("order_id") or "")
    shipping = data.get("shipping") or data.get("billing") or {}
    if isinstance(shipping, str):
        try:
            shipping = json.loads(shipping)
        except Exception:
            shipping = {}

    city = shipping.get("city") or shipping.get("town") or ""
    postcode = shipping.get("postcode") or shipping.get("zip") or ""
    country = shipping.get("country") or ""
    customer_name = ((data.get("billing") or {}).get("first_name", "") + " " + (data.get("billing") or {}).get("last_name", "")).strip()

    service = "APC Priority DDU"
    shipping_lines = data.get("shipping_lines") or []
    if isinstance(shipping_lines, list) and len(shipping_lines) > 0:
        first = shipping_lines[0]
        if isinstance(first, dict):
            service = first.get("method_title") or first.get("name") or service
        else:
            service = str(first)

    created_at_raw = data.get("date_created") or data.get("created_at")
    parsed = parse_datetime_iso(created_at_raw) if created_at_raw else None
    created_at_iso = parsed.isoformat() if parsed else now_utc().isoformat()

    unique_id = str(uuid.uuid4())[:8]
    tracking_link = make_tracking_link(unique_id)

    try:
        sheet.append_row([order_id, tracking_link, created_at_iso, service, country, city, postcode, customer_name])
    except Exception as e:
        print("ERR append_row:", repr(e))
        return jsonify({"error": "Errore salvataggio su Google Sheet", "detail": str(e)}), 500

    return jsonify({"status": "success", "tracking_link": tracking_link}), 200

# ---------- API timeline ----------
@app.route("/api/track/<unique_id>", methods=["GET"])
def api_track(unique_id):
    try:
        records = sheet.get_all_records()
    except Exception as e:
        return jsonify({"error": "Errore lettura Sheet", "detail": str(e)}), 500

    found = None
    for r in records:
        for v in r.values():
            if isinstance(v, str) and unique_id in v:
                found = r
                break
        if found:
            break

    if not found:
        return jsonify({"error": "Tracking ID non trovato"}), 404

    order_id = found.get("Order ID") or found.get("order_id") or found.get("Numero Ordine") or found.get("order") or ""
    created_at_raw = found.get("Created At") or found.get("created_at") or found.get("Data Ordine") or ""
    service = found.get("Service") or found.get("service") or "APC Priority DDU"
    country = found.get("Country") or found.get("country") or ""
    city = found.get("City") or found.get("city") or ""
    postcode = found.get("Postcode") or found.get("postcode") or ""
    customer = found.get("Customer") or found.get("customer") or found.get("Cliente") or ""

    created_dt = parse_datetime_iso(created_at_raw) or now_utc()
    days_passed = (now_utc() - created_dt).days
    is_delivered = days_passed >= 21
    status_text = "CONSEGNATO" if is_delivered else ("IN TRANSITO" if days_passed > 3 else "IN PREPARAZIONE")

    est_start = (created_dt + timedelta(days=19)).date().isoformat()
    est_end = (created_dt + timedelta(days=21)).date().isoformat()

    events = []
    for ev in EVENTS_TEMPLATE:
        ev_date = created_dt + timedelta(days=ev["day"])
        loc = ev["loc"]
        if ev["title"] == "CONSEGNATO":
            loc = "{} {}".format(postcode, country).strip()
        events.append({
            "title": ev["title"],
            "day_offset": ev["day"],
            "date_iso": ev_date.isoformat(),
            "date_readable": ev_date.strftime("%d/%m/%Y"),
            "location": loc,
            "occurred": days_passed >= ev["day"]
        })

    payload = {
        "order_id": order_id,
        "customer": customer,
        "service": service,
        "country": country,
        "city": city,
        "postcode": postcode,
        "created_at": created_dt.isoformat(),
        "days_passed": days_passed,
        "status": status_text,
        "estimated_start": est_start,
        "estimated_end": est_end,
        "events": events
    }

    return jsonify(payload), 200

# ---------- Debug HTML route ----------
@app.route("/track/<unique_id>")
def track_html(unique_id):
    api_url = "/api/track/{}".format(unique_id)
    html = """
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>Tracking</title></head>
      <body style="font-family:Arial,Helvetica,sans-serif; background:#f2f4f7; padding:20px;">
        <div style="max-width:900px;margin:20px auto;background:#fff;padding:20px;border-radius:8px;">
          <h2>Tracking ID: {uid}</h2>
          <pre id="content">Caricamento...</pre>
        </div>
        <script>
          (function(){
            var api = "{api}";
            fetch(api).then(function(r){ return r.json(); })
              .then(function(j){
                document.getElementById('content').innerText = JSON.stringify(j, null, 2);
              }).catch(function(e){
                document.getElementById('content').innerText = "Errore: " + e;
              });
          })();
        </script>
      </body>
    </html>
    """.replace("{uid}", unique_id).replace("{api}", api_url)
    return html

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
