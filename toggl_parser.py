import re
import math
import pdfplumber

DEFAULT_RATE = 35.0
CLIENT_RATES = {"KATANA": 28.0}
FORFAIT_CLIENTS = {"GFA": {"hours": 9.0, "rate": DEFAULT_RATE}}


def duration_to_hours(h, m, s):
    return int(h) + int(m) / 60 + int(s) / 3600


def clean(line: str) -> str:
    """Replace null bytes with spaces and collapse whitespace."""
    return re.sub(r'\s+', ' ', line.replace('\x00', ' ')).strip()


def normalize_client(name: str) -> str:
    """Remove common prefixes like 'CLIENT ' to unify names."""
    return re.sub(r'^CLIENT\s+', '', name.strip().upper())


def parse_toggl_pdf(filepath: str) -> list[dict]:
    with pdfplumber.open(filepath) as pdf:
        raw_lines = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                raw_lines.extend(text.split("\n"))

    # ── Step 1: extract client totals from summary (page 1) ──────────────
    # Format: "CLIENT NAME PCT% | H MM SS"  (null bytes between H MM SS)
    clients: dict[str, dict] = {}
    summary_re = re.compile(
        r"([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇÒÍÁÚ][A-Za-zÀ-ÿÒÍÁÚ\s\-']+?)\s+([\d.]+)%\s*\|\s*(\d+)\s+(\d+)\s+(\d+)"
    )
    for raw in raw_lines:
        line = clean(raw)
        m = summary_re.search(line)
        if m:
            name = normalize_client(m.group(1))
            h, mi, s = m.group(3), m.group(4), m.group(5)
            hours = duration_to_hours(h, mi, s)
            clients[name] = {
                "actual_hours": hours,
                "actual_duration": f"{int(h):02d}:{int(mi):02d}:{int(s):02d}",
                "tasks": [],
            }

    # ── Step 2: extract tasks from breakdown section ──────────────────────
    # Client total line: "CLIENT NAME  N  H MM SS"  (N = nb entries)
    client_line_re = re.compile(
        r"^([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇÒÍÁÚ][A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇÒÍÁÚ\s\-']+?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$"
    )
    # Simple duration line at end: "description H MM SS"
    task_re = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")

    current_client = None
    in_breakdown = False

    for raw in raw_lines:
        line = clean(raw)
        if not line:
            continue
        if "Client and description breakdown" in line or "CLIENT | DESCRIPTION" in line:
            in_breakdown = True
            continue
        if not in_breakdown:
            continue
        # Skip page footers
        if re.match(r"AGENCE\s+SYM[EÉ]TRIE", line, re.I) or re.match(r"^Page\s+\d", line, re.I):
            continue
        if line == "TOTAL":
            current_client = None
            continue

        # Client total line?
        m = client_line_re.match(line)
        if m:
            name = normalize_client(m.group(1))
            h, mi, s = m.group(3), m.group(4), m.group(5)
            hours = duration_to_hours(h, mi, s)
            current_client = name
            if name not in clients:
                clients[name] = {
                    "actual_hours": hours,
                    "actual_duration": f"{int(h):02d}:{int(mi):02d}:{int(s):02d}",
                    "tasks": [],
                }
            continue

        # Task line?
        if current_client:
            m = task_re.match(line)
            if m:
                desc = m.group(1).strip()
                h, mi, s = m.group(2), m.group(3), m.group(4)
                # Skip aggregated lines (- or +)
                if desc in ("-", "+") or re.match(r"^[-+]\s", desc):
                    continue
                clients[current_client]["tasks"].append({
                    "description": desc,
                    "duration": f"{int(h):02d}:{int(mi):02d}:{int(s):02d}",
                })

    # ── Step 3: compute billing ───────────────────────────────────────────
    result = []
    for name, data in clients.items():
        actual_h = data["actual_hours"]
        is_forfait = name in FORFAIT_CLIENTS
        if is_forfait:
            billed_h = FORFAIT_CLIENTS[name]["hours"]
            rate = FORFAIT_CLIENTS[name]["rate"]
        else:
            billed_h = math.ceil(actual_h)  # always round up to full hour
            rate = CLIENT_RATES.get(name, DEFAULT_RATE)

        ht = round(billed_h * rate, 2)
        result.append({
            "name": name,
            "actual_hours": round(actual_h, 4),
            "actual_duration": data["actual_duration"],
            "billed_hours": round(billed_h, 4),
            "rate": rate,
            "is_forfait": is_forfait,
            "ht": ht,
            "tva": round(ht * 0.20, 2),
            "ttc": round(ht * 1.20, 2),
            "tasks": data["tasks"],
        })

    result.sort(key=lambda x: x["ht"], reverse=True)
    return result
