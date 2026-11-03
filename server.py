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


def hp(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


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
            return 400, {"error": "Invalid item in the cart"}
        qty[it["product_id"]] = qty.get(it["product_id"], 0) + q
    if not qty:
        return 400, {"error": "The cart is empty"}
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
        if m == "POST" and len(a) == 1:
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
            d["id"] = uid()
            append("products", d)
            move(u, d, "opening", d["stock"], "New product")
            audit(u, "product created", d["name"])
            return 201, d
        row = next((r for r in prods if len(a) == 2 and r["id"] == a[1]), None)
        if not row:
            return 404, {"error": "Product not found"}
        if m == "PUT":
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
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
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if p == "/api/login" and m == "POST":
                return self.reply(*login(body))
            tok = self.headers.get("Authorization", "").replace("Bearer ", "")
            u = TOKENS.get(tok)
            if not u:
                return self.reply(401, {"error": "Please sign in again"})
            if p == "/api/logout":
                TOKENS.pop(tok, None)
                return self.reply(200, {"ok": True})
            with LOCK:  # one request changes the CSV files at a time
                self.reply(*api(m, p, u, body))
        except Exception:
            self.reply(400, {"error": "That request could not be processed"})

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_DELETE = lambda self: self.handle_any("DELETE")


if __name__ == "__main__":
    init()
    print("SmartPOS running at http://localhost:8000  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
