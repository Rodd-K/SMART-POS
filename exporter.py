"""SmartPOS data export. Builds Excel-friendly CSV files (or one ZIP of all of them) from smartpos.db.
Read-only: it never changes the database. Passwords and user accounts are never exported.
Python standard library only."""
import csv, io, re, zipfile
from datetime import datetime

KES = "printf('%d.%02d', {0}/100, {0}%100)"  # cents -> "65.50" (done in SQL so no rounding errors)


def _m(col):
    return KES.format(col)


# name -> (columns shown in the file, SQL, date column used for the From/To filter or None)
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
}

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_date(v):
    v = (v or "").strip()
    if not v:
        return ""
    if not DATE.match(v):
        raise ValueError("bad date")
    datetime.strptime(v, "%Y-%m-%d")  # rejects things like 2026-13-45
    return v


def _safe(v):
    """Stop Excel running text as a formula (a cell starting with = + - @ can be an attack). Numbers are left alone."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


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


def _csv_bytes(cols, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow([_safe(v) for v in tuple(r)])
    return out.getvalue().encode("utf-8-sig")  # the BOM makes Excel read the file as UTF-8


def _span(frm, to):
    return ("_" + (frm or "start") + "_to_" + (to or "now")) if (frm or to) else ""


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
