import os
import json
import uuid
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

# ---------- Config Flask ----------
app = Flask(__name__)
CORS(app)

# ---------- Carica credenziali Google da env (sicuro) ----------
creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_env:
    raise Exception("Variabile GOOGLE_APPLICATION_CREDENTIALS_JSON mancante nell'environment")

try:
    creds_dict = json.loads(creds_env)
except Exception as e:
    raise Exception("Errore parsing GOOGLE_APPLICATION_CREDENTIALS_JSON: " + str(e))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)

# Inserisci qui l'ID del foglio Google (quello tra /d/ e /edit)
SHEET_ID = "16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI"
sheet = gc.open_by_key(SHEET_ID).sheet1

# ---------- Webhook secret (da env) ----------
WC_SECRET = os.environ.get("WC_WEBHOOK_SECRET")  # impostala su Render
# Nota: se non vuoi verificare signature, lascia WC_SECRET vuota; è comunque consigliato impostarla

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

def verify_wc_signature(secret, payload_bytes, header_sig):
    """
    WooCommerce signature: base64.b64encode(hmac_sha256(secret, payload_bytes))
    Some stores or proxies may produce hex digests in other setups; we check both.
    """
    if not secret:
        return True  # se non setti il secret, salta verifica (ma non è consigliato)
    try:
        computed = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
        computed_b64 = base64.b64encode(computed).decode()
        computed_hex = computed.hex()
        # header_sig può arrivare in diversi formati; proviamo a confrontare
        if header_sig == computed_b64 or header_sig == computed_hex:
            return True
        # alcuni sistemi inviano signature prefissata (es: sha256=...)
        if header_sig and header_sig.startswith("sha256="):
            if hmac.compare_digest("sha256=" + computed_hex, header_sig):
                return True
        return False
    except Exception:
        return False

# Template eventi (modifica se vuoi)
EVENTS_TEMPLATE = [
    {"title": "Etichetta creata", "day": 0, "loc": ""},
    {"title": "Presso magazzino", "day": 2, "loc": ""},
    {"title": "In transito", "day": 5, "loc": "Los Angeles, US"},
    {"title": "Arrivato in Italia", "day": 10, "loc": "Milan, IT"},
    {"title": "Dogana", "day": 14, "loc": "Malpensa, IT"},
    {"title": "In consegna", "day": 18, "loc": "IT"},
    {"title": "CONSEGNATO", "day": 21, "loc": ""}  # loc finale verrà impostata con CAP+Paese
]

# ---------- Webhook endpoint (WooCommerce) ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    # ricevo il body raw per la verifica HMAC (importantissimo)
    payload_bytes = request.get_data()

    # verifica firma se WC_SECRET settato
    header_sig = request.headers.get("X-WC-Webhook-Signature", "")
    if WC_SECRET:
        if not header_sig:
            # header mancante
            return jsonify({"error": "Missing X-WC-Webhook-Signature header"}), 401
        if not verify_wc_signature(WC_SECRET, payload_bytes, header_sig):
            return jsonify({"error": "Invalid webhook signature"}), 401

    # parsing robusto: JSON preferito, altrimenti form / json string in field
    data = {}
    if request.is_json:
        try:
            data = request.get_json()
        except Exception:
            data = {}
    else:
        form = request.form.to_dict()
        if form:
            # se è un singolo campo che contiene JSON, proviamo a parse
            if len(form) == 1:
                v = next(iter(form.values()))
                try:
                    data = json.loads(v)
                except Exception:
                    data = form
            else:
                data = form

    if not data:
        return jsonify({"error": "Empty payload"}), 400

    # Considera solo ordini pagati/processing/completed
    status = (data.get("status") or "").lower()
    if status not in ["processing", "completed", "on-hold", "paid"]:
        return jsonify({"status": "ignored"}), 200

    # Estrazione campi utili
    order_id = str(data.get("id") or data.get("number") or data.get("order_id") or "")
    # shipping preferito, fallback billing
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
    # service: prova shipping_lines array
    service = "APC Priority DDU"
    shipping_lines = data.get("shipping_lines") or data.get("shipping_lines", [])
    if isinstance(shipping_lines, list) and len(shipping_lines) > 0:
        first = shipping_lines[0]
        if isinstance(first, dict):
            service = first.get("method_title") or first.get("name") or service
        else:
            # potrebbe essere stringa
            service = str(first)

    # data ordine preferibile date_created
    created_at_raw = data.get("date_created") or data.get("created_at") or data.get("date_created_gmt")
    parsed = parse_datetime_iso(created_at_raw) if created_at_raw else None
    created_at_iso = parsed.isoformat() if parsed else now_utc().isoformat()

    # genera unique id e tracking link
    unique_id = str(uuid.uuid4())[:8]
    tracking_link = make_tracking_link(unique_id)

    # SALVA su Google Sheet
    # Intestazione prevista (prima riga): Order ID | Tracking Link | Created At | Service | Country | City | Postcode | Customer
    try:
        sheet.append_row([order_id, tracking_link, created_at_iso, service, country, city, postcode, customer_name])
    except Exception as e:
        # log su Render
        print("Errore append_row:", repr(e))
        return jsonify({"error": "Errore salvataggio su Google Sheet", "detail": str(e)}), 500

    # Risposta OK per WooCommerce
    return jsonify({"status": "success", "tracking_link": tracking_link}), 200

# ---------- API timeline completa ----------
@app.route("/api/track/<unique_id>", methods=["GET"])
def api_track(unique_id):
    try:
        records = sheet.get_all_records()
    except Exception as e:
        print("Errore lettura sheet:", repr(e))
        return jsonify({"error": "Errore lettura Sheet", "detail": str(e)}), 500

    found = None
    for r in records:
        # cerca unique_id in qualsiasi cella della riga
        for v in r.values():
            if isinstance(v, str) and unique_id in v:
                found = r
                break
        if found:
            break

    if not found:
        return jsonify({"error": "Tracking ID non trovato"}), 404

    # mappatura campi - prova vari nomi
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

    # costruisci events con occurred boolean e date
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

# ---------- Route HTML minimale per debug ----------
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
