import re
import pdfplumber
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class Transaction:
    date: str
    date_valeur: str
    label: str
    debit: Optional[float]
    credit: Optional[float]
    tva_status: str = "unknown"
    tva_amount: float = 0.0
    tva_rate: float = 0.0
    note: str = ""


# Patterns for debit classification — order matters (first match wins)
EXEMPT_PATTERNS = [
    (r"SALAIRE",                         "Salaire — exonéré TVA"),
    (r"URSSAF",                          "Cotisations sociales — exonéré TVA"),
    (r"MALAKOFF",                        "Mutuelle/retraite — exonéré TVA"),
    (r"MMA\s*IARD",                      "Assurance — exonéré TVA"),
    (r"FINANCES PUBLIQUES|DGFIP|TVA1-",  "Paiement impôts/TVA — exonéré"),
    (r"FRAIS PAIE CB",                   "Frais bancaires — exonéré TVA"),
    (r"SNCF|LMW\*SNCF",                 "SNCF — exonéré TVA (transport de personnes)"),
]

DEDUCTIBLE_PATTERNS = [
    (r"OPENAI|CHATGPT",         0.20, "OpenAI / ChatGPT — TVA déductible 20%"),
    (r"MICROSOFT",              0.20, "Microsoft — TVA déductible 20%"),
    (r"ADOBE",                  0.20, "Adobe — TVA déductible 20%"),
    (r"CANVA",                  0.20, "Canva — TVA déductible 20%"),
    (r"CAPCUT",                 0.20, "CapCut — TVA déductible 20%"),
    (r"ORANGE",                 0.20, "Orange pro — TVA déductible 20%"),
    (r"CYBEARY",                0.20, "Stockage cloud — TVA déductible 20%"),
    (r"AUTOROUTES",             0.20, "Péages — TVA déductible 20%"),
    (r"AIRBNB",                 0.10, "Hébergement — TVA déductible 10%"),
    (r"INPI",                   0.20, "INPI — TVA déductible 20%"),
    (r"TASKER",                 0.20, "Tasker — TVA déductible 20%"),
]


def parse_amount(s: str) -> Optional[float]:
    """Parse European number format: 1.509,40 → 1509.40"""
    if not s:
        return None
    s = s.strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def classify_debit(label: str) -> tuple:
    """Returns (status, note, tva_rate)"""
    upper = label.upper()
    for pattern, note in EXEMPT_PATTERNS:
        if re.search(pattern, upper):
            return "exempt", note, 0.0
    for pattern, rate, note in DEDUCTIBLE_PATTERNS:
        if re.search(pattern, upper):
            return "deductible", note, rate
    return "pending", "À valider — classification inconnue", 0.20


def _apply_tva(tx: Transaction) -> Transaction:
    """Calcule le statut et montant TVA selon débit/crédit."""
    if tx.credit is not None:
        tx.tva_status = "collected"
        tx.tva_rate = 0.20
        tx.tva_amount = round(tx.credit * 20 / 120, 2)
        tx.note = "Encaissement client — TVA collectée 20%"
    elif tx.debit is not None:
        status, note, rate = classify_debit(tx.label)
        tx.tva_status = status
        tx.tva_rate = rate
        tx.note = note
        if status == "deductible":
            tx.tva_amount = round(tx.debit * rate / (1 + rate), 2)
    else:
        tx.tva_status = "unknown"
        tx.note = "Montant non détecté — à vérifier"
    return tx


AMOUNT_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
DATE_RE   = re.compile(r"^\d{2}/\d{2}/\d{4}$")
NOISE_RE  = re.compile(
    r"^(SOLDE|Total|Page|Sous réserve|Information|CAISSE|Pour toute|Médiateur|"
    r"TVA intra|www\.|ICS\s*:|RUM\s*:|BQE|UR\s+|PK|MD0|B9F|SCTINST|"
    r"DIJUKK|EXMWQ|VU6|2C2|G033|NN9|220|Vous disposez|Retrouvez|"
    r"\(GE\)|\(GD\)|<<|0 820|FAX |BIC :|15489|04854|AGENCE SYM)",
    re.IGNORECASE,
)


def _group_words_by_row(words: list, y_tol: float = 4.0) -> list:
    """Regroupe les mots par ligne (même y approximatif)."""
    if not words:
        return []
    rows = []
    words_sorted = sorted(words, key=lambda w: w["top"])
    current_row = [words_sorted[0]]
    for w in words_sorted[1:]:
        if abs(w["top"] - current_row[-1]["top"]) <= y_tol:
            current_row.append(w)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [w]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def parse_pdf_reliable(filepath: str) -> list[Transaction]:
    """
    Parse un relevé Crédit Mutuel PDF en utilisant les coordonnées X
    pour distinguer la colonne Débit de la colonne Crédit.
    """
    transactions = []

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words:
                continue

            rows = _group_words_by_row(words)

            # ── Trouver les x-centres des colonnes Débit / Crédit ──────────
            debit_x_center  = None
            credit_x_center = None
            for row in rows:
                texts = [w["text"] for w in row]
                row_text = " ".join(texts).upper()
                if "DÉBIT" in row_text or "DEBIT" in row_text:
                    for w in row:
                        t = w["text"].upper()
                        if t in ("DÉBIT", "DEBIT"):
                            debit_x_center = (w["x0"] + w["x1"]) / 2
                        elif t in ("CRÉDIT", "CREDIT"):
                            credit_x_center = (w["x0"] + w["x1"]) / 2
                    break

            # ── Regrouper les lignes par transaction (commence par date) ───
            tx_rows: list[list] = []   # liste de listes de rows
            for row in rows:
                texts = [w["text"] for w in row]
                # Une ligne de transaction commence par deux dates
                if len(texts) >= 2 and DATE_RE.match(texts[0]) and DATE_RE.match(texts[1]):
                    tx_rows.append([row])
                elif tx_rows:
                    # Continuation si pas du bruit
                    row_text = " ".join(texts)
                    if not NOISE_RE.match(row_text.strip()):
                        tx_rows[-1].append(row)

            # ── Parser chaque bloc transaction ─────────────────────────────
            for block in tx_rows:
                first_row = block[0]
                texts = [w["text"] for w in first_row]
                if len(texts) < 2:
                    continue

                date       = texts[0]
                date_val   = texts[1]

                # Tous les mots du bloc (hors dates)
                all_words_in_block = [w for row in block for w in row]
                amount_words = [w for w in all_words_in_block if AMOUNT_RE.match(w["text"])]
                label_words  = [w for w in all_words_in_block
                                if not DATE_RE.match(w["text"])
                                and not AMOUNT_RE.match(w["text"])]

                # Label = mots non-montant, triés par position
                label_parts = [w["text"] for w in sorted(label_words, key=lambda w: (w["top"], w["x0"]))]
                # Nettoyer les mots parasites (codes, références)
                label_clean = []
                for part in label_parts:
                    if re.match(r"^[A-Z0-9]{12,}$", part):  # code trop long → skip
                        continue
                    label_clean.append(part)
                label = " ".join(label_clean).strip()
                if not label:
                    label = " ".join(label_parts).strip()

                # Construire libellé avec séparateur | pour la partie marchand
                # (les 2 premières parties = type virement + nom principal)
                parts = label.split()
                if len(parts) > 5:
                    # Essayer de détecter une rupture marchand (ex: "| KATANA")
                    label = label  # garder tel quel pour l'instant

                # ── Assigner les montants à Débit ou Crédit ────────────────
                debit:  Optional[float] = None
                credit: Optional[float] = None

                if debit_x_center and credit_x_center and amount_words:
                    # Seuil = milieu entre les deux colonnes
                    mid_x = (debit_x_center + credit_x_center) / 2
                    for aw in amount_words:
                        ax = (aw["x0"] + aw["x1"]) / 2
                        v  = parse_amount(aw["text"])
                        if v is None:
                            continue
                        if ax < mid_x:
                            # Dans la colonne Débit
                            if debit is None or v > debit:
                                debit = v
                        else:
                            # Dans la colonne Crédit
                            if credit is None or v > credit:
                                credit = v
                else:
                    # Fallback : pas de colonnes détectées — on utilise l'ancien heuristic
                    vals = [parse_amount(w["text"]) for w in amount_words]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        debit = vals[-1]  # prudent : on met en débit par défaut

                tx = Transaction(
                    date=date,
                    date_valeur=date_val,
                    label=label,
                    debit=debit,
                    credit=credit,
                )
                _apply_tva(tx)
                transactions.append(tx)

    return transactions
