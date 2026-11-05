<<<<<<< HEAD
"""SmartPOS - local server on SQLite. Python standard library only. Run: python3 server.py -> http://localhost:8000"""
import os, sys, json, uuid, hashlib, secrets
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import db
import mpesa
import exporter

ROOT = os.path.dirname(os.path.abspath(__file__))
TOKENS = {}
uid = lambda: str(uuid.uuid4())
now = lambda: datetime.now().isoformat(timespec="seconds")
cents = lambda v: int((Decimal(str(v)) * 100).to_integral_value())  # "65.50" -> 6550
money = lambda c: "%d.%02d" % divmod(c, 100)                         # 6550 -> "65.50"


def new_conn():
    c = db.connect()
    c.isolation_level = None  # we control transactions ourselves
    return c


@contextmanager
def tx(c):
    """All-or-nothing block. BEGIN IMMEDIATE lets only one writer in at a time, so stock checks cannot race."""
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
=======
"""SmartPOS - local server. Standard library only. Run: python3 server.py  ->  http://localhost:8000"""
import csv, os, json, uuid, hashlib, secrets, threading
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
LOCK = threading.RLock()
TOKENS = {}
Q = Decimal("0.01")
F = {
    "users": ["id", "username", "name", "role", "salt", "hash", "created_at"],
    "products": ["id", "name", "category", "price", "cost", "stock", "reorder_level", "barcode", "updated_at"],
    "sales": ["id", "receipt_no", "client_ref", "created_at", "cashier_id", "cashier_name", "method", "payment_ref", "total", "tendered", "change"],
    "sale_items": ["id", "sale_id", "product_id", "product_name", "category", "qty", "unit_price", "unit_cost", "line_total"],
    "movements": ["id", "created_at", "product_id", "product_name", "type", "qty_change", "stock_after", "reason", "user_name"],
    "audit": ["id", "created_at", "user_name", "action", "details"],
}

D = lambda x: Decimal(str(x))
M = lambda x: str(x.quantize(Q))
uid = lambda: str(uuid.uuid4())
now = lambda: datetime.now().isoformat(timespec="seconds")
path = lambda n: os.path.join(DATA, n + ".csv")
# Stop spreadsheet apps from running text like "=1+1" as a formula.
safe = lambda s: "'" + s if s and s[0] in "=+-@" else s


def read(n):
    with LOCK, open(path(n), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(n, rows):
    with LOCK:
        tmp = path(n) + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=F[n])
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path(n))


def append(n, *rows):
    with LOCK, open(path(n), "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=F[n]).writerows(rows)
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c


def hp(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


<<<<<<< HEAD
def audit(c, u, action, details=""):
    c.execute("INSERT INTO audit_log VALUES(?,?,?,?,?)", (uid(), now(), u["name"], action, details))


def move(c, u, pid, pname, kind, change, after, reason):
    c.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?,?,?)", (uid(), now(), pid, pname, kind, change, after, reason, u["name"]))


def init():
    if not os.path.exists(db.DB_PATH) and os.path.exists(os.path.join(ROOT, "data", "users.csv")):
        sys.exit("Your data is still in the CSV files. Run  python3 migrate_csv.py  first, then start the server again.")
    c = new_conn()
    db.init()
    if not c.execute("SELECT 1 FROM users").fetchone():
        with tx(c):
            for un, nm, role, pw in [("admin", "Administrator", "admin", "admin123"), ("cashier", "Cashier One", "cashier", "cashier123")]:
                s = secrets.token_hex(8)
                c.execute("INSERT INTO users VALUES(?,?,?,?,?,?,1,?)", (uid(), un, nm, role, s, hp(pw, s), now()))
            demo = [("White Bread 400g", "Bakery", 65, 48, 40), ("Mandazi (6 pack)", "Bakery", 60, 40, 25), ("Fresh Milk 500ml", "Dairy", 60, 48, 60),
                    ("Yoghurt 250ml", "Dairy", 80, 58, 30), ("Soda 500ml", "Drinks", 70, 52, 80), ("Bottled Water 1L", "Drinks", 50, 32, 100),
                    ("Orange Juice 1L", "Drinks", 190, 150, 20), ("Crisps 50g", "Snacks", 50, 35, 45), ("Chocolate Bar", "Snacks", 120, 85, 8),
                    ("Laundry Soap", "Household", 140, 105, 35), ("Tissue Roll", "Household", 40, 28, 50), ("Tomatoes 1kg", "Produce", 120, 85, 15),
                    ("Onions 1kg", "Produce", 100, 70, 3), ("Maize Flour 2kg", "Pantry", 210, 175, 50)]
            for n, cat, p, k, s in demo:
                c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,10,'',?)", (uid(), n, cat, p * 100, k * 100, s, now()))
    c.close()


def login(c, b):
    r = c.execute("SELECT * FROM users WHERE username=? AND active=1", (str(b.get("username", "")).strip().lower(),)).fetchone()
    if r and secrets.compare_digest(hp(str(b.get("password", "")), r["salt"]), r["hash"]):
        t = secrets.token_urlsafe(24)
        TOKENS[t] = dict(r)
        audit(c, r, "login")
        return 200, {"token": t, "user": {"name": r["name"], "role": r["role"], "mode": mpesa.mode()}}
    return 401, {"error": "Wrong username or password"}


def prod(r, admin=True):
    d = dict(id=r["id"], name=r["name"], category=r["category"], price=money(r["price_cents"]), stock=r["stock"],
             reorder_level=r["reorder_level"], barcode=r["barcode"], updated_at=r["updated_at"])
    if admin:
        d["cost"] = money(r["cost_cents"])
    return d


def clean(b):
    try:
        d = dict(name=str(b["name"]).strip(), category=str(b.get("category") or "General").strip(), price_cents=cents(b["price"]),
                 cost_cents=cents(b.get("cost") or 0), stock=int(b["stock"]), reorder_level=int(b.get("reorder_level") or 0),
                 barcode=str(b.get("barcode") or "").strip())
    except Exception:
        return None
    return d if d["name"] and min(d["price_cents"], d["cost_cents"], d["stock"], d["reorder_level"]) >= 0 else None


def receipt(c, s):
    items = c.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY rowid", (s["id"],)).fetchall()
    return {**{k: s[k] for k in ("id", "receipt_no", "client_ref", "created_at", "cashier_id", "cashier_name", "method", "payment_ref")},
            "total": money(s["total_cents"]), "tendered": money(s["tendered_cents"]), "change": money(s["change_cents"]),
            "items": [dict(product_name=i["product_name"], qty=i["qty"], unit_price=money(i["unit_price_cents"]),
                           line_total=money(i["line_total_cents"])) for i in items]}


def _sell(c, u, b, paid_ref=None, dry=False):
    """Runs inside a transaction opened by the caller (unless dry, which only reads)."""
    ref = str(b.get("client_ref") or uid())
    old = c.execute("SELECT * FROM sales WHERE client_ref=?", (ref,)).fetchone()
    if old:  # same request sent twice: return the original sale, never charge twice
        return 200, receipt(c, old)
    qty = {}
    for it in b.get("items") or []:
        q = int(it["qty"])
        if q < 1:
=======
def audit(u, action, details=""):
    append("audit", dict(id=uid(), created_at=now(), user_name=u["name"], action=action, details=safe(details)))


def move(u, p, kind, change, reason):
    append("movements", dict(id=uid(), created_at=now(), product_id=p["id"], product_name=p["name"], type=kind,
                             qty_change=change, stock_after=p["stock"], reason=safe(reason), user_name=u["name"]))


def init():
    os.makedirs(DATA, exist_ok=True)
    for n in F:
        if not os.path.exists(path(n)):
            write(n, [])
    if not read("users"):
        rows = []
        for un, nm, role, pw in [("admin", "Administrator", "admin", "admin123"), ("cashier", "Cashier One", "cashier", "cashier123")]:
            s = secrets.token_hex(8)
            rows.append(dict(id=uid(), username=un, name=nm, role=role, salt=s, hash=hp(pw, s), created_at=now()))
        write("users", rows)
    if not read("products"):
        demo = [("White Bread 400g", "Bakery", 65, 48, 40), ("Mandazi (6 pack)", "Bakery", 60, 40, 25),
                ("Fresh Milk 500ml", "Dairy", 60, 48, 60), ("Yoghurt 250ml", "Dairy", 80, 58, 30),
                ("Soda 500ml", "Drinks", 70, 52, 80), ("Bottled Water 1L", "Drinks", 50, 32, 100),
                ("Orange Juice 1L", "Drinks", 190, 150, 20), ("Crisps 50g", "Snacks", 50, 35, 45),
                ("Chocolate Bar", "Snacks", 120, 85, 8), ("Laundry Soap", "Household", 140, 105, 35),
                ("Tissue Roll", "Household", 40, 28, 50), ("Tomatoes 1kg", "Produce", 120, 85, 15),
                ("Onions 1kg", "Produce", 100, 70, 3), ("Maize Flour 2kg", "Pantry", 210, 175, 50)]
        write("products", [dict(id=uid(), name=n, category=c, price=M(D(p)), cost=M(D(k)), stock=s, reorder_level=10,
                                barcode="", updated_at=now()) for n, c, p, k, s in demo])


def login(b):
    name = str(b.get("username", "")).strip().lower()
    for u in read("users"):
        if u["username"] == name and secrets.compare_digest(hp(str(b.get("password", "")), u["salt"]), u["hash"]):
            t = secrets.token_urlsafe(24)
            TOKENS[t] = u
            audit(u, "login")
            return 200, {"token": t, "user": {"name": u["name"], "role": u["role"]}}
    return 401, {"error": "Wrong username or password"}


def clean(b):
    try:
        d = dict(name=safe(str(b["name"]).strip()), category=safe(str(b.get("category") or "General").strip()),
                 price=D(b["price"]), cost=D(b.get("cost") or 0), stock=int(b["stock"]),
                 reorder_level=int(b.get("reorder_level") or 0), barcode=safe(str(b.get("barcode") or "").strip()))
        if not d["name"] or min(d["price"], d["cost"], d["stock"], d["reorder_level"]) < 0:
            return None
        d["price"], d["cost"], d["updated_at"] = M(d["price"]), M(d["cost"]), now()
        return d
    except Exception:
        return None


def receipt(s):
    return {**s, "items": [i for i in read("sale_items") if i["sale_id"] == s["id"]]}


def sell(u, b):
    ref = str(b.get("client_ref") or uid())
    sales = read("sales")
    old = next((s for s in sales if s["client_ref"] == ref), None)
    if old:  # same request sent twice: return the original sale, never charge twice
        return 200, receipt(old)
    prods = read("products")
    byid = {p["id"]: p for p in prods}
    qty = {}
    for it in b.get("items") or []:
        q = int(it["qty"])
        if q < 1 or it["product_id"] not in byid:
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c
            return 400, {"error": "Invalid item in the cart"}
        qty[it["product_id"]] = qty.get(it["product_id"], 0) + q
    if not qty:
        return 400, {"error": "The cart is empty"}
<<<<<<< HEAD
    lines, total = [], 0
    for pid, q in qty.items():  # prices always come from the database, never the browser
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return 400, {"error": "Invalid item in the cart"}
        if p["stock"] < q:
            return 409, {"error": f"Only {p['stock']} left of {p['name']}"}
        lines.append((p, q))
        total += p["price_cents"] * q
    is_mpesa = b.get("method") == "mpesa"  # simulated until Daraja mode is switched on
    tendered = total if is_mpesa else cents(b.get("tendered") or 0)
    if tendered < total:
        return 400, {"error": "Cash received is less than the total"}
    if dry:
        return 200, {"total": money(total), "total_cents": total}
    sid = uid()
    rn = "R" + datetime.now().strftime("%y%m%d") + str(c.execute("SELECT COUNT(*) FROM sales").fetchone()[0] + 1).zfill(4)
    pref = paid_ref or ("SIM" + secrets.token_hex(4).upper() if is_mpesa else "")
    c.execute("INSERT INTO sales VALUES(?,?,?,?,?,?,?,?,?,?,?)", (sid, rn, ref, now(), u["id"], u["name"], "mpesa" if is_mpesa else "cash",
                                                                   pref, total, tendered, tendered - total))
    for p, q in lines:
        after = p["stock"] - q
        c.execute("UPDATE products SET stock=?, updated_at=? WHERE id=?", (after, now(), p["id"]))
        c.execute("INSERT INTO sale_items VALUES(?,?,?,?,?,?,?,?,?)", (uid(), sid, p["id"], p["name"], p["category"], q,
                                                                       p["price_cents"], p["cost_cents"], p["price_cents"] * q))
        move(c, u, p["id"], p["name"], "sale", -q, after, rn)
    audit(c, u, "sale", f"{rn} {money(total)}")
    return 201, receipt(c, c.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())


def sell(c, u, b, paid_ref=None, dry=False):
    if dry:
        return _sell(c, u, b, paid_ref, True)
    with tx(c):
        return _sell(c, u, b, paid_ref)


def mpesa_start(c, u, b):
    miss = mpesa.missing()
    if mpesa.mode() == "simulated" or miss:
        return 503, {"error": "M-Pesa is not set up yet. Missing in .env: " + ", ".join(miss) if miss else "M-Pesa is in simulated mode"}
    phone = mpesa.norm(b.get("phone"))
    if not phone:
        return 400, {"error": "Enter a valid Safaricom number, e.g. 0712345678"}
    code, res = sell(c, u, {**b, "method": "mpesa"}, dry=True)  # checks items and stock; prices come from the database
    if code != 200:
        return code, res
    total = res["total_cents"]
    if total % 100:
        return 400, {"error": "M-Pesa can only charge whole shillings"}
    ref = str(b.get("client_ref") or uid())
    try:
        r = mpesa.stk_push(phone, total // 100, ref)
    except Exception as e:
        return 502, {"error": "Could not reach Daraja: " + str(e)[:100]}
    if str(r.get("ResponseCode")) != "0":
        return 502, {"error": str(r.get("errorMessage") or r.get("ResponseDescription") or "Daraja did not accept the request")[:150]}
    items = json.dumps([{"product_id": i["product_id"], "qty": int(i["qty"])} for i in b["items"]])
    c.execute("INSERT INTO mpesa_payments VALUES(?,?,?,?,?,?,?,?,?,'pending','','')",
              (uid(), now(), ref, u["id"], phone, total, items, r["CheckoutRequestID"], r.get("MerchantRequestID", "")))
    audit(c, u, "mpesa prompt sent", f"{phone} {total // 100}")
    return 201, {"status": "pending"}


def settle(c, pay_id, receipt_no):
    """Money has arrived: record the sale and mark the payment paid, as ONE transaction, exactly once."""
    with tx(c):
        pay = c.execute("SELECT * FROM mpesa_payments WHERE id=?", (pay_id,)).fetchone()
        if pay["status"] not in ("pending", "timeout"):
            return  # another request (callback or status check) already settled it
        u = dict(c.execute("SELECT * FROM users WHERE id=?", (pay["cashier_id"],)).fetchone())
        code, res = _sell(c, u, {"client_ref": pay["client_ref"], "method": "mpesa", "items": json.loads(pay["items_json"])}, paid_ref=receipt_no)
        ok = code < 300  # not ok means the money arrived but stock changed meanwhile: an admin must review
        c.execute("UPDATE mpesa_payments SET status=?, receipt=?, result_desc=? WHERE id=?",
                  ("paid" if ok else "paid_review", receipt_no, "" if ok else res.get("error", "")[:120], pay_id))
        audit(c, u, "mpesa paid", f"{receipt_no} {money(pay['amount_cents'])}")


def mpesa_callback(c, b):
    cb = b["Body"]["stkCallback"]
    pay = c.execute("SELECT * FROM mpesa_payments WHERE checkout_request_id=?", (cb["CheckoutRequestID"],)).fetchone()
    if pay and pay["status"] in ("pending", "timeout"):  # duplicate callbacks are ignored
        rc, desc = int(cb["ResultCode"]), str(cb.get("ResultDesc", ""))[:120]
        if rc == 0:
            meta = {i["Name"]: i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
            if cents(meta.get("Amount", 0)) == pay["amount_cents"]:
                settle(c, pay["id"], str(meta.get("MpesaReceiptNumber")))
            else:
                c.execute("UPDATE mpesa_payments SET status='paid_review', result_desc=? WHERE id=?", ("Amount received did not match the sale", pay["id"]))
        else:
            c.execute("UPDATE mpesa_payments SET status=?, result_desc=? WHERE id=?", ("cancelled" if rc == 1032 else "failed", desc, pay["id"]))
    return 200, {"ResultCode": 0, "ResultDesc": "Accepted"}


def mpesa_status(c, u, ref):
    q = "SELECT * FROM mpesa_payments WHERE client_ref=? AND cashier_id=? ORDER BY rowid DESC LIMIT 1"
    pay = c.execute(q, (ref, u["id"])).fetchone()
    if not pay:
        return 404, {"error": "Payment not found"}
    if pay["status"] == "pending":
        age = (datetime.now() - datetime.fromisoformat(pay["created_at"])).total_seconds()
        if age > 15:  # the callback may be late or lost: ask Daraja directly
            try:
                rc = str(mpesa.stk_query(pay["checkout_request_id"]).get("ResultCode"))
                if rc == "0":
                    settle(c, pay["id"], "CHK" + pay["checkout_request_id"][-8:])
                elif rc != "None":
                    c.execute("UPDATE mpesa_payments SET status=?, result_desc=? WHERE id=? AND status='pending'",
                              ("cancelled" if rc == "1032" else "failed", "Declined or cancelled (code " + rc + ")", pay["id"]))
            except Exception:
                pass
        if age > 120:
            c.execute("UPDATE mpesa_payments SET status='timeout' WHERE id=? AND status='pending'", (pay["id"],))
        pay = c.execute(q, (ref, u["id"])).fetchone()
    out = {"status": pay["status"], "result_desc": pay["result_desc"]}
    if pay["status"] == "paid":
        out["sale"] = receipt(c, c.execute("SELECT * FROM sales WHERE client_ref=?", (ref,)).fetchone())
    return 200, out


def dashboard(c):
    day = now()[:10]
    rev, n = c.execute("SELECT COALESCE(SUM(total_cents),0), COUNT(*) FROM sales WHERE substr(created_at,1,10)=?", (day,)).fetchone()
    profit = c.execute("SELECT COALESCE(SUM((i.unit_price_cents-i.unit_cost_cents)*i.qty),0) FROM sale_items i JOIN sales s ON s.id=i.sale_id "
                       "WHERE substr(s.created_at,1,10)=?", (day,)).fetchone()[0]
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    per = dict(c.execute("SELECT substr(created_at,1,10), SUM(total_cents) FROM sales WHERE created_at>=? GROUP BY 1", (days[0],)).fetchall())
    return 200, {
        "revenue": rev / 100, "profit": profit / 100, "sales": n,
        "week": [{"day": d, "total": per.get(d, 0) / 100} for d in days],
        "top": [[r[0], r[1], r[2] / 100] for r in c.execute("SELECT product_name, SUM(qty), SUM(line_total_cents) FROM sale_items GROUP BY product_name "
                                                          "ORDER BY 3 DESC LIMIT 6")],
        "cashiers": [[r[0], r[1], r[2] / 100] for r in c.execute("SELECT cashier_name, COUNT(*), SUM(total_cents) FROM sales "
                                                                 "WHERE substr(created_at,1,10)=? GROUP BY 1", (day,))],
        "low": [dict(name=r[0], stock=r[1], reorder=r[2]) for r in c.execute("SELECT name, stock, reorder_level FROM products WHERE stock<=reorder_level ORDER BY stock")],
    }


def api(c, m, p, u, b):
    a = p.split("/")[2:]
    admin = u["role"] == "admin"
    if a == ["me"]:
        return 200, {"name": u["name"], "role": u["role"], "mode": mpesa.mode()}
    if a == ["products"] and m == "GET":
        return 200, [prod(r, admin) for r in c.execute("SELECT * FROM products ORDER BY rowid")]
    if a == ["sales"] and m == "POST":  # only cashiers can process sales
        if u["role"] != "cashier":
            return 403, {"error": "Only cashiers can process sales"}
        if b.get("method") == "mpesa" and mpesa.mode() != "simulated":
            return 400, {"error": "Use the M-Pesa payment prompt for M-Pesa sales"}
        return sell(c, u, b)
    if a[:1] == ["mpesa"] and u["role"] == "cashier":
        if a == ["mpesa", "start"] and m == "POST":
            return mpesa_start(c, u, b)
        if len(a) == 3 and a[1] == "status":
            return mpesa_status(c, u, a[2])
        return 404, {"error": "Not found"}
    if not admin:
        return 403, {"error": "Admin access required"}
    if a == ["dashboard"]:
        return dashboard(c)
    if a == ["sales"]:
        rows = c.execute("SELECT * FROM sales ORDER BY created_at DESC, rowid DESC LIMIT 100").fetchall()
        return 200, [receipt(c, r) for r in rows]
    if a == ["mpesa"]:
        return 200, [dict(created_at=r["created_at"], phone=r["phone"], amount=r["amount_cents"] / 100, status=r["status"], receipt=r["receipt"],
                          result_desc=r["result_desc"]) for r in c.execute("SELECT * FROM mpesa_payments ORDER BY rowid DESC LIMIT 100")]
    if a == ["movements"]:
        return 200, [dict(r) for r in c.execute("SELECT created_at, product_name, type, qty_change, stock_after, reason, user_name "
                                                "FROM stock_movements ORDER BY rowid DESC LIMIT 150")]
    if a[:1] == ["products"]:
=======
    total, lines = Decimal(0), []
    for pid, q in qty.items():  # prices always come from the server, never the browser
        p = byid[pid]
        if int(p["stock"]) < q:
            return 409, {"error": f"Only {p['stock']} left of {p['name']}"}
        lines.append((p, q))
        total += D(p["price"]) * q
    mpesa = b.get("method") == "mpesa"  # simulated until Daraja is integrated
    tendered = total if mpesa else D(b.get("tendered") or 0)
    if tendered < total:
        return 400, {"error": "Cash received is less than the total"}
    s = dict(id=uid(), receipt_no="R" + datetime.now().strftime("%y%m%d") + str(len(sales) + 1).zfill(4), client_ref=ref,
             created_at=now(), cashier_id=u["id"], cashier_name=u["name"], method="mpesa" if mpesa else "cash",
             payment_ref="SIM" + secrets.token_hex(4).upper() if mpesa else "", total=M(total), tendered=M(tendered),
             change=M(tendered - total))
    items = []
    for p, q in lines:
        p["stock"] = int(p["stock"]) - q
        items.append(dict(id=uid(), sale_id=s["id"], product_id=p["id"], product_name=p["name"], category=p["category"], qty=q,
                          unit_price=p["price"], unit_cost=p["cost"], line_total=M(D(p["price"]) * q)))
        move(u, p, "sale", -q, s["receipt_no"])
    write("products", prods)
    append("sales", s)
    append("sale_items", *items)
    audit(u, "sale", f"{s['receipt_no']} {s['total']}")
    return 201, receipt(s)


def dashboard():
    sales, items, prods = read("sales"), read("sale_items"), read("products")
    today = now()[:10]
    ts = [s for s in sales if s["created_at"][:10] == today]
    tid = {s["id"] for s in ts}
    top, cash = {}, {}
    for i in items:
        t = top.setdefault(i["product_name"], [0, Decimal(0)])
        t[0] += int(i["qty"])
        t[1] += D(i["line_total"])
    for s in ts:
        c = cash.setdefault(s["cashier_name"], [0, Decimal(0)])
        c[0] += 1
        c[1] += D(s["total"])
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    tot = lambda rows: float(sum((D(r["total"]) for r in rows), Decimal(0)))
    return 200, {
        "revenue": tot(ts), "sales": len(ts),
        "profit": float(sum(((D(i["unit_price"]) - D(i["unit_cost"])) * int(i["qty"]) for i in items if i["sale_id"] in tid), Decimal(0))),
        "week": [{"day": d, "total": tot([s for s in sales if s["created_at"][:10] == d])} for d in days],
        "top": sorted(([k, v[0], float(v[1])] for k, v in top.items()), key=lambda x: -x[2])[:6],
        "cashiers": [[k, v[0], float(v[1])] for k, v in cash.items()],
        "low": [dict(name=p["name"], stock=p["stock"], reorder=p["reorder_level"]) for p in prods if int(p["stock"]) <= int(p["reorder_level"])],
    }


def api(m, p, u, b):
    a = p.split("/")[2:]
    if a == ["me"]:
        return 200, {"name": u["name"], "role": u["role"]}
    if a == ["products"] and m == "GET":
        rows = read("products")
        return 200, rows if u["role"] == "admin" else [{k: v for k, v in r.items() if k != "cost"} for r in rows]
    if a == ["sales"] and m == "POST":  # only cashiers can process sales
        return sell(u, b) if u["role"] == "cashier" else (403, {"error": "Only cashiers can process sales"})
    if u["role"] != "admin":
        return 403, {"error": "Admin access required"}
    if a == ["dashboard"]:
        return dashboard()
    if a == ["sales"]:
        its = read("sale_items")
        return 200, [{**s, "items": [i for i in its if i["sale_id"] == s["id"]]} for s in read("sales")[-100:][::-1]]
    if a == ["movements"]:
        return 200, read("movements")[-150:][::-1]
    if a[:1] == ["products"]:
        prods = read("products")
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c
        if m == "POST" and len(a) == 1:
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
<<<<<<< HEAD
            pid = uid()
            with tx(c):
                c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?)", (pid, d["name"], d["category"], d["price_cents"], d["cost_cents"],
                                                                              d["stock"], d["reorder_level"], d["barcode"], now()))
                move(c, u, pid, d["name"], "opening", d["stock"], d["stock"], "New product")
                audit(c, u, "product created", d["name"])
            return 201, prod(c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
        row = c.execute("SELECT * FROM products WHERE id=?", (a[1] if len(a) == 2 else "",)).fetchone()
=======
            d["id"] = uid()
            append("products", d)
            move(u, d, "opening", d["stock"], "New product")
            audit(u, "product created", d["name"])
            return 201, d
        row = next((r for r in prods if len(a) == 2 and r["id"] == a[1]), None)
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c
        if not row:
            return 404, {"error": "Product not found"}
        if m == "PUT":
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
<<<<<<< HEAD
            with tx(c):
                c.execute("UPDATE products SET name=?, category=?, price_cents=?, cost_cents=?, stock=?, reorder_level=?, barcode=?, updated_at=? WHERE id=?",
                          (d["name"], d["category"], d["price_cents"], d["cost_cents"], d["stock"], d["reorder_level"], d["barcode"], now(), row["id"]))
                if d["stock"] != row["stock"]:
                    move(c, u, row["id"], d["name"], "adjustment", d["stock"] - row["stock"], d["stock"], str(b.get("reason") or "Manual stock edit")[:80])
                audit(c, u, "product updated", d["name"])
            return 200, prod(c.execute("SELECT * FROM products WHERE id=?", (row["id"],)).fetchone())
        if m == "DELETE":  # sales history is kept: sale_items store their own copy of the name and prices
            with tx(c):
                c.execute("DELETE FROM products WHERE id=?", (row["id"],))
                audit(c, u, "product deleted", row["name"])
=======
            diff = d["stock"] - int(row["stock"])
            row.update(d)
            write("products", prods)
            if diff:
                move(u, row, "adjustment", diff, str(b.get("reason") or "Manual stock edit")[:80])
            audit(u, "product updated", row["name"])
            return 200, row
        if m == "DELETE":
            write("products", [r for r in prods if r is not row])
            audit(u, "product deleted", row["name"])
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c
            return 200, {"ok": True}
    return 404, {"error": "Not found"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def handle_any(self, m):
        p = urlparse(self.path).path
        if not p.startswith("/api/"):
            b = open(os.path.join(ROOT, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
<<<<<<< HEAD
        c = new_conn()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if p == "/api/login" and m == "POST":
                return self.reply(*login(c, body))
            if p.startswith("/api/mpesa/callback/") and m == "POST":  # called by Safaricom, protected by the secret in the URL
                sec = mpesa.cfg("CALLBACK_SECRET")
                if not sec or not secrets.compare_digest(p.rsplit("/", 1)[1], sec):
                    return self.reply(404, {"error": "Not found"})
                return self.reply(*mpesa_callback(c, body))
=======
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if p == "/api/login" and m == "POST":
                return self.reply(*login(body))
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c
            tok = self.headers.get("Authorization", "").replace("Bearer ", "")
            u = TOKENS.get(tok)
            if not u:
                return self.reply(401, {"error": "Please sign in again"})
            if p == "/api/logout":
                TOKENS.pop(tok, None)
                return self.reply(200, {"ok": True})
<<<<<<< HEAD
            if p.startswith("/api/export/") and m == "GET":  # admin-only data export
                if u["role"] != "admin":
                    return self.reply(403, {"error": "Admin access required"})
                res = exporter.run(c, p.rsplit("/", 1)[1], parse_qs(urlparse(self.path).query))
                if not res:
                    return self.reply(400, {"error": "Unknown export or invalid dates"})
                fname, data, ctype, n = res
                audit(c, u, "export", f"{fname} ({n} rows)")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            self.reply(*api(c, m, p, u, body))
        except Exception:
            traceback.print_exc()
            self.reply(400, {"error": "That request could not be processed"})
        finally:
            c.close()
=======
            with LOCK:  # one request changes the CSV files at a time
                self.reply(*api(m, p, u, body))
        except Exception:
            self.reply(400, {"error": "That request could not be processed"})
>>>>>>> 0a6b6295338419c1964a11616daa574f9a41368c

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_DELETE = lambda self: self.handle_any("DELETE")


if __name__ == "__main__":
    init()
    print("SmartPOS running at http://localhost:8000  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
