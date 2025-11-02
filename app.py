from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import uuid
import gspread
from google.oauth2.service_account import Credentials
import os



app = Flask(__name__)
CORS(app) 

# --------------------------
# Configurazione Google Sheet
# --------------------------
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
CREDS = Credentials.from_service_account_file('credenziali.json', scopes=SCOPE)
CLIENT = gspread.authorize(CREDS)
SHEET = CLIENT.open_by_key("16v-pieF7pQt7GMoTnjknCV0XkWAlgDzLZs9SCycNSXI").sheet1

# --------------------------
# In-memory database
# --------------------------
orders = {}  # chiave: unique_id → dati ordine

# --------------------------
# Webhook WooCommerce
# --------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}

    # Solo ordini pagati
    if data.get("status") not in ["processing", "completed"]:
        return jsonify({"status": "ignored"}), 200

    order_id = str(data.get("id") or data.get("number") or "test-order")
    created_at = datetime.now().isoformat()
    unique_id = str(uuid.uuid4())[:8]
	tracking_link = f"https://tracking-backend-tb40.onrender.com/track/{unique_id}"  # cambia con dominio finale

    # Estrazione dati spedizione
    shipping = data.get("shipping", {})
    city = shipping.get("city", "")
    postcode = shipping.get("postcode", "")
    country = shipping.get("country", "")
    service = "APC Priority DDU"  # puoi prendere dal prodotto

    # Salvataggio nel Google Sheet
    SHEET.append_row([order_id, tracking_link, created_at, service, country, city, postcode])

    # Salvataggio in memoria per demo locale
    orders[unique_id] = {
        "order_id": order_id,
        "created_at": created_at,
        "service": service,
        "country": country,
        "city": city,
        "postcode": postcode
    }

    return jsonify({"status": "success", "link": tracking_link}), 200

# --------------------------
# Pagina di tracking
# --------------------------
@app.route('/track/<unique_id>')
def track(unique_id):
    order = orders.get(unique_id)
    if not order:
        return "<h1 style='text-align:center; color:red;'>Ordine non trovato</h1>", 404

    # Calcolo giorni passati e step
    created_date = datetime.fromisoformat(order["created_at"])
    days_passed = (datetime.now() - created_date).days
    is_delivered = days_passed >= 21
    step = 3 if is_delivered else 2 if days_passed > 10 else 1
    est_start = (created_date + timedelta(days=19)).strftime('%B %d, %Y')
    est_end = (created_date + timedelta(days=21)).strftime('%B %d, %Y')

    # Eventi simulati
    events = [
        ("Etichetta creata", 0, ""),
        ("Ordine arrivato APC", 1, "Bell, CA"),
        ("Processato APC", 1, "Bell, CA"),
        ("Ordine lasciato APC", 2, "Bell, CA"),
        ("In transito", 3, "Los Angeles, US"),
        ("Arrivo aeroporto", 4, "Los Angeles, US"),
        ("In viaggio verso Italia", 5, "New York, US"),
        ("Arrivato Italia", 7, "Milan, IT"),
        ("Dogana", 8, "Malpensa, IT"),
        ("Ufficio postale", 10, "IT"),
        ("Tentativo consegna", 12, "IT"),
        ("Tentativo consegna", 15, "IT"),
        ("Tentativo consegna", 18, "IT"),
        ("CONSEGNATO", 21, f"{order['postcode']} {order['country']}")
    ]

    shown = []
    for status, day, loc in events:
        if days_passed >= day:
            date_str = (created_date + timedelta(days=day)).strftime('%m/%d/%Y %I:%M %p UTC')
            shown.append(f"""
                <div style='margin:15px 0; padding:10px; border-left:4px solid #007bff; background:#f9f9f9;'>
                    <b>{status}</b><br>
                    <small>{date_str}<br>{loc}</small>
                </div>
            """)

    status_text = "CONSEGNATO" if is_delivered else "IN TRANSITO"

    html = f"""
    <div style="font-family:Arial; max-width:600px; margin:30px auto; text-align:center; background:white; padding:20px; border-radius:10px; box-shadow:0 0 10px #ccc;">
        <h1 style="color:#28a745;">{status_text}</h1>
        <p style="font-size:18px;">Step {step} of 3</p>
        <h3 style="color:#555;">Estimated Delivery: {est_start} - {est_end}</h3>
        <hr style="border:1px solid #eee;">
        <h3 style="text-align:left; color:#333;">Shipment Status:</h3>
        <div style="text-align:left;">{''.join(reversed(shown))}</div>
        <hr style="border:1px solid #eee;">
        <div style="text-align:left;">
            <b>Package ID:</b> {order['order_id']}<br>
            <b>Service:</b> {order['service']}<br>
            <b>Ship To:</b> {order['postcode']} {order['city']} {order['country']}
        </div>
        <div style="margin-top:20px;">
            <a href="/" style="color:#007bff; text-decoration:underline;">Track Another Package</a>
        </div>
    </div>
    """
    return html

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
