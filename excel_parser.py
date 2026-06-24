"""
Parse les exports Excel Crédit Mutuel (format natif .xlsx).
Retourne une liste de Transaction compatibles avec tresorerie_analyzer.
"""
import re
import pandas as pd
from parser import Transaction


def parse_excel_statement(filepath: str) -> list[Transaction]:
    """Lit un fichier Excel Crédit Mutuel et retourne les transactions."""
    xl = pd.read_excel(filepath, sheet_name=None, header=None)

    transactions = []
    for sheet_name, df in xl.items():
        # Chercher la ligne d'en-tête "Date / Valeur / Libellé / Débit / Crédit"
        header_row = None
        for i, row in df.iterrows():
            vals = [str(v).strip().lower() for v in row if pd.notna(v)]
            if any("date" in v for v in vals) and any("lib" in v for v in vals):
                header_row = i
                break

        if header_row is None:
            continue

        # Trouver les colonnes par leur position dans la ligne d'en-tête
        headers = [str(v).strip().lower() if pd.notna(v) else "" for v in df.iloc[header_row]]
        col_date   = next((i for i, h in enumerate(headers) if "date" in h and "valeur" not in h), None)
        col_label  = next((i for i, h in enumerate(headers) if "lib" in h), None)
        col_debit  = next((i for i, h in enumerate(headers) if "débit" in h or "debit" in h), None)
        col_credit = next((i for i, h in enumerate(headers) if "crédit" in h or "credit" in h), None)

        if col_date is None or col_label is None:
            continue

        for i in range(header_row + 1, len(df)):
            row = df.iloc[i]

            raw_date = row.iloc[col_date] if col_date < len(row) else None
            raw_label = row.iloc[col_label] if col_label is not None and col_label < len(row) else None
            raw_debit  = row.iloc[col_debit]  if col_debit  is not None and col_debit  < len(row) else None
            raw_credit = row.iloc[col_credit] if col_credit is not None and col_credit < len(row) else None

            # Ignorer les lignes sans date valide
            if pd.isna(raw_date) or pd.isna(raw_label):
                continue
            if str(raw_label).strip().lower() in ("libellé", "nan", ""):
                continue

            # Formater la date
            try:
                if hasattr(raw_date, 'strftime'):
                    date_str = raw_date.strftime("%d/%m/%Y")
                else:
                    import datetime
                    d = pd.to_datetime(raw_date)
                    date_str = d.strftime("%d/%m/%Y")
            except Exception:
                continue

            label = str(raw_label).strip()
            if not label or label.lower() == "nan":
                continue

            # Débit : valeur négative dans le fichier CM → on prend l'abs
            debit = 0.0
            if raw_debit is not None and pd.notna(raw_debit):
                try:
                    v = float(raw_debit)
                    debit = abs(v) if v != 0 else 0.0
                except Exception:
                    pass

            # Crédit
            credit = 0.0
            if raw_credit is not None and pd.notna(raw_credit):
                try:
                    v = float(raw_credit)
                    credit = abs(v) if v != 0 else 0.0
                except Exception:
                    pass

            if debit == 0 and credit == 0:
                continue

            tx = Transaction(
                date=date_str,
                date_valeur=date_str,
                label=label,
                debit=debit if debit > 0 else None,
                credit=credit if credit > 0 else None,
                tva_status="pending",
                tva_rate=0.0,
                tva_amount=0.0,
                note="",
            )
            transactions.append(tx)

    return transactions
