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
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                from datetime import datetime as dt
                return dt.strptime(s, fmt)
            except Exception:
                continue
    return None

def make_tracking_link(uid):
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
        if header_sig == b64 or header_sig == hexs:
            return True
        if header_sig and header_sig.startswith("sha256="):
            if header_sig == "sha256=" + hexs:
                return True
        return False
    except Exception:
        return False

# ---------- Realistic events templates (USA to Europe, 14-day total) ----------
EVENTS_TEMPLATE_EN = [
    {"title": "Label created", "day": 0, "loc": "Los Angeles, CA"},
    {"title": "Package received at origin facility", "day": 1, "loc": "Los Angeles, CA"},
    {"title": "Package departed origin facility", "day": 2, "loc": "Los Angeles, CA"},
    {"title": "Departed from airport of origin", "day": 3, "loc": "Los Angeles International Airport (LAX)"},
    {"title": "In transit to Europe", "day": 5, "loc": "Over the Atlantic Ocean"},
    {"title": "Arrived at European transit facility", "day": 7, "loc": "Warsaw, PL (Transit Hub)"},
    {"title": "Departed European transit facility", "day": 8, "loc": "Warsaw, PL"},
    {"title": "Arrived at local sorting center", "day": 10, "loc": "Local hub"},
    {"title": "Arrived at destination country facility", "day": 11, "loc": "Destination country"},
    {"title": "Customs clearance initiated (in progress)", "day": 12, "loc": "Destination customs"},
    {"title": "Ending customs clearance", "day": 14, "loc": "Destination customs"},
    {"title": "Customs cleared (final delivery expected)", "day": 16, "loc": ""},
    {"title": "In transit to final delivery city", "day": 17, "loc": "Destination country"},
    {"title": "Attempted delivery - contact us to reschedule", "day": 19, "loc": "Destination country"},
]

EVENTS_TEMPLATE_IT = [
    {"title": "Etichetta creata", "day": 0, "loc": "Los Angeles, CA"},
    {"title": "Pacco ricevuto nella struttura di origine", "day": 1, "loc": "Los Angeles, CA"},
    {"title": "Pacco partito dalla struttura di origine", "day": 2, "loc": "Los Angeles, CA"},
    {"title": "Partito dall'aeroporto di origine", "day": 3, "loc": "Los Angeles International Airport (LAX)"},
    {"title": "In transito verso l'Europa", "day": 5, "loc": "Sopra l'Oceano Atlantico"},
    {"title": "Arrivato nella struttura di transito europea", "day": 7, "loc": "Varsavia, PL (Hub di transito)"},
    {"title": "Partito dalla struttura di transito europea", "day": 8, "loc": "Varsavia, PL"},
    {"title": "Arrivato al centro di smistamento locale", "day": 10, "loc": "Hub locale"},
    {"title": "Arrivato nella struttura del paese di destinazione", "day": 11, "loc": "Paese di destinazione"},
    {"title": "Sdoganamento avviato (in corso)", "day": 12, "loc": "Dogana di destinazione"},
    {"title": "Conclusione sdoganamento", "day": 14, "loc": "Dogana di destinazione"},
    {"title": "Sdoganamento completato (consegna finale prevista)", "day": 16, "loc": ""},
    {"title": "In transito verso la città di consegna finale", "day": 17, "loc": "Paese di destinazione"},
    {"title": "Tentativo di consegna - contattaci per riprogrammare", "day": 19, "loc": "Paese di destinazione"},
]

EVENTS_TEMPLATE_DE = [
    {"title": "Etikett erstellt", "day": 0, "loc": "Los Angeles, CA"},
    {"title": "Paket in der Ursprungseinrichtung erhalten", "day": 1, "loc": "Los Angeles, CA"},
    {"title": "Paket hat Ursprungseinrichtung verlassen", "day": 2, "loc": "Los Angeles, CA"},
    {"title": "Vom Ursprungsflughafen abgeflogen", "day": 3, "loc": "Los Angeles International Airport (LAX)"},
    {"title": "Auf dem Weg nach Europa", "day": 5, "loc": "Über dem Atlantischen Ozean"},
    {"title": "In europäischer Transiteinrichtung angekommen", "day": 7, "loc": "Warschau, PL (Transit-Hub)"},
    {"title": "Europäische Transiteinrichtung verlassen", "day": 8, "loc": "Warschau, PL"},
    {"title": "Im lokalen Sortierzentrum angekommen", "day": 10, "loc": "Lokales Hub"},
    {"title": "In der Einrichtung des Ziellandes angekommen", "day": 11, "loc": "Zielland"},
    {"title": "Zollabfertigung eingeleitet (in Bearbeitung)", "day": 12, "loc": "Zoll am Zielort"},
    {"title": "Abschluss der Zollabfertigung", "day": 14, "loc": "Zoll am Zielort"},
    {"title": "Zollabfertigung abgeschlossen (endgültige Lieferung erwartet)", "day": 16, "loc": ""},
    {"title": "Auf dem Weg zur endgültigen Lieferstadt", "day": 17, "loc": "Zielland"},
    {"title": "Zustellversuch - kontaktieren Sie uns zur Neuplanung", "day": 19, "loc": "Zielland"},
]

EVENTS_TEMPLATE_SE = [
    {"title": "Etikett skapat", "day": 0, "loc": "Los Angeles, CA"},
    {"title": "Paket mottaget vid ursprungsanläggning", "day": 1, "loc": "Los Angeles, CA"},
    {"title": "Paket har lämnat ursprungsanläggning", "day": 2, "loc": "Los Angeles, CA"},
    {"title": "Avgått från ursprungsflygplats", "day": 3, "loc": "Los Angeles International Airport (LAX)"},
    {"title": "På väg till Europa", "day": 5, "loc": "Över Atlanten"},
    {"title": "Anlänt till europeisk transitanläggning", "day": 7, "loc": "Warszawa, PL (Transitnav)"},
    {"title": "Lämnat europeisk transitanläggning", "day": 8, "loc": "Warszawa, PL"},
    {"title": "Anlänt till lokalt sorteringscenter", "day": 10, "loc": "Lokalt nav"},
    {"title": "Anlänt till destinationslandets anläggning", "day": 11, "loc": "Destinationsland"},
    {"title": "Tullklarering påbörjad (pågår)", "day": 12, "loc": "Destinationstull"},
    {"title": "Avslutar tullklarering", "day": 14, "loc": "Destinationstull"},
    {"title": "Tullklarerad (slutlig leverans förväntas)", "day": 16, "loc": ""},
    {"title": "På väg till slutgiltig leveransstad", "day": 17, "loc": "Destinationsland"},
    {"title": "Leveransförsök - kontakta oss för ombokning", "day": 19, "loc": "Destinationsland"},
]

EVENTS_TEMPLATE_NL = [
    {"title": "Label aangemaakt", "day": 0, "loc": "Los Angeles, CA"},
    {"title": "Pakket ontvangen bij oorsprongsfaciliteit", "day": 1, "loc": "Los Angeles, CA"},
    {"title": "Pakket vertrokken van oorsprongsfaciliteit", "day": 2, "loc": "Los Angeles, CA"},
    {"title": "Vertrokken van oorsprongsluchthaven", "day": 3, "loc": "Los Angeles International Airport (LAX)"},
    {"title": "Onderweg naar Europa", "day": 5, "loc": "Boven de Atlantische Oceaan"},
    {"title": "Aangekomen bij Europese transitfaciliteit", "day": 7, "loc": "Warschau, PL (Transithub)"},
    {"title": "Vertrokken van Europese transitfaciliteit", "day": 8, "loc": "Warschau, PL"},
    {"title": "Aangekomen bij lokaal sorteercentrum", "day": 10, "loc": "Lokale hub"},
    {"title": "Aangekomen bij faciliteit van bestemmingsland", "day": 11, "loc": "Bestemmingsland"},
    {"title": "Douaneafhandeling gestart (in behandeling)", "day": 12, "loc": "Douane bestemming"},
    {"title": "Beëindiging douaneafhandeling", "day": 14, "loc": "Douane bestemming"},
    {"title": "Douane afgehandeld (definitieve levering verwacht)", "day": 16, "loc": ""},
    {"title": "Onderweg naar definitieve leveringsstad", "day": 17, "loc": "Bestemmingsland"},
    {"title": "Bezorgpoging - neem contact op voor herplanning", "day": 19, "loc": "Bestemmingsland"},
]

# Funzione per scegliere il template in base al paese
def get_events_template(country_code):
    country = (country_code or "").upper()
    if country == "IT":
        return EVENTS_TEMPLATE_IT
    elif country in ["DE", "AT", "CH"]:
        return EVENTS_TEMPLATE_DE
    elif country in ["SE", "NO", "DK"]:
        return EVENTS_TEMPLATE_SE
    elif country in ["NL", "BE"]:
        return EVENTS_TEMPLATE_NL
    else:
        return EVENTS_TEMPLATE_EN

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
    payload_bytes = request.get_data()
    content_type = request.headers.get("Content-Type", "")

    if content_type and "application/x-www-form-urlencoded" in content_type:
        form = request.form.to_dict()
        if form and ("webhook_id" in form or "webhook" in form):
            print("PING received (form):", form)
            return jsonify({"status": "ping acknowledged"}), 200

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

    header_sig = request.headers.get("X-WC-Webhook-Signature", "")
    if WC_SECRET:
        if not header_sig:
            print("WARN: signature header missing")
            return jsonify({"error": "Missing signature header"}), 401
        if not verify_sig(WC_SECRET, payload_bytes, header_sig):
            print("WARN: signature mismatch")
            return jsonify({"error": "Invalid webhook signature"}), 401

    data = None
    if request.is_json:
        try:
            data = request.get_json()
        except Exception:
            data = None

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

    if data is None:
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            data = None

    if data is None:
        print("ERROR: Empty or unparseable payload; content-type:", content_type)
        return jsonify({"error": "Empty or unparseable payload", "hint": "Use /webhook-inspect to see raw body"}), 400

    if not isinstance(data, dict):
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            data = data[0]
        else:
            print("ERROR: Unexpected payload type:", type(data))
            return jsonify({"error": "Unexpected payload type", "type": str(type(data))}), 400

    status = (data.get("status") or "").lower()
    if status not in ["processing", "completed", "paid"]:
        print("IGNORED order status:", status, "order_id:", data.get("id"))
        return jsonify({"status": "ignored", "order_status": status}), 200

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

    order_id = str(data.get("id") or data.get("number") or safe_get(data, "order_key") or "")
    billing = data.get("billing") or {}
    shipping = data.get("shipping") or {}
    if not shipping or not any(shipping.values()):
        shipping = billing or {}

    customer_name = (billing.get("first_name", "") + " " + billing.get("last_name", "")).strip()
    city = shipping.get("city", "") or shipping.get("town", "") or ""
    postcode = shipping.get("postcode", "") or shipping.get("zip", "") or ""
    country = shipping.get("country", "") or ""
    total = data.get("total") or data.get("order_total") or ""

    service = "International Air Express"
    shipping_lines = data.get("shipping_lines") or data.get("shipping_lines", [])
    if isinstance(shipping_lines, list) and len(shipping_lines) > 0:
        first = shipping_lines[0]
        if isinstance(first, dict):
            service = first.get("method_title") or first.get("name") or service
        else:
            service = str(first)

    unique_id = str(uuid.uuid4())[:8]
    tracking_link = make_tracking_link(unique_id)

    try:
        sheet.append_row([order_id, tracking_link, created_at_iso, service, country, city, postcode, customer_name, status, total])
    except Exception as e:
        print("ERR append_row:", repr(e))
        return jsonify({"error": "Errore salvataggio su Google Sheet", "detail": str(e)}), 500

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

    order_id = found.get("Order ID") or found.get("order_id") or found.get("Numero Ordine") or found.get("order") or found.get("Order") or ""
    created_at_raw = found.get("Created At") or found.get("created_at") or found.get("Data Ordine") or ""
    service = found.get("Service") or found.get("service") or "International Air Express"
    country = found.get("Country") or found.get("country") or ""
    city = found.get("City") or found.get("city") or ""
    postcode = found.get("Postcode") or found.get("postcode") or ""
    customer = found.get("Customer") or found.get("customer") or found.get("Cliente") or ""
    status = found.get("Status") or found.get("status") or ""
    total = found.get("Total") or found.get("total") or ""

    created_dt = parse_datetime_iso(created_at_raw) or now_utc()

    # Shipment starts 2 days after order creation
    shipment_start = created_dt + timedelta(days=2)
    days_passed = (now_utc() - shipment_start).days
    if days_passed < 0:
        days_passed = 0

    is_delivered = days_passed >= 16
    status_text = "DELIVERED" if is_delivered else ("IN TRANSIT" if days_passed >= 3 else "PREPARING SHIPMENT")

    est_start = (shipment_start + timedelta(days=14)).date().isoformat()
    est_end = est_start

    # Seleziona il template degli eventi in base al paese
    events_template = get_events_template(country)
    
    events = []
    for ev in events_template:
        ev_date = shipment_start + timedelta(days=ev["day"])
        loc = ev["loc"]
        # Controlla se è l'evento finale di sdoganamento (giorno 16 in tutti i template)
        if ev["day"] == 16:
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
          <pre id="content">Loading...</pre>
        </div>
        <script>
          (function(){
            var api = "{api}";
            fetch(api).then(function(r){ return r.json(); })
              .then(function(j){
                document.getElementById('content').innerText = JSON.stringify(j, null, 2);
              }).catch(function(e){
                document.getElementById('content').innerText = "Error: " + e;
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
