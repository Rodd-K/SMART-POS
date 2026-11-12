"""One-time import of the CSV files in data/ into smartpos.db.
Backs up data/ first, imports everything in one transaction, checks the totals,
and rolls back if anything does not match. Your CSV files are never changed."""
# One-time tool: copies the very early CSV data (data/*.csv) into the database. It backs up data/ first, imports everything in ONE
# transaction, checks the counts and money totals, and cancels the whole import if anything does not match. Run it once: python3 migrate_csv.py
import csv, os, shutil, sys
from datetime import datetime
from decimal import Decimal

import db

DATA = os.environ.get("SMARTPOS_DATA", os.path.join(db.ROOT, "data"))
cents = lambda v: int((Decimal(str(v or 0)) * 100).to_integral_value())


# Reads one CSV file as a list of rows.
def rows(name):
    p = os.path.join(DATA, name + ".csv")
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# Inserts many rows into one table.
def put(c, table, cols, values):
    c.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", values)


# Backup, import, verify, done. It refuses to run if the database already has data.
def main():
    if not os.path.isdir(DATA):
        sys.exit(f"No data folder found at {DATA}")
    c = db.init()
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        sys.exit("The database already has data, so nothing was imported. Delete smartpos.db first if you want to start over.")
    backup = DATA.rstrip("/") + "_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copytree(DATA, backup)
    print("CSV backup saved to", backup)

    U, P, S, I, M, A, X = (rows(n) for n in ["users", "products", "sales", "sale_items", "movements", "audit", "mpesa"])
    with c:  # one transaction: everything is imported, or nothing is
        put(c, "users", "id username name role salt hash active created_at".split(),
            [(r["id"], r["username"], r["name"], r["role"], r["salt"], r["hash"], 1, r["created_at"]) for r in U])
        put(c, "products", "id name category price_cents cost_cents stock reorder_level barcode updated_at".split(),
            [(r["id"], r["name"], r["category"], cents(r["price"]), cents(r["cost"]), int(r["stock"]), int(r["reorder_level"]),
              r.get("barcode", ""), r["updated_at"]) for r in P])
        put(c, "sales", "id receipt_no client_ref created_at cashier_id cashier_name method payment_ref total_cents tendered_cents change_cents".split(),
            [(r["id"], r["receipt_no"], r["client_ref"], r["created_at"], r["cashier_id"], r["cashier_name"], r["method"],
              r["payment_ref"], cents(r["total"]), cents(r["tendered"]), cents(r["change"])) for r in S])
        put(c, "sale_items", "id sale_id product_id product_name category qty unit_price_cents unit_cost_cents line_total_cents".split(),
            [(r["id"], r["sale_id"], r["product_id"], r["product_name"], r["category"], int(r["qty"]), cents(r["unit_price"]),
              cents(r["unit_cost"]), cents(r["line_total"])) for r in I])
        put(c, "stock_movements", "id created_at product_id product_name type qty_change stock_after reason user_name".split(),
            [(r["id"], r["created_at"], r["product_id"], r["product_name"], r["type"], int(r["qty_change"]), int(r["stock_after"]),
              r["reason"], r["user_name"]) for r in M])
        put(c, "audit_log", "id created_at user_name action details".split(),
            [(r["id"], r["created_at"], r["user_name"], r["action"], r["details"]) for r in A])
        put(c, "mpesa_payments", "id created_at client_ref cashier_id phone amount_cents items_json checkout_request_id merchant_request_id status result_desc receipt".split(),
            [(r["id"], r["created_at"], r["client_ref"], r["cashier_id"], r["phone"], int(r["amount"]) * 100, r["items"],
              r["checkout_request_id"], r["merchant_request_id"], r["status"], r["result_desc"], r["receipt"]) for r in X])

        # Verification: counts and money must match the CSV files exactly.
        n = lambda t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        one = lambda q: c.execute(q).fetchone()[0] or 0
        checks = [
            ("users", len(U), n("users")), ("products", len(P), n("products")), ("sales", len(S), n("sales")),
            ("sale items", len(I), n("sale_items")), ("stock movements", len(M), n("stock_movements")),
            ("audit entries", len(A), n("audit_log")), ("M-Pesa payments", len(X), n("mpesa_payments")),
            ("total sales (cents)", sum(cents(r["total"]) for r in S), one("SELECT SUM(total_cents) FROM sales")),
            ("units in stock", sum(int(r["stock"]) for r in P), one("SELECT SUM(stock) FROM products")),
            ("sale items total (cents)", sum(cents(r["line_total"]) for r in I), one("SELECT SUM(line_total_cents) FROM sale_items")),
        ]
        bad = False
        print(f"\n{'Check':28}{'CSV':>12}{'Database':>12}")
        for name, a, b in checks:
            ok = a == b
            bad |= not ok
            print(f"{name:28}{a:>12}{b:>12}   {'OK' if ok else 'MISMATCH'}")
        problems = c.execute("PRAGMA foreign_key_check").fetchall()
        if problems or bad:
            sys.exit("\nSomething did not match, so NOTHING was imported. Your CSV files are untouched. Send me this output.")
    print("\nIntegrity check:", c.execute("PRAGMA integrity_check").fetchone()[0])
    print("Done. Your data is now in", db.DB_PATH)


if __name__ == "__main__":
    main()
