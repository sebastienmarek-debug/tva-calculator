import os
import json
import uuid
import re
from flask import Flask, request, jsonify, render_template, session
from werkzeug.utils import secure_filename
from parser import parse_pdf_reliable, Transaction
from toggl_parser import parse_toggl_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

UPLOAD_FOLDER = "/tmp/tva_uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# In-memory session store (for Railway single-instance)
SESSIONS: dict[str, list[dict]] = {}


def tx_to_dict(tx: Transaction, idx: int) -> dict:
    return {
        "id": idx,
        "date": tx.date,
        "date_valeur": tx.date_valeur,
        "label": tx.label,
        "debit": tx.debit,
        "credit": tx.credit,
        "tva_status": tx.tva_status,
        "tva_rate": tx.tva_rate,
        "tva_amount": tx.tva_amount,
        "note": tx.note,
    }


@app.route("/")
def home():
    return render_template("home.html")

@app.route("/tva")
def tva():
    return render_template("tva.html")

@app.route("/facturation")
def facturation():
    return render_template("facturation.html")

@app.route("/facturation/upload", methods=["POST"])
def facturation_upload():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".pdf"):
        return jsonify({"error": "Fichier PDF requis"}), 400
    path = os.path.join(UPLOAD_FOLDER, secure_filename(f.filename))
    f.save(path)
    try:
        clients = parse_toggl_pdf(path)
    except Exception as e:
        return jsonify({"error": f"Erreur de parsing: {str(e)}"}), 500
    return jsonify({"clients": clients})


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier"}), 400

    f = request.files["file"]
    if not f.filename.endswith(".pdf"):
        return jsonify({"error": "Fichier PDF requis"}), 400

    filename = secure_filename(f.filename)
    path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(path)

    try:
        txs = parse_pdf_reliable(path)
    except Exception as e:
        return jsonify({"error": f"Erreur de parsing: {str(e)}"}), 500

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = [tx_to_dict(tx, i) for i, tx in enumerate(txs)]

    return jsonify({"session_id": session_id, "transactions": SESSIONS[session_id]})


@app.route("/validate", methods=["POST"])
def validate():
    data = request.json
    session_id = data.get("session_id")
    tx_id = data.get("id")
    new_status = data.get("tva_status")
    new_rate = float(data.get("tva_rate", 0.20))
    note = data.get("note", "")

    if session_id not in SESSIONS:
        return jsonify({"error": "Session inconnue"}), 404

    txs = SESSIONS[session_id]
    for tx in txs:
        if tx["id"] == tx_id:
            tx["tva_status"] = new_status
            tx["tva_rate"] = new_rate
            tx["note"] = note
            if new_status == "deductible" and tx["debit"]:
                tx["tva_amount"] = round(tx["debit"] * new_rate / (1 + new_rate), 2)
            elif new_status == "exempt":
                tx["tva_amount"] = 0.0
            elif new_status == "collected" and tx["credit"]:
                tx["tva_amount"] = round(tx["credit"] * new_rate / (1 + new_rate), 2)
            else:
                tx["tva_amount"] = 0.0
            return jsonify(tx)

    return jsonify({"error": "Transaction non trouvée"}), 404


@app.route("/summary/<session_id>")
def summary(session_id):
    if session_id not in SESSIONS:
        return jsonify({"error": "Session inconnue"}), 404

    txs = SESSIONS[session_id]
    tva_collected = sum(t["tva_amount"] for t in txs if t["tva_status"] == "collected")
    tva_deductible = sum(t["tva_amount"] for t in txs if t["tva_status"] == "deductible")
    tva_due = round(tva_collected - tva_deductible, 2)
    pending_count = sum(1 for t in txs if t["tva_status"] == "pending")

    return jsonify({
        "tva_collected": round(tva_collected, 2),
        "tva_deductible": round(tva_deductible, 2),
        "tva_due": tva_due,
        "pending_count": pending_count,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
