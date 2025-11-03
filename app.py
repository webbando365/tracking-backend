import os
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

# ---------- Config Flask ----------
app = Flask(__name__)
CORS(app)

# ---------- Config Google Sheets (da variabile d'ambiente) ----------
creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_env:
    raise Exception("Variabile GOOGLE_APPLICATION_CREDENTIALS_JSON mancante su environment")

creds_dict = json.loads(creds_env)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)

# Inserisci qui l'ID del foglio Google (quello tra /d/ e /edit)
SHEET_ID = "16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI"
sheet = gc.open_by_key(SHEET_ID).sheet1

# ---------- Helper ----------
def now_utc():
    return datetime.utcnow()

def parse_datetime_iso(s):
    if not s:
        return None
    try:
        # rimuovi eventuale Z
        s2 = s.rstrip("Z")
        return datetime.fromisoformat(s2)
    except Exception:
        # prova formati comuni
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

def make_tracking_link(unique_id):
    # Link pubblico -> usa il dominio Render
    return f"https://tracking-backend-tb40.onrender.com/track/{unique_id}"

# Template eventi: titolo, giorno offset, luogo (puoi modificare aggiungendo/rimuovendo)
EVENTS_TEMPLATE = [
    {"title": "Etichetta creata", "day": 0, "loc": ""},
    {"title": "Presso magazzino", "day": 2, "loc": ""},
    {"title": "In transito", "day": 5, "loc": "Los Angeles, US"},
    {"title": "Arrivato in Italia", "day": 10, "loc": "Milan, IT"},
    {"title": "Dogana", "day": 14, "loc": "Malpensa, IT"},
    {"title": "In consegna", "day": 18, "loc": "IT"},
    {"title": "CONSEGNATO", "day": 21, "loc": ""}  # loc verrà sostituita con CAP + Paese se disponibile
]

# ---------- Webhook (WooCommerce) ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    # WooCommerce può inviare JSON oppure form-data: gestiamo entrambi
    data = {}
    if request.is_json:
        data = request.get_json()
    else:
        # request.form può contenere campi come payload (talvolta JSON string dentro un campo)
        form = request.form.to_dict()
        if form:
            # se esiste un singolo campo che è un JSON, proviamo a parsarlo
            if len(form) == 1:
                v = next(iter(form.values()))
                try:
                    data = json.loads(v)
                except Exception:
                    data = form
            else:
                data = form

    if not data:
        return jsonify({"error": "Payload vuoto"}), 400

    # Verifica status: vogliamo solo ordini pagati / in lavorazione o completati
    status = (data.get("status") or "").lower()
    if status not in ["processing", "completed", "on-hold", "paid"]:
        return jsonify({"status": "ignored"}), 200

    # Estrazione campi utili (proviamo sia shipping che billing)
    order_id = str(data.get("id") or data.get("number") or data.get("order_id") or "")
    shipping = data.get("shipping") or data.get("billing") or {}

    # Se shipping è stringa (a volte succede), proviamo a parsarla
    if isinstance(shipping, str):
        try:
            shipping = json.loads(shipping)
        except Exception:
            shipping = {}

    city = shipping.get("city", "")
    postcode = shipping.get("postcode", "") or shipping.get("zip", "")
    country = shipping.get("country", "")
    customer_name = (data.get("billing") or {}).get("first_name", "") + " " + (data.get("billing") or {}).get("last_name", "")
    service = data.get("shipping_lines", "") or data.get("service", "") or "APC Priority DDU"

    # created_at se presente da WooCommerce (date_created), altrimenti now
    created_at = data.get("date_created") or data.get("created_at")
    if created_at:
        parsed = parse_datetime_iso(created_at)
        if parsed:
            created_at_iso = parsed.isoformat()
        else:
            created_at_iso = now_utc().isoformat()
    else:
        created_at_iso = now_utc().isoformat()

    unique_id = str(uuid.uuid4())[:8]
    tracking_link = make_tracking_link(unique_id)

    # Salvataggio su Google Sheet
    # Intestazione prevista (assicurati che il tuo foglio abbia in prima riga questi nomi, o usa indici)
    # Order ID | Tracking Link | Created At | Service | Country | City | Postcode | Customer
    try:
        sheet.append_row([order_id, tracking_link, created_at_iso, service, country, city, postcode, customer_name])
    except Exception as e:
        # Se append_row fallisce, ritorna 500 con messaggio (vedi logs Render)
        return jsonify({"error": "Errore salvataggio su Google Sheet", "detail": str(e)}), 500

    # Risposta al webhook
    return jsonify({"status": "success", "tracking_link": tracking_link}), 200

# ---------- Endpoint API che fornisce la timeline completa ----------
@app.route("/api/track/<unique_id>", methods=["GET"])
def api_track(unique_id):
    # Leggi tutte le righe (get_all_records ritorna dict con header come chiavi)
    try:
        records = sheet.get_all_records()
    except Exception as e:
        return jsonify({"error": "Errore lettura Sheet", "detail": str(e)}), 500

    # Cerco la riga il cui campo Tracking Link contiene unique_id
    found = None
    for r in records:
        link = r.get("Tracking Link") or r.get("tracking_link") or r.get("Link Tracking") or r.get("tracking")
        if not link:
            # prova le colonne per compatibilità
            for v in r.values():
                if isinstance(v, str) and unique_id in v:
                    found = r
                    break
        else:
            if unique_id in str(link):
                found = r
        if found:
            break

    if not found:
        return jsonify({"error": "Tracking ID non trovato"}), 404

    # Estraggo i campi dalla riga (usa fallback su nomi diversi)
    order_id = found.get("Order ID") or found.get("order_id") or found.get("Numero Ordine") or found.get("order")
    created_at_raw = found.get("Created At") or found.get("created_at") or found.get("Data Ordine") or found.get("created")
    service = found.get("Service") or found.get("service") or found.get("Servizio") or "APC Priority DDU"
    country = found.get("Country") or found.get("country") or found.get("Paese") or ""
    city = found.get("City") or found.get("city") or found.get("Città") or ""
    postcode = found.get("Postcode") or found.get("postcode") or found.get("CAP") or ""
    customer = found.get("Customer") or found.get("customer") or found.get("Cliente") or ""

    created_dt = parse_datetime_iso(created_at_raw) or now_utc()
    days_passed = (now_utc() - created_dt).days
    is_delivered = days_passed >= 21
    status_text = "CONSEGNATO" if is_delivered else ("IN TRANSITO" if days_passed > 3 else "IN PREPARAZIONE")

    est_start = (created_dt + timedelta(days=19)).date().isoformat()
    est_end = (created_dt + timedelta(days=21)).date().isoformat()

    # Costruisco events a partire dal template
    events = []
    for ev in EVENTS_TEMPLATE:
        ev_date = created_dt + timedelta(days=ev["day"])
        loc = ev["loc"]
        # Sostituisco il luogo finale con CAP + Paese se disponibili
        if ev["title"] == "CONSEGNATO":
            loc = f"{postcode} {country}".strip()
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

# ---------- Route HTML minimale per debug / compatibilità (opzionale) ----------
@app.route("/track/<unique_id>")
def track_html(unique_id):
    # Per compatibilità manteniamo una pagina semplice che fa fetch a /api/track e visualizza JSON
    api_url = f"/api/track/{unique_id}"
    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>Tracking {unique_id}</title></head>
      <body style="font-family:Arial,Helvetica,sans-serif; background:#f2f4f7; padding:20px;">
        <div style="max-width:800px;margin:20px auto;background:#fff;padding:20px;border-radius:8px;">
          <h2>Tracking ID: {unique_id}</h2>
          <div id="content">Caricamento...</div>
        </div>
        <script>
          fetch("{api_url}").then(r=>r.json()).then(j=>{
            document.getElementById('content').innerText = JSON.stringify(j, null, 2);
          }).catch(e=>{
            document.getElementById('content').innerText = "Errore: " + e;
          });
        </script>
      </body>
    </html>
    """
    return html

# ---------- Run ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
