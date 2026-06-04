import re
import pdfplumber
from dataclasses import dataclass
from typing import Optional


@dataclass
class Transaction:
    date: str
    date_valeur: str
    label: str
    debit: Optional[float]
    credit: Optional[float]
    tva_status: str = "unknown"   # collected | deductible | exempt | pending | unknown
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
]

DEDUCTIBLE_PATTERNS = [
    (r"OPENAI|CHATGPT",         0.20, "OpenAI / ChatGPT — TVA déductible 20%"),
    (r"MICROSOFT",              0.20, "Microsoft — TVA déductible 20%"),
    (r"ADOBE",                  0.20, "Adobe — TVA déductible 20%"),
    (r"CANVA",                  0.20, "Canva — TVA déductible 20%"),
    (r"CAPCUT",                 0.20, "CapCut — TVA déductible 20%"),
    (r"ORANGE",                 0.20, "Orange pro — TVA déductible 20%"),
    (r"CYBEARY",                0.20, "Stockage cloud — TVA déductible 20%"),
    (r"SNCF|LMW\*SNCF",        0.10, "SNCF — TVA déductible 10%"),
    (r"AUTOROUTES",             0.20, "Péages — TVA déductible 20%"),
    (r"AIRBNB",                 0.10, "Hébergement — TVA déductible 10%"),
    (r"INPI",                   0.20, "INPI — TVA déductible 20%"),
    (r"TASKER",                 0.20, "Tasker — TVA déductible 20%"),
]

# Credits: wire transfers from clients (Crédit Mutuel puts them in Crédit column)
CREDIT_KEYWORDS = [
    "AGENCE TAPIS ROUGE", "CLJ BUSINESS", "LYDIA SOLUTIONS",
    "KATANA", "GRAVELAT FLEURY",
]


def parse_amount(s: str) -> Optional[float]:
    """Parse European number format: 1.509,40 → 1509.40"""
    if not s:
        return None
    s = s.strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        # European: period = thousands sep, comma = decimal
        s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def classify_debit(label: str) -> tuple[str, str, float]:
    """Returns (status, note, tva_rate)"""
    upper = label.upper()

    for pattern, note in EXEMPT_PATTERNS:
        if re.search(pattern, upper):
            return "exempt", note, 0.0

    for pattern, rate, note in DEDUCTIBLE_PATTERNS:
        if re.search(pattern, upper):
            return "deductible", note, rate

    return "pending", "À valider — classification inconnue", 0.20


def is_credit_label(label: str) -> bool:
    upper = label.upper()
    return any(kw in upper for kw in CREDIT_KEYWORDS)


def parse_pdf_reliable(filepath: str) -> list[Transaction]:
    transactions = []
    amount_re = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")
    date_line_re = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$")

    # Noise lines to skip when collecting continuation
    noise_re = re.compile(
        r"^(SOLDE|Total|Page|Sous réserve|Information|CAISSE|Pour toute|Médiateur|"
        r"TVA intra|www\.|ICS\s*:|RUM\s*:|BQE|UR\s+|PK|MD0|B9F|SCTINST|"
        r"DIJUKK|EXMWQ|VU6|2C2|G033|NN9|220|Vous disposez|Retrouvez|"
        r"\(GE\)|\(GD\)|<<|0 820|FAX |BIC :|15489|04854|AGENCE SYM)",
        re.IGNORECASE
    )

    with pdfplumber.open(filepath) as pdf:
        # Collect all lines across pages
        all_lines: list[str] = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                all_lines.extend(text.split("\n"))

    i = 0
    while i < len(all_lines):
        line = all_lines[i].strip()
        m = date_line_re.match(line)
        if not m:
            i += 1
            continue

        date, date_valeur, rest = m.groups()
        rest = rest.strip()

        # Collect continuation lines (merchant name, reference, etc.)
        extra_labels = []
        j = i + 1
        while j < len(all_lines):
            nl = all_lines[j].strip()
            if date_line_re.match(nl):
                break
            if nl and not noise_re.match(nl):
                extra_labels.append(nl)
            j += 1

        # Extract amounts from rest + continuation lines
        raw_amounts = amount_re.findall(rest)
        for el in extra_labels:
            raw_amounts.extend(amount_re.findall(el))

        parsed_amounts = [v for a in raw_amounts if (v := parse_amount(a)) is not None]

        # Build full label: base label (strip amounts) + first meaningful continuation
        base_label = amount_re.sub("", rest).strip()
        # Remove trailing currency codes like "USD", "EUR"
        base_label = re.sub(r"\s+[A-Z]{3}$", "", base_label).strip()

        # First continuation line that looks like a merchant name (not a reference code)
        merchant = ""
        for el in extra_labels:
            if not amount_re.search(el) and not re.match(r"^[A-Z0-9]{10,}$", el.strip()):
                candidate = el.strip()
                if len(candidate) > 3 and not re.match(r"^\d", candidate):
                    merchant = candidate
                    break

        full_label = f"{base_label} | {merchant}" if merchant else base_label

        # Determine debit/credit
        debit: Optional[float] = None
        credit: Optional[float] = None

        if is_credit_label(full_label):
            credit = parsed_amounts[0] if parsed_amounts else None
        else:
            if len(parsed_amounts) >= 2:
                # e.g. "20,00 USD  17,07" — last is the EUR amount
                debit = parsed_amounts[-1]
            elif parsed_amounts:
                debit = parsed_amounts[0]

        tx = Transaction(
            date=date,
            date_valeur=date_valeur,
            label=full_label,
            debit=debit,
            credit=credit,
        )

        if credit is not None:
            tx.tva_status = "collected"
            tx.tva_rate = 0.20
            tx.tva_amount = round(credit * 20 / 120, 2)
            tx.note = "Encaissement client — TVA collectée 20%"
        elif debit is not None:
            status, note, rate = classify_debit(full_label)
            tx.tva_status = status
            tx.tva_rate = rate
            tx.note = note
            if status == "deductible":
                tx.tva_amount = round(debit * rate / (1 + rate), 2)
        else:
            tx.tva_status = "unknown"
            tx.note = "Montant non détecté — à vérifier"

        transactions.append(tx)
        i = j  # skip continuation lines

    return transactions
