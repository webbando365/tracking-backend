import os
import json
import uuid
import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

# Configurazione Flask
app = Flask(__name__)
CORS(app)

# Configurazione Google Sheets tramite variabile d'ambiente
credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

if not credentials_json:
    raise Exception("Errore: variabile d'ambiente GOOGLE_APPLICATION_CREDENTIALS_JSON mancante")

creds_dict = json.loads(credentials_json)
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
client = gspread.authorize(creds)

# ID del foglio Google Sheet
SHEET_ID = "16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI"
SHEET = client.open_by_key(SHEET_ID).sheet1

# Endpoint di test
@app.route("/")
def home():
    return "Backend Tracking Ordini attivo!"

# Webhook per WooCommerce (ordine completato)
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Nessun JSON ricevuto"}), 400

    # Estraggo i dati principali
    order_id = data.get("id")
    customer_name = data.get("billing", {}).get("first_name", "") + " " + data.get("billing", {}).get("last_name", "")
    customer_email = data.get("billing", {}).get("email", "")
    order_date = data.get("date_created", datetime.datetime.now().isoformat())
    status = data.get("status", "")
    total = data.get("total", "")
    address = data.get("billing", {}).get("address_1", "")

    # Genero ID univoco per tracking
    unique_id = str(uuid.uuid4())[:8]
    tracking_link = f"https://tracking-backend-tb40.onrender.com/track/{unique_id}"  # sostituire con dominio finale

    # Salvo i dati nel Google Sheet
    SHEET.append_row([order_id, customer_name, customer_email, order_date, status, total, address, tracking_link])

    return jsonify({"success": True, "tracking_link": tracking_link})

# Endpoint di tracking (consultazione stato)
@app.route("/track/<tracking_id>", methods=["GET"])
def track(tracking_id):
    records = SHEET.get_all_records()
    for row in records:
        if tracking_id in row.get("tracking_link", ""):
            order_date_str = row.get("order_date") or row.get("data") or ""
            order_date = datetime.datetime.fromisoformat(order_date_str.replace("Z", ""))
            days_since = (datetime.datetime.now() - order_date).days

            # Simulazione stato spedizione in base ai giorni passati
            if days_since < 2:
                stato = "Ordine in preparazione"
            elif days_since < 5:
                stato = "Spedito"
            elif days_since < 7:
                stato = "In transito"
            else:
                stato = "Consegnato"

            return jsonify({
                "order_id": row.get("order_id"),
                "cliente": row.get("customer_name"),
                "email": row.get("customer_email"),
                "indirizzo": row.get("address"),
                "stato": stato,
                "giorni_trascorsi": days_since
            })

    return jsonify({"error": "Tracking ID non trovato"}), 404


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
