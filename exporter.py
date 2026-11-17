"""SmartPOS data export. Builds Excel-friendly CSV files (or one ZIP of all of them) from smartpos.db.
Read-only: it never changes the database. Passwords and user accounts are never exported.
Python standard library only."""
# =====================================================================================================
# Builds the Excel-friendly files on the admin Export page. It only READS the database.
# Passwords and staff accounts are never exported. Each file starts with a marker (utf-8-sig) so Excel reads accents correctly.
# =====================================================================================================
import csv, io, re, zipfile
from datetime import datetime

# Money columns are formatted inside the SQL query (cents to "65.50") so there are no rounding errors.
KES = "printf('%d.%02d', {0}/100, {0}%100)"  # cents -> "65.50" (done in SQL so no rounding errors)


def _m(col):
    return KES.format(col)


# Turns a score into Low / Medium / High / Critical with the same boundaries as the fraud engine.
def _level(expr):  # same bands as the fraud engine: Low < 30, Medium < 60, High < 80, Critical
    return f"CASE WHEN {expr}<30 THEN 'Low' WHEN {expr}<60 THEN 'Medium' WHEN {expr}<80 THEN 'High' ELSE 'Critical' END"


_RISK = "MIN(100, SUM(CASE WHEN status<>'legitimate' THEN score ELSE 0 END))"  # findings a manager cleared do not count


# name -> (columns shown in the file, SQL, date column used for the From/To filter or None)
# THE LIST OF EXPORTS. Each entry is:  name: (column headings, the SQL that fetches the rows, the date column the From/To dates filter on).
# {w} in the SQL is replaced by the date filter. TO ADD A NEW EXPORT: add an entry here and a button for it in index.html (the 'export' tab).
DATASETS = {
    "sales": (
        ["Receipt", "Date/Time", "Cashier", "Payment", "Payment ref", "Total (KES)", "Received (KES)", "Change (KES)"],
        f"SELECT receipt_no, created_at, cashier_name, method, payment_ref, {_m('total_cents')}, {_m('tendered_cents')}, {_m('change_cents')} "
        "FROM sales {w} ORDER BY created_at, rowid",
        "created_at"),
    "sale_items": (
        ["Receipt", "Date/Time", "Product", "Category", "Qty", "Unit price (KES)", "Unit cost (KES)", "Line total (KES)", "Profit (KES)"],
        f"SELECT s.receipt_no, s.created_at, i.product_name, i.category, i.qty, {_m('i.unit_price_cents')}, {_m('i.unit_cost_cents')}, "
        f"{_m('i.line_total_cents')}, {_m('(i.unit_price_cents - i.unit_cost_cents) * i.qty')} "
        "FROM sale_items i JOIN sales s ON s.id = i.sale_id {w} ORDER BY s.created_at, s.rowid, i.rowid",
        "s.created_at"),
    "products": (
        ["Product", "Category", "Price (KES)", "Cost (KES)", "In stock", "Reorder level", "Barcode", "Last updated"],
        f"SELECT name, category, {_m('price_cents')}, {_m('cost_cents')}, stock, reorder_level, barcode, updated_at FROM products ORDER BY rowid",
        None),
    "movements": (
        ["Date/Time", "Product", "Type", "Change", "Stock after", "Reason", "By"],
        "SELECT created_at, product_name, type, qty_change, stock_after, reason, user_name FROM stock_movements {w} ORDER BY created_at, rowid",
        "created_at"),
    "mpesa": (
        ["Date/Time", "Phone", "Amount (KES)", "Status", "M-Pesa receipt", "Note"],
        f"SELECT created_at, phone, {_m('amount_cents')}, status, receipt, result_desc FROM mpesa_payments {{w}} ORDER BY created_at, rowid",
        "created_at"),
    "audit": (
        ["Date/Time", "User", "Action", "Details"],
        "SELECT created_at, user_name, action, details FROM audit_log {w} ORDER BY created_at, rowid",
        "created_at"),
    "mpesa_manual": (  # payments the cashier confirmed by hand when there was no internet: compare them with the M-Pesa statement
        ["Time", "Cashier", "Receipt", "M-Pesa code", "Phone", "Amount (KES)", "Taken while the system was online", "Status", "Checked by", "Checked on", "Note"],
        "SELECT m.created_at, u.name, s.receipt_no, m.code, m.phone, " + _m("m.amount_cents") + ", CASE m.online_when_taken WHEN 1 THEN 'yes' ELSE 'no' END, "
        "m.status, m.checked_by, m.checked_at, m.note FROM mpesa_manual m JOIN users u ON u.id=m.cashier_id JOIN sales s ON s.id=m.sale_id {w} ORDER BY m.created_at",
        "m.created_at"),
    "fraud": (  # the date range applies to when the suspicious activity happened
        ["Date/Time of activity", "Found on", "Person", "Rule", "Finding", "Score", "Risk level", "Reason", "Status", "Decided by", "Decided on", "Manager note"],
        "SELECT event_time, created_at, subject, rule, title, score, " + _level("score") + ", reason, status, reviewed_by, reviewed_at, note "
        "FROM fraud_events {w} ORDER BY event_time, rowid",
        "event_time"),
    "fraud_summary": (
        ["Person", "Findings", "Risk score (max 100, cleared findings excluded)", "Risk level", "Still to review", "Legitimate", "Policy violation",
         "Suspected fraud", "Inconclusive"],
        "SELECT subject, COUNT(*), " + _RISK + ", " + _level(_RISK) + ", SUM(status IN ('new','investigating')), SUM(status='legitimate'), "
        "SUM(status='policy_violation'), SUM(status='suspected_fraud'), SUM(status='inconclusive') FROM fraud_events {w} GROUP BY subject ORDER BY 3 DESC, subject",
        "event_time"),
}

# Dates must look like 2026-11-30.
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Accepts a blank or a real YYYY-MM-DD date; anything else (like 2026-13-45) is refused.
def _clean_date(v):
    v = (v or "").strip()
    if not v:
        return ""
    if not DATE.match(v):
        raise ValueError("bad date")
    datetime.strptime(v, "%Y-%m-%d")  # rejects things like 2026-13-45
    return v


# Protects Excel users: text starting with = + - @ could run as a formula, so it gets a leading apostrophe.
def _safe(v):
    """Stop Excel running text as a formula (a cell starting with = + - @ can be an attack). Numbers are left alone."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


# Runs one export's SQL with the From/To dates applied.
def _rows(c, name, frm, to):
    cols, sql, dcol = DATASETS[name]
    where, args = "", []
    if dcol and (frm or to):
        parts = []
        if frm:
            parts.append(f"substr({dcol},1,10) >= ?")
            args.append(frm)
        if to:
            parts.append(f"substr({dcol},1,10) <= ?")
            args.append(to)
        where = "WHERE " + " AND ".join(parts)
    return cols, c.execute(sql.replace("{w}", where), args).fetchall()


# Builds the CSV file's bytes.
def _csv_bytes(cols, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow([_safe(v) for v in tuple(r)])
    return out.getvalue().encode("utf-8-sig")  # the BOM makes Excel read the file as UTF-8


# Adds the chosen dates to the file name.
def _span(frm, to):
    return ("_" + (frm or "start") + "_to_" + (to or "now")) if (frm or to) else ""


# The function server.py calls. Returns (file name, file bytes, type, number of rows), or None for a bad name or dates.
def run(c, name, query):
    """name: a dataset name or 'all'. query: dict like {'from': ['2026-11-01'], 'to': [...]}.
    Returns (filename, bytes, content_type, rows_exported), or None for an unknown name / bad date."""
    try:
        frm, to = _clean_date((query.get("from") or [""])[0]), _clean_date((query.get("to") or [""])[0])
    except ValueError:
        return None
    if frm and to and frm > to:
        return None
    stamp, span = datetime.now().strftime("%Y%m%d_%H%M"), _span(frm, to)
    if name == "all":
        buf, total = io.BytesIO(), 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n in DATASETS:
                cols, rows = _rows(c, n, frm, to)
                z.writestr(n + ".csv", _csv_bytes(cols, rows))
                total += len(rows)
        return f"smartpos_export{span}_{stamp}.zip", buf.getvalue(), "application/zip", total
    if name not in DATASETS:
        return None
    cols, rows = _rows(c, name, frm, to)
    return f"smartpos_{name}{span}_{stamp}.csv", _csv_bytes(cols, rows), "text/csv; charset=utf-8", len(rows)
