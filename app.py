import os
import json
import uuid
import re
import math
import requests as http_requests
from flask import Flask, request, jsonify, render_template, session
from werkzeug.utils import secure_filename
from parser import parse_pdf_reliable, Transaction
from toggl_parser import parse_toggl_pdf, DEFAULT_RATE, CLIENT_RATES, FORFAIT_CLIENTS

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


@app.route("/facturation/toggl", methods=["POST"])
def facturation_toggl():
    """Fetch Toggl report directly via API using user's token."""
    data = request.json
    token = data.get("token", "").strip()
    start_date = data.get("start_date")  # YYYY-MM-DD
    end_date = data.get("end_date")      # YYYY-MM-DD

    if not token or not start_date or not end_date:
        return jsonify({"error": "Token, start_date et end_date requis"}), 400

    auth = (token, "api_token")
    headers = {"Content-Type": "application/json"}

    # Get workspace ID
    try:
        me_res = http_requests.get("https://api.track.toggl.com/api/v9/me",
                                   auth=auth, headers=headers, timeout=10)
        if me_res.status_code == 403:
            return jsonify({"error": "Token invalide — vérifiez votre token API Toggl"}), 403
        me = me_res.json()
        workspace_id = me.get("default_workspace_id")
        if not workspace_id:
            workspaces = http_requests.get("https://api.track.toggl.com/api/v9/workspaces",
                                           auth=auth, headers=headers, timeout=10).json()
            workspace_id = workspaces[0]["id"]
    except Exception as e:
        return jsonify({"error": f"Erreur connexion Toggl: {str(e)}"}), 500

    from collections import defaultdict

    # Fetch clients map: id → name
    try:
        r = http_requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients",
                              auth=auth, headers=headers, timeout=10)
        raw = r.json() if r.text.strip() else []
        client_map = {c["id"]: c["name"].upper().strip() for c in (raw or [])}
    except Exception:
        client_map = {}

    # Fetch projects map: project_id → {client_id, name}
    try:
        r = http_requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects",
                              auth=auth, headers=headers, timeout=10)
        raw = r.json() if r.text.strip() else []
        project_map = {p["id"]: {"client_id": p.get("client_id"), "name": p.get("name", "")} for p in (raw or [])}
    except Exception:
        project_map = {}

    # Fetch all time entries for the period (free API)
    try:
        r = http_requests.get(
            "https://api.track.toggl.com/api/v9/me/time_entries",
            auth=auth, headers=headers, timeout=15,
            params={"start_date": start_date + "T00:00:00Z", "end_date": end_date + "T23:59:59Z"},
        )
        if r.status_code != 200:
            return jsonify({"error": f"Toggl a répondu HTTP {r.status_code} : {r.text[:200]}"}), 500
        entries = r.json() if r.text.strip() else []
    except Exception as e:
        return jsonify({"error": f"Erreur récupération entrées Toggl: {str(e)}"}), 500

    # Debug: show sample entry and maps
    sample_entry = entries[0] if entries else {}
    print("DEBUG client_map:", client_map)
    print("DEBUG project_map sample:", dict(list(project_map.items())[:3]))
    print("DEBUG sample entry:", {k: sample_entry.get(k) for k in ["description","project_id","duration","tags"]})

    # Group entries by client
    seconds_by_client: dict = defaultdict(int)
    tasks_by_client: dict = defaultdict(list)

    for entry in (entries or []):
        dur = entry.get("duration", 0)
        if dur < 0:  # still running
            continue
        pid = entry.get("project_id")
        proj = project_map.get(pid, {}) if pid else {}
        cid = proj.get("client_id")
        seconds_by_client[cid] += dur
        desc = (entry.get("description") or "").strip()
        if desc:
            h2, m2, s2 = dur // 3600, (dur % 3600) // 60, dur % 60
            tasks_by_client[cid].append({"description": desc, "duration": f"{h2:02d}:{m2:02d}:{s2:02d}"})

    # Build client billing list
    clients = []
    for cid, seconds in seconds_by_client.items():
        name = client_map.get(cid, f"Client #{cid}") if cid else "Sans client"
        name = re.sub(r'^CLIENT\s+', '', name)
        actual_h = seconds / 3600
        hh, mm, ss = seconds // 3600, (seconds % 3600) // 60, seconds % 60
        actual_duration = f"{hh:02d}:{mm:02d}:{ss:02d}"

        is_forfait = name in FORFAIT_CLIENTS
        if is_forfait:
            billed_h = FORFAIT_CLIENTS[name]["hours"]
            rate = FORFAIT_CLIENTS[name]["rate"]
        else:
            billed_h = math.ceil(actual_h) if actual_h > 0 else 0
            rate = CLIENT_RATES.get(name, DEFAULT_RATE)

        ht = round(billed_h * rate, 2)
        clients.append({
            "name": name,
            "actual_hours": round(actual_h, 4),
            "actual_duration": actual_duration,
            "billed_hours": float(billed_h),
            "rate": rate,
            "is_forfait": is_forfait,
            "ht": ht,
            "tva": round(ht * 0.20, 2),
            "ttc": round(ht * 1.20, 2),
            "tasks": tasks_by_client.get(cid, []),
        })

    clients.sort(key=lambda x: x["ht"], reverse=True)
    return jsonify({
        "clients": clients,
        "_debug": {
            "nb_entries": len(entries or []),
            "client_map": client_map,
            "project_sample": dict(list(project_map.items())[:5]),
            "sample_entry": {k: sample_entry.get(k) for k in ["description","project_id","duration","tags"]} if entries else {},
        }
    })


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
