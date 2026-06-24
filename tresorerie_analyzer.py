"""
Analyse les relevés bancaires (6 mois) pour détecter les dépenses récurrentes
et produire une prévision de trésorerie mois suivant.
"""
import re
from collections import defaultdict
from datetime import datetime
from parser import parse_pdf_reliable, Transaction

# Patterns de normalisation pour regrouper les libellés similaires
NORMALIZE_PATTERNS = [
    (r"ORANGE\s+SA.*", "Orange (abonnement fibre)"),
    (r"MALAKOFF\s+HUMANIS.*", "Malakoff Humanis (retraite)"),
    (r"URSSAF.*", "URSSAF (cotisations)"),
    (r"VIR\s+INST\s+SALAIRE?S?\s*.*", "Salaires"),
    (r"VIR\s+INST\s+AVRIL|VIR\s+INST\s+MAI|VIR\s+INST\s+JUIN|VIR\s+INST\s+JUIL|VIR\s+INST\s+AOUT|VIR\s+INST\s+SEPT|VIR\s+INST\s+OCT|VIR\s+INST\s+NOV|VIR\s+INST\s+DEC|VIR\s+INST\s+JANV|VIR\s+INST\s+FEV|VIR\s+INST\s+MARS", "Salaires"),
    (r"DGFIP|FINANCES\s+PUBLIQUES|TVA1-", "DGFIP (TVA / impôts)"),
    (r"MMA\s+IARD", "MMA (assurance)"),
    (r"OPENAI|CHATGPT", "OpenAI / ChatGPT"),
    (r"MICROSOFT", "Microsoft"),
    (r"ADOBE", "Adobe"),
    (r"CANVA", "Canva"),
    (r"CAPCUT", "CapCut"),
    (r"CYBEARY", "Cybeary (stockage)"),
    (r"TASKER", "Tasker"),
    (r"AUTOROUTES|VINCI\s+AUTOROUTES", "Autoroutes (péages)"),
    (r"LIONS\s+CLUB", "Lions Club (cotisation)"),
    (r"AMHAPPY", "Amhappy"),
    (r"WIX", "Wix"),
    (r"AIRBNB", "Airbnb"),
    (r"SNCF|LMW\*SNCF", "SNCF"),
    (r"INPI|COURBEVOIE.*INPI|INPI.*VADS", "INPI"),
    (r"SAGGART|ADOBE", "Adobe"),
    (r"DUBLIN|MICROSOFT|MSBILL", "Microsoft"),
    (r"APRIL\s+SANTE|APRIL\s+PRE", "April Santé (mutuelle)"),
]

# Virements clients (revenus) à exclure des débits récurrents
# Ces entrées sont des encaissements clients trackés dans Toggl
REVENUE_PATTERNS = [
    r"DREAM\s*ACT",
    r"MEET\s*YOUR\s*MARKET",
    r"JOIA\s*TIME",
    r"AGENCE\s*TAPIS\s*ROUGE",
    r"GRAVELAT",
    r"KATANA",
    r"CLJ\s*BUSINESS",
    r"LYDIA\s*SOLUTIONS",
    r"APR\s*MOTOR",
    r"GENICADO",
]

def is_revenue(label: str) -> bool:
    """Retourne True si ce libellé est un encaissement client (à exclure des débits)."""
    up = label.upper()
    return any(re.search(p, up) for p in REVENUE_PATTERNS)

def normalize_label(label: str) -> str:
    """Normalise un libellé pour regrouper les transactions similaires."""
    # Chercher dans le libellé COMPLET (base + marchand après |)
    full_up = label.upper()
    for pattern, name in NORMALIZE_PATTERNS:
        if re.search(pattern, full_up):
            return name
    # Fallback : utiliser la partie avant | pour le nom affiché
    base = label.split(' | ')[0].strip()
    up = base.upper()
    clean = re.sub(r'\b(PRLV|SEPA|VIR|INST|PAIEMENT|CB|SA|SAS|SARL)\b', '', up)
    clean = re.sub(r'\d{4,}', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:50] if clean else base[:50]


def parse_date(tx: Transaction):
    try:
        return datetime.strptime(tx.date, "%d/%m/%Y")
    except Exception:
        return None


def analyze_cash_flow(file_paths: list[str]) -> dict:
    """
    Analyse plusieurs relevés (PDF ou Excel .xlsx) et retourne :
    - recurring_debits  : dépenses récurrentes
    - recurring_credits : recettes récurrentes
    - monthly_summary   : [{month, total_debit, total_credit, net}]
    - tva_info          : {collected, deductible, net_due}
    """
    from excel_parser import parse_excel_statement

    all_txs: list[Transaction] = []
    for path in file_paths:
        try:
            if path.lower().endswith(('.xlsx', '.xls')):
                txs = parse_excel_statement(path)
            else:
                txs = parse_pdf_reliable(path)
            all_txs.extend(txs)
        except Exception:
            pass

    if not all_txs:
        return {"error": "Aucune transaction trouvée dans les relevés"}

    # Regrouper par mois
    by_month: dict[str, list[Transaction]] = defaultdict(list)
    for tx in all_txs:
        d = parse_date(tx)
        if d:
            key = f"{d.year}-{d.month:02d}"
            by_month[key].append(tx)

    months_sorted = sorted(by_month.keys())
    n_months = len(months_sorted)

    # ── Détection récurrences ────────────────────────────────────
    # Regrouper par libellé normalisé → {label: {month: [amounts]}}
    debit_groups:  dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    credit_groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    debit_days:    dict[str, list[int]] = defaultdict(list)
    credit_days:   dict[str, list[int]] = defaultdict(list)

    for tx in all_txs:
        d = parse_date(tx)
        if not d:
            continue
        month_key = f"{d.year}-{d.month:02d}"
        norm = normalize_label(tx.label)

        if tx.debit and tx.debit > 0 and not is_revenue(tx.label):
            debit_groups[norm][month_key].append(tx.debit)
            debit_days[norm].append(d.day)
        if tx.credit and tx.credit > 0:
            credit_groups[norm][month_key].append(tx.credit)
            credit_days[norm].append(d.day)

    def build_recurring(groups, days_map, threshold=None):
        # "Tous les mois" : présent dans au moins n_months-1 mois (tolérance 1 absence)
        if threshold is None:
            threshold = max(2, n_months - 1)
        result = []
        for label, months_data in groups.items():
            n_seen = len(months_data)
            if n_seen < threshold:
                continue

            # Agréger par mois (somme de toutes les entrées du même mois)
            monthly_totals = {m: sum(amts) for m, amts in months_data.items()}
            sorted_months = sorted(monthly_totals.keys())
            totals = [monthly_totals[m] for m in sorted_months]

            # Médiane comme montant de référence
            s = sorted(totals)
            mid = len(s) // 2
            median = (s[mid - 1] + s[mid]) / 2 if len(s) % 2 == 0 else s[mid]

            # Comptage des mois "stables" : dans ±30% du médian
            stable_months = sum(1 for t in totals if median * 0.70 <= t <= median * 1.30)
            if stable_months < threshold:
                continue  # montants trop variables → pas une vraie récurrence

            avg_day = round(sum(days_map[label]) / len(days_map[label]))

            result.append({
                "label":            label,
                "avg_amount":       round(sum(totals) / len(totals), 2),
                "estimated_amount": round(median, 2),  # médian = meilleure estimation
                "avg_day":          avg_day,
                "months_seen":      n_seen,
                "last_amount":      round(totals[-1], 2),
                "amounts":          [round(t, 2) for t in totals[-6:]],
            })
        result.sort(key=lambda x: x["estimated_amount"], reverse=True)
        return result

    recurring_debits  = build_recurring(debit_groups,  debit_days)
    recurring_credits = build_recurring(credit_groups, credit_days)

    # ── Résumé mensuel ───────────────────────────────────────────
    monthly_summary = []
    for month in months_sorted:
        txs = by_month[month]
        total_debit  = sum(tx.debit  for tx in txs if tx.debit)
        total_credit = sum(tx.credit for tx in txs if tx.credit)
        monthly_summary.append({
            "month":        month,
            "total_debit":  round(total_debit, 2),
            "total_credit": round(total_credit, 2),
            "net":          round(total_credit - total_debit, 2),
        })

    # ── TVA du dernier mois ──────────────────────────────────────
    # Collectée = crédits × 20/120 (TVA incluse sur encaissements)
    # Déductible = depuis les patterns connus
    from parser import EXEMPT_PATTERNS, DEDUCTIBLE_PATTERNS
    last_month = months_sorted[-1] if months_sorted else None
    tva_collected  = 0.0
    tva_deductible = 0.0

    if last_month:
        for tx in by_month[last_month]:
            if tx.credit:
                # Vérifier si pas exonéré
                is_exempt = any(re.search(p, tx.label.upper()) for p, _ in EXEMPT_PATTERNS)
                if not is_exempt:
                    tva_collected += tx.credit * 20 / 120

            if tx.debit:
                for pattern, rate, _ in DEDUCTIBLE_PATTERNS:
                    if re.search(pattern, tx.label.upper()):
                        tva_deductible += tx.debit * rate / (1 + rate)
                        break

    tva_net = round(tva_collected - tva_deductible, 2)

    return {
        "months_analyzed":  months_sorted,
        "n_months":         n_months,
        "recurring_debits": recurring_debits,
        "recurring_credits": recurring_credits,
        "monthly_summary":  monthly_summary,
        "tva_info": {
            "collected":   round(tva_collected, 2),
            "deductible":  round(tva_deductible, 2),
            "net_due":     tva_net,
            "month":       last_month or "",
        },
        "avg_monthly_debit":  round(sum(m["total_debit"]  for m in monthly_summary) / max(len(monthly_summary), 1), 2),
        "avg_monthly_credit": round(sum(m["total_credit"] for m in monthly_summary) / max(len(monthly_summary), 1), 2),
    }
