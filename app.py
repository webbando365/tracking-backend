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

# ---------- Google Sheets (creds via env) ----------
creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_env:
    raise Exception("GOOGLE_APPLICATION_CREDENTIALS_JSON missing in environment")

try:
    creds_dict = json.loads(creds_env)
except Exception as e:
    raise Exception("Error parsing GOOGLE_APPLICATION_CREDENTIALS_JSON: " + str(e))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)

# Put your Google Sheet ID here (the /d/<ID>/ part)
SHEET_ID = "16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI"
sheet = gc.open_by_key(SHEET_ID).sheet1

# ---------- Webhook secret and debug ----------
WC_SECRET = os.environ.get("WC_WEBHOOK_SECRET", "")  # set on Render
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
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                from datetime import datetime as dt
                return dt.strptime(s, fmt)
            except Exception:
                continue
    return None

def make_tracking_link(uid):
    # Change domain if you add custom domain later
    return "https://tracking-backend-tb40.onrender.com/track/{}".format(uid)

def compute_sigs(secret, payload_bytes):
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    b64 = base64.b64encode(digest).decode()
    hexs = digest.hex()
    return b64, hexs

def verify_sig(secret, payload_bytes, header_sig):
    if not secret:
        return True
    try:
        b64, hexs = compute_sigs(secret, payload_bytes)
        # compare robustly
        if header_sig == b64 or header_sig == hexs:
            return True
        if header_sig and header_sig.startswith("sha256="):
            if header_sig == "sha256=" + hexs:
                return True
        return False
    except Exception:
        return False

# ---------- Realistic events template ----------
# offsets in days from order start (you can tweak)
EVENTS_TEMPLATE = [
    {"title": "Etichetta creata", "day": 0, "loc": ""},
    {"title": "Presso magazzino mittente", "day": 1, "loc": ""},
    {"title": "Spedito dal magazzino", "day": 2, "loc": ""},
    {"title": "Dogana di partenza", "day": 3, "loc": ""},
    {"title": "In transito internazionale", "day": 5, "loc": "In transito"},
    {"title": "Arrivato in aeroporto di destinazione", "day": 9, "loc": "Aeroporto IT"},
    {"title": "Sdoganamento", "day": 11, "loc": "Dogana IT"},
    {"title": "Consegnato al centro smistamento locale", "day": 14, "loc": "Ufficio postale locale"},
    {"title": "In consegna", "day": 18, "loc": "In consegna"},
    {"title": "CONSEGNATO", "day": 21, "loc": ""}  # final loc replaced by postcode+country
]

# ---------- Utility: safe extract for nested keys ----------
def safe_get(d, *keys, default=""):
    cur = d or {}
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, {})
        else:
            return default
    return cur if cur not in (None, {}) else default

# ---------- Root / health ----------
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "tracking-backend",
        "endpoints": ["/webhook (POST)", "/webhook-inspect (POST)", "/api/track/<id> (GET)", "/track/<id> (GET)"]
    }), 200

# ---------- Inspect endpoint (temporary debug) ----------
@app.route("/webhook-inspect", methods=["POST"])
def webhook_inspect():
    payload_bytes = request.get_data()
    content_type = request.headers.get("Content-Type", "")
    headers = dict(request.headers)
    try:
        preview = payload_bytes[:4000].decode("utf-8", errors="replace")
    except Exception:
        preview = str(payload_bytes[:4000])
    print("INSPECT: content-type:", content_type)
    print("INSPECT: headers:", json.dumps(headers))
    print("INSPECT: payload_length:", len(payload_bytes))
    print("INSPECT: payload_preview:", preview)
    return jsonify({"status": "inspected", "content_type": content_type, "length": len(payload_bytes)}), 200

# ---------- Webhook endpoint (production) ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    # raw body needed for signature verification
    payload_bytes = request.get_data()
    content_type = request.headers.get("Content-Type", "")

    # If it's a short form ping like webhook_id=1, just ack (200)
    if content_type and "application/x-www-form-urlencoded" in content_type:
        # often WooCommerce test pings send webhook_id=1
        form = request.form.to_dict()
        if form and ("webhook_id" in form or "webhook" in form):
            # ack the ping to keep WooCommerce happy
            print("PING received (form):", form)
            return jsonify({"status": "ping acknowledged"}), 200
        # else continue trying to parse

    # debug logs if requested
    if DEBUG_SIG:
        try:
            b64, hexs = compute_sigs(WC_SECRET, payload_bytes) if WC_SECRET else ("", "")
        except Exception as e:
            b64, hexs = ("ERR", "ERR")
        print("DEBUG: content-type:", content_type)
        print("DEBUG: payload_length:", len(payload_bytes))
        print("DEBUG: computed_b64:", repr(b64))
        print("DEBUG: computed_hex:", repr(hexs))
        print("DEBUG: header_sig:", repr(request.headers.get("X-WC-Webhook-Signature", "")))
        print("DEBUG: WC_SECRET present?:", bool(WC_SECRET))

    # verify signature if secret is present
    header_sig = request.headers.get("X-WC-Webhook-Signature", "")
    if WC_SECRET:
        if not header_sig:
            print("WARN: signature header missing")
            return jsonify({"error": "Missing signature header"}), 401
        if not verify_sig(WC_SECRET, payload_bytes, header_sig):
            print("WARN: signature mismatch")
            return jsonify({"error": "Invalid webhook signature"}), 401

    # Robust parsing: prefer parsed JSON, fallback to form or raw decode
    data = None
    if request.is_json:
        try:
            data = request.get_json()
        except Exception:
            data = None

    if data is None:
        # try form data
        try:
            form = request.form.to_dict()
        except Exception:
            form = {}
        if form:
            # sometimes WooCommerce wraps JSON inside a single form field
            if len(form) == 1:
                v = next(iter(form.values()))
                try:
                    data = json.loads(v)
                except Exception:
                    data = form
            else:
                data = form

    if data is None:
        # try raw JSON decode
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            data = None

    if data is None:
        print("ERROR: Empty or unparseable payload; content-type:", content_type)
        return jsonify({"error": "Empty or unparseable payload", "hint": "Use /webhook-inspect to see raw body"}), 400

    # Normalize if it's a list with one dict
    if not isinstance(data, dict):
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            data = data[0]
        else:
            print("ERROR: Unexpected payload type:", type(data))
            return jsonify({"error": "Unexpected payload type", "type": str(type(data))}), 400

    # Decide whether to process: prefer orders that are paid/processing/completed
    status = (data.get("status") or "").lower()
    if status not in ["processing", "completed", "paid"]:
        # skip non-final statuses (but ack)
        print("IGNORED order status:", status, "order_id:", data.get("id"))
        return jsonify({"status": "ignored", "order_status": status}), 200

    # Extract reliable timestamps: prefer date_paid, then date_completed, then date_created
    date_paid = data.get("date_paid")
    date_completed = data.get("date_completed")
    date_created = data.get("date_created") or data.get("created_at")
    chosen_dt = None
    for cand in (date_paid, date_completed, date_created):
        if cand:
            parsed = parse_datetime_iso(cand)
            if parsed:
                chosen_dt = parsed
                break
    if not chosen_dt:
        chosen_dt = now_utc()
    created_at_iso = chosen_dt.isoformat()

    # Extract order details (safe extraction)
    order_id = str(data.get("id") or data.get("number") or safe_get(data, "order_key") or "")
    billing = data.get("billing") or {}
    shipping = data.get("shipping") or {}
    # shipping sometimes empty — fallback to billing
    if not shipping or not any(shipping.values()):
        shipping = billing or {}

    customer_name = (billing.get("first_name", "") + " " + billing.get("last_name", "")).strip()
    city = shipping.get("city", "") or shipping.get("town", "") or ""
    postcode = shipping.get("postcode", "") or shipping.get("zip", "") or ""
    country = shipping.get("country", "") or ""
    total = data.get("total") or data.get("order_total") or ""

    # service: try shipping_lines array first
    service = "APC Priority DDU"
    shipping_lines = data.get("shipping_lines") or data.get("shipping_lines", [])
    if isinstance(shipping_lines, list) and len(shipping_lines) > 0:
        first = shipping_lines[0]
        if isinstance(first, dict):
            service = first.get("method_title") or first.get("name") or service
        else:
            service = str(first)

    # Generate unique ID and tracking link
    unique_id = str(uuid.uuid4())[:8]
    tracking_link = make_tracking_link(unique_id)

    # Save into Google Sheet (columns expected in first row):
    # Order ID | Tracking Link | Created At | Service | Country | City | Postcode | Customer | Status | Total
    try:
        sheet.append_row([order_id, tracking_link, created_at_iso, service, country, city, postcode, customer_name, status, total])
    except Exception as e:
        print("ERR append_row:", repr(e))
        return jsonify({"error": "Errore salvataggio su Google Sheet", "detail": str(e)}), 500

    # Ok response (WooCommerce expects 200)
    return jsonify({"status": "success", "tracking_link": tracking_link}), 200

# ---------- API: full timeline for frontend ----------
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

    # Map fields (compatible with different sheet headers)
    order_id = found.get("Order ID") or found.get("order_id") or found.get("Numero Ordine") or found.get("order") or found.get("Order") or ""
    created_at_raw = found.get("Created At") or found.get("created_at") or found.get("Data Ordine") or ""
    service = found.get("Service") or found.get("service") or "APC Priority DDU"
    country = found.get("Country") or found.get("country") or ""
    city = found.get("City") or found.get("city") or ""
    postcode = found.get("Postcode") or found.get("postcode") or ""
    customer = found.get("Customer") or found.get("customer") or found.get("Cliente") or ""
    status = found.get("Status") or found.get("status") or ""
    total = found.get("Total") or found.get("total") or ""

    created_dt = parse_datetime_iso(created_at_raw) or now_utc()
    days_passed = (now_utc() - created_dt).days
    is_delivered = days_passed >= 21
    status_text = "CONSEGNATO" if is_delivered else ("IN TRANSITO" if days_passed >= 3 else "IN PREPARAZIONE")

    est_start = (created_dt + timedelta(days=19)).date().isoformat()
    est_end = (created_dt + timedelta(days=21)).date().isoformat()

    events = []
    for ev in EVENTS_TEMPLATE:
        ev_date = created_dt + timedelta(days=ev["day"])
        loc = ev["loc"]
        if ev["title"] == "CONSEGNATO":
            loc = "{} {}".format(postcode, country).strip()
        # For minor realism: if event day is before 3 and city is blank use "Origin"
        if not loc:
            if ev["day"] <= 3:
                loc = "Origin"
            else:
                loc = ev.get("loc", "")
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
        "status": status,
        "total": total,
        "created_at": created_dt.isoformat(),
        "days_passed": days_passed,
        "status_text": status_text,
        "estimated_start": est_start,
        "estimated_end": est_end,
        "events": events
    }

    return jsonify(payload), 200

# ---------- Debug HTML route (renders JSON) ----------
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

# ---------- Run ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
