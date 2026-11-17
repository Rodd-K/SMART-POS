"""SmartPOS - local server on SQLite. Python standard library only. Run: python3 server.py -> http://localhost:8000"""
# =====================================================================================================
# HOW THIS FILE IS ORGANISED (read this first)
#
#   1. Helpers ............ new_conn, tx, hp, audit, move, init
#   2. Sign-in ............ login
#   3. Products and sales . prod, clean, receipt, _sell, sell
#   4. M-Pesa payments .... mpesa_start, settle, mpesa_callback, mpesa_status
#   5. Admin features ..... dashboard, fraud_api, print_receipt, hardware_api
#   6. api() .............. the ROUTER: decides which function answers each web address, and who may use it
#   7. class H ............ the web server itself: receives a request, calls api(), sends the answer back
#   8. scan_loop + the last lines ... start everything
#
# THE JOURNEY OF A REQUEST
#   browser page  ->  H.handle_any  ->  api()  ->  one of the functions above  ->  database  ->  answer (JSON) back to the page
#
# TO ADD A NEW FEATURE
#   write a function (sections 3-5), add one route line in api() (section 6), call it from index.html, add a test in tests/.
# =====================================================================================================
import os, re, sys, json, uuid, hashlib, secrets, threading, time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import db
import accounts
import connectivity
import mpesa
import exporter
from fraud import engine as fraud_engine
from hardware import escpos, printer

# ROOT = the folder this file lives in (used to find index.html).
# TOKENS = who is signed in. It maps a random sign-in token to that user's record. It lives in memory only,
# so everybody is signed out whenever the server restarts.
ROOT = os.path.dirname(os.path.abspath(__file__))
TOKENS = {}
# Tiny helpers used everywhere:
#   uid()   makes a unique id           now()    the current time as text
#   cents() turns "65.50" into 6550      money()  turns 6550 back into "65.50"
# IMPORTANT: money is always kept as WHOLE CENTS inside the program, so totals never have rounding errors.
uid = lambda: str(uuid.uuid4())
now = lambda: datetime.now().isoformat(timespec="seconds")
cents = lambda v: int((Decimal(str(v)) * 100).to_integral_value())  # "65.50" -> 6550
money = lambda c: "%d.%02d" % divmod(c, 100)                         # 6550 -> "65.50"


# Opens a database connection for ONE request. isolation_level=None means "we decide when a transaction starts"
# (see tx below). Every request gets its own connection and closes it when finished.
def new_conn():
    c = db.connect()
    c.isolation_level = None  # we control transactions ourselves
    return c


# tx() = a transaction: a group of database changes that ALL succeed or ALL are undone.
# Use it like:   with tx(c):  ...several changes...
# BEGIN IMMEDIATE lets only one writer in at a time, which is what stops two cashiers selling the last unit together.
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


# Turns a password into a scrambled "hash" (PBKDF2, 200,000 rounds, plus a random salt per user).
# Passwords are never stored or compared in plain text.
def hp(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


# Writes one line to the audit log: who did what, and when. Call this for anything sensitive so there is always a trail.
def audit(c, u, action, details=""):
    c.execute("INSERT INTO audit_log VALUES(?,?,?,?,?)", (uid(), now(), u["name"], action, details))


# Records a stock movement (a sale, opening stock, a manual adjustment). Stock is never just overwritten:
# every change leaves a row here, which is also what the fraud engine reads.
def move(c, u, pid, pname, kind, change, after, reason):
    c.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?,?,?)", (uid(), now(), pid, pname, kind, change, after, reason, u["name"]))


# Runs once when the server starts. It makes sure the database exists and is up to date.
# A REAL install starts with NO accounts: the first visit shows the set-up screen (see setup()), where the installer creates the
# business and its admin. Only a development copy (SMARTPOS_DEMO=1 in .env) gets demo accounts and sample products.
def init():
    if not os.path.exists(db.DB_PATH) and os.path.exists(os.path.join(ROOT, "data", "users.csv")):
        sys.exit("Your data is still in the CSV files. Run  python3 migrate_csv.py  first, then start the server again.")
    db.init().close()  # creates the tables, or upgrades an older database
    c = new_conn()
    if os.environ.get("SMARTPOS_DEMO") == "1" and not c.execute("SELECT 1 FROM users").fetchone():
        with tx(c):
            for un, nm, role, pw in [("admin", "Administrator", "admin", "admin123"), ("cashier", "Cashier One", "cashier", "cashier123")]:
                s = secrets.token_hex(8)
                c.execute("INSERT INTO users(id,username,name,role,salt,hash,active,created_at) VALUES(?,?,?,?,?,?,1,?)", (uid(), un, nm, role, s, hp(pw, s), now()))
            demo = [("White Bread 400g", "Bakery", 65, 48, 40), ("Mandazi (6 pack)", "Bakery", 60, 40, 25), ("Fresh Milk 500ml", "Dairy", 60, 48, 60),
                    ("Yoghurt 250ml", "Dairy", 80, 58, 30), ("Soda 500ml", "Drinks", 70, 52, 80), ("Bottled Water 1L", "Drinks", 50, 32, 100),
                    ("Orange Juice 1L", "Drinks", 190, 150, 20), ("Crisps 50g", "Snacks", 50, 35, 45), ("Chocolate Bar", "Snacks", 120, 85, 8),
                    ("Laundry Soap", "Household", 140, 105, 35), ("Tissue Roll", "Household", 40, 28, 50), ("Tomatoes 1kg", "Produce", 120, 85, 15),
                    ("Onions 1kg", "Produce", 100, 70, 3), ("Maize Flour 2kg", "Pantry", 210, 175, 50)]
            for n, cat, p, k, s in demo:
                c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,10,'',?)", (uid(), n, cat, p * 100, k * 100, s, now()))
    c.close()


# ======================================== ACCOUNTS: sign-in, sessions, set-up, staff ========================================
# Reads one business setting (for example the business name) from the settings table.
def setting(c, key, default=""):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


# A SESSION is a signed-in person. TOKENS[token] = {"uid": the user's id, "seen": when they last did something}.
# The user's record is read fresh on EVERY request, so switching someone off, changing their role or their password takes effect at once.
# A session also ends after SMARTPOS_SESSION_MINUTES without activity (default 480 = 8 hours).
def start_session(r):
    t = secrets.token_urlsafe(24)
    TOKENS[t] = {"uid": r["id"], "seen": time.time()}
    return t


def session_user(c, tok):
    s = TOKENS.get(tok)
    if not s:
        return None
    if time.time() - s["seen"] > float(os.environ.get("SMARTPOS_SESSION_MINUTES", "480")) * 60:
        TOKENS.pop(tok, None)
        return None
    r = c.execute("SELECT * FROM users WHERE id=? AND active=1", (s["uid"],)).fetchone()
    if not r:
        TOKENS.pop(tok, None)
        return None
    s["seen"] = time.time()
    return dict(r)


# What the page needs to know about the signed-in person (never the password hash).
def user_info(c, u):
    return {"name": u["name"], "role": u["role"], "mode": mpesa.mode(), "printer": printer.configured(), "autoprint": printer.settings()["autoprint"],
            "must_change": bool(u["must_change_password"]), "business": setting(c, "business_name")}


# Checks the username and password. On success it returns a random token that the page keeps and sends with every later request.
# SAFETY: after accounts.MAX_FAILURES wrong passwords in a row the account is locked for accounts.LOCK_MINUTES (an admin can unlock it).
# A demo password (admin123...) is accepted but forces a new password straight away, unless this is a development copy (SMARTPOS_DEMO=1).
# Failed attempts are written to the audit log; the fraud engine watches for password guessing.
def login(c, b):
    name, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    r = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    if r and r["locked_until"] > now():
        audit(c, r, "login blocked", "account locked")
        return 429, {"error": f"Too many wrong passwords. This account is locked for {accounts.LOCK_MINUTES} minutes. An admin can unlock it."}
    if r and r["active"] and secrets.compare_digest(hp(pw, r["salt"]), r["hash"]):
        must = r["must_change_password"] or (pw in accounts.DEMO_PASSWORDS and os.environ.get("SMARTPOS_DEMO") != "1")
        c.execute("UPDATE users SET failed_logins=0, locked_until='', last_login=?, must_change_password=? WHERE id=?", (now(), 1 if must else 0, r["id"]))
        audit(c, r, "login")
        r = c.execute("SELECT * FROM users WHERE id=?", (r["id"],)).fetchone()
        return 200, {"token": start_session(r), "user": user_info(c, dict(r))}
    if r:  # a real account: count this failure, and lock the account after too many in a row
        n = r["failed_logins"] + 1
        if n >= accounts.MAX_FAILURES:
            until = (datetime.now() + timedelta(minutes=accounts.LOCK_MINUTES)).isoformat(timespec="seconds")
            c.execute("UPDATE users SET failed_logins=0, locked_until=? WHERE id=?", (until, r["id"]))
        else:
            c.execute("UPDATE users SET failed_logins=? WHERE id=?", (n, r["id"]))
    audit(c, {"name": name[:40] or "?"}, "login failed")  # the fraud engine watches for guessing
    return 401, {"error": "Wrong username or password"}


# FIRST-RUN SET-UP. A brand-new install has no accounts. The page asks (setup_status) and shows the set-up screen, where the installer
# enters the business name and creates the admin with a password they choose. Nothing is a default. It works ONCE: after that it refuses.
def setup_status(c):
    return 200, {"needed": not c.execute("SELECT 1 FROM users LIMIT 1").fetchone(), "business": setting(c, "business_name")}


def setup(c, b):
    if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():  # already set up: refuse straight away, whatever was typed
        return 403, {"error": "This system is already set up."}
    business, name = str(b.get("business", "")).strip()[:80], str(b.get("name", "")).strip()[:60]
    username, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if not business or not name:
        return 400, {"error": "Please enter the business name and your name."}
    err = accounts.check_username(username) or accounts.check_password(pw, username)
    if err:
        return 400, {"error": err}
    with tx(c):
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():  # checked again inside the transaction: no one can set up twice
            return 403, {"error": "This system is already set up."}
        s = secrets.token_hex(8)
        c.execute("INSERT INTO users(id,username,name,role,salt,hash,active,created_at,password_changed_at) VALUES(?,?,?,'admin',?,?,1,?,?)",
                  (uid(), username, name, s, hp(pw, s), now(), now()))
        c.execute("INSERT OR REPLACE INTO settings VALUES('business_name', ?)", (business,))
        audit(c, {"name": name}, "setup", f"business: {business}")
    return login(c, {"username": username, "password": pw})


# Any signed-in person can change their own password. They must know the current one.
def change_password(c, u, b):
    cur, new = str(b.get("current", "")), str(b.get("new", ""))
    r = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    if not secrets.compare_digest(hp(cur, r["salt"]), r["hash"]):
        return 403, {"error": "Your current password is not correct."}
    err = accounts.check_password(new, r["username"])
    if err:
        return 400, {"error": err}
    if secrets.compare_digest(hp(new, r["salt"]), r["hash"]):
        return 400, {"error": "Please choose a password different from the current one."}
    s = secrets.token_hex(8)
    c.execute("UPDATE users SET salt=?, hash=?, must_change_password=0, password_changed_at=? WHERE id=?", (s, hp(new, s), now(), u["id"]))
    audit(c, u, "password changed")
    return 200, {"ok": True}


# How one staff member appears in the Staff tab (no password data, ever).
def user_row(r):
    return dict(id=r["id"], username=r["username"], name=r["name"], role=r["role"], active=bool(r["active"]), must_change=bool(r["must_change_password"]),
                locked=r["locked_until"] > now(), last_login=r["last_login"], created_at=r["created_at"])


def other_active_admins(c, user_id):
    return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (user_id,)).fetchone()[0]


# The Staff tab (ADMIN ONLY): list, add, edit, switch off or on, reset a password, unlock. People are never deleted, so their sales
# and stock history stay linked to them. RULE: there must always be at least one active admin, so a shop can never lock itself out.
def users_api(c, m, a, u, b):
    if a == ["users"] and m == "GET":
        return 200, [user_row(r) for r in c.execute("SELECT * FROM users ORDER BY role, name")]
    if a == ["users"] and m == "POST":
        username, name, role, pw = str(b.get("username", "")).strip().lower(), str(b.get("name", "")).strip()[:60], b.get("role"), str(b.get("password", ""))
        if not name:
            return 400, {"error": "Please enter the person's name."}
        if role not in accounts.ROLES:
            return 400, {"error": "Choose a role."}
        err = accounts.check_username(username) or accounts.check_password(pw, username)
        if err:
            return 400, {"error": err}
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return 409, {"error": "That username is already taken."}
        s, new_id = secrets.token_hex(8), uid()
        c.execute("INSERT INTO users(id,username,name,role,salt,hash,active,created_at,must_change_password,password_changed_at) VALUES(?,?,?,?,?,?,1,?,1,?)",
                  (new_id, username, name, role, s, hp(pw, s), now(), now()))
        audit(c, u, "user created", f"{username} ({role})")
        return 201, user_row(c.execute("SELECT * FROM users WHERE id=?", (new_id,)).fetchone())
    t = c.execute("SELECT * FROM users WHERE id=?", (a[1] if len(a) > 1 else "",)).fetchone()
    if not t:
        return 404, {"error": "Person not found"}
    if len(a) == 2 and m == "PUT":
        name, role = str(b.get("name", t["name"])).strip()[:60], b.get("role", t["role"])
        active = 1 if b.get("active", bool(t["active"])) else 0
        if not name or role not in accounts.ROLES:
            return 400, {"error": "Check the name and role."}
        if t["role"] == "admin" and t["active"] and (role != "admin" or not active) and other_active_admins(c, t["id"]) == 0:
            return 400, {"error": "There must always be at least one active admin."}
        c.execute("UPDATE users SET name=?, role=?, active=? WHERE id=?", (name, role, active, t["id"]))
        audit(c, u, "user updated", f"{t['username']}: role {role}, {'active' if active else 'switched off'}")
        return 200, user_row(c.execute("SELECT * FROM users WHERE id=?", (t["id"],)).fetchone())
    if len(a) == 3 and a[2] == "reset-password" and m == "POST":
        pw = str(b.get("password", ""))
        err = accounts.check_password(pw, t["username"])
        if err:
            return 400, {"error": err}
        s = secrets.token_hex(8)
        c.execute("UPDATE users SET salt=?, hash=?, must_change_password=1, failed_logins=0, locked_until='' WHERE id=?", (s, hp(pw, s), t["id"]))
        audit(c, u, "password reset", t["username"])  # the new password is never written to the log
        return 200, {"ok": True}
    if len(a) == 3 and a[2] == "unlock" and m == "POST":
        c.execute("UPDATE users SET failed_logins=0, locked_until='' WHERE id=?", (t["id"],))
        audit(c, u, "account unlocked", t["username"])
        return 200, {"ok": True}
    return 404, {"error": "Not found"}


# Shapes one product row for the page: cents become "65.00" text. Cashiers do not get the cost price (admin=False hides it).
def prod(r, admin=True):
    d = dict(id=r["id"], name=r["name"], category=r["category"], price=money(r["price_cents"]), stock=r["stock"],
             reorder_level=r["reorder_level"], barcode=r["barcode"], updated_at=r["updated_at"])
    if admin:
        d["cost"] = money(r["cost_cents"])
    return d


# Checks the product details typed into the admin form. Returns None if anything is wrong (empty name, negative price...).
# TO ADD A NEW PRODUCT FIELD: add it here, in prod() above, in the products table (db.py, as a migration), and in prodForm() in index.html.
def clean(b):
    try:
        d = dict(name=str(b["name"]).strip(), category=str(b.get("category") or "General").strip(), price_cents=cents(b["price"]),
                 cost_cents=cents(b.get("cost") or 0), stock=int(b["stock"]), reorder_level=int(b.get("reorder_level") or 0),
                 barcode=str(b.get("barcode") or "").strip())
    except Exception:
        return None
    return d if d["name"] and min(d["price_cents"], d["cost_cents"], d["stock"], d["reorder_level"]) >= 0 else None


# Builds the complete receipt (the sale plus its lines) in the shape the page AND the printer both use.
def receipt(c, s):
    items = c.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY rowid", (s["id"],)).fetchall()
    return {**{k: s[k] for k in ("id", "receipt_no", "client_ref", "created_at", "cashier_id", "cashier_name", "method", "payment_ref")},
            "total": money(s["total_cents"]), "tendered": money(s["tendered_cents"]), "change": money(s["change_cents"]),
            "items": [dict(product_name=i["product_name"], qty=i["qty"], unit_price=money(i["unit_price_cents"]),
                           line_total=money(i["line_total_cents"])) for i in items]}


# THE HEART OF THE POS: saves one sale. In order:
#   1. If this client_ref was already used, return that sale (so a double-click can never charge twice).
#   2. Check every item exists and there is enough stock.
#   3. Work out the total from the DATABASE prices, never from the browser.
#   4. Check the cash covers the total.
#   5. Write the sale, its lines, the reduced stock and the stock movements.
# dry=True only checks and returns the total without saving (used before sending an M-Pesa prompt).
# It must run inside a transaction: use sell() below, which does that for you.
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
            return 400, {"error": "Invalid item in the cart"}
        qty[it["product_id"]] = qty.get(it["product_id"], 0) + q
    if not qty:
        return 400, {"error": "The cart is empty"}
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


# Wraps _sell in a transaction so a sale is saved completely or not at all. Call THIS, not _sell, from other code.
def sell(c, u, b, paid_ref=None, dry=False):
    if dry:
        return _sell(c, u, b, paid_ref, True)
    with tx(c):
        return _sell(c, u, b, paid_ref)


# M-PESA STEP 1: check the cart, ask Safaricom to send the customer a payment prompt, and remember the attempt as "pending".
# At this point there is NO sale and NO stock change yet.
def mpesa_start(c, u, b):
    miss = mpesa.missing()
    if mpesa.mode() == "simulated" or miss:
        return 503, {"error": "M-Pesa is not set up yet. Missing in .env: " + ", ".join(miss) if miss else "M-Pesa is in simulated mode"}
    if not connectivity.online():  # fail fast with a clear message instead of making the cashier wait for a prompt that cannot arrive
        return 503, {"error": "There is no internet connection right now, so an M-Pesa prompt cannot be sent. Use Checkout with the customer's M-Pesa message, or take cash."}
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


# M-PESA STEP 3: the money has arrived. In ONE transaction: record the sale, reduce stock, mark the payment paid.
# Safe to call twice: if the payment is already settled it does nothing.
def settle(c, pay_id, receipt_no, note=""):
    """Money has arrived: record the sale and mark the payment paid, as ONE transaction, exactly once."""
    with tx(c):
        pay = c.execute("SELECT * FROM mpesa_payments WHERE id=?", (pay_id,)).fetchone()
        if pay["status"] not in ("pending", "timeout"):
            return  # another request (callback, status check or the recovery job) already settled it
        u = dict(c.execute("SELECT * FROM users WHERE id=?", (pay["cashier_id"],)).fetchone())
        # DOUBLE-SALE GUARD. If the payment was slow, the cashier may have given up and taken CASH for the same goods.
        # If this cashier has made ANY sale since this prompt was sent, recording this one as well could sell the goods twice,
        # so the payment is held for a manager instead (it shows in the M-Pesa tab and raises a fraud alert).
        if c.execute("SELECT 1 FROM sales WHERE cashier_id=? AND created_at>? LIMIT 1", (pay["cashier_id"], pay["created_at"])).fetchone():
            c.execute("UPDATE mpesa_payments SET status='paid_review', receipt=?, result_desc=? WHERE id=?",
                      (receipt_no, "Customer paid, but the cashier made another sale meanwhile. Check for a double payment.", pay_id))
            audit(c, u, "mpesa held", f"{receipt_no} {money(pay['amount_cents'])}")
            return
        code, res = _sell(c, u, {"client_ref": pay["client_ref"], "method": "mpesa", "items": json.loads(pay["items_json"])}, paid_ref=receipt_no)
        ok = code < 300  # not ok means the money arrived but stock changed meanwhile: an admin must review
        c.execute("UPDATE mpesa_payments SET status=?, receipt=?, result_desc=? WHERE id=?",
                  ("paid" if ok else "paid_review", receipt_no, note if ok else res.get("error", "")[:120], pay_id))
        audit(c, u, "mpesa paid", f"{receipt_no} {money(pay['amount_cents'])}")


# OUTAGE RECOVERY. If the internet drops, Safaricom's callback can be lost even though the customer really paid.
# This job (run every couple of minutes while we are online) asks Safaricom about every payment that never got a result
# and finishes it: paid -> the sale is recorded, declined or cancelled -> closed, no answer for 2 minutes -> timeout.
# If Safaricom cannot be reached it simply stops and tries again next time. Nothing is ever lost, only delayed.
def recover_payments(c, limit=20):
    if mpesa.mode() == "simulated" or mpesa.missing():
        return 0
    now_ = datetime.now()
    rows = c.execute("SELECT * FROM mpesa_payments WHERE status IN ('pending','timeout') AND created_at<=? AND created_at>=? ORDER BY rowid LIMIT ?",
                     ((now_ - timedelta(seconds=30)).isoformat(timespec="seconds"), (now_ - timedelta(hours=48)).isoformat(timespec="seconds"), limit)).fetchall()
    recovered = 0
    for r in rows:
        try:
            rc = str(mpesa.stk_query(r["checkout_request_id"]).get("ResultCode"))
        except Exception:
            return recovered  # no connection to Safaricom: stop now, try again at the next run
        if rc == "0":
            settle(c, r["id"], "CHK" + r["checkout_request_id"][-8:], note="Recovered after a connection problem")
            recovered += 1
        elif rc != "None":  # Safaricom gave a final "no": the customer cancelled, had too little money, entered a wrong PIN ...
            c.execute("UPDATE mpesa_payments SET status=?, result_desc=? WHERE id=? AND status IN ('pending','timeout')",
                      ("cancelled" if rc == "1032" else "failed", f"Declined or cancelled (code {rc})", r["id"]))
        elif r["status"] == "pending" and (now_ - datetime.fromisoformat(r["created_at"])).total_seconds() > 120:
            c.execute("UPDATE mpesa_payments SET status='timeout' WHERE id=? AND status='pending'", (r["id"],))  # no answer: give up waiting
    return recovered


# M-PESA STEP 2: Safaricom calls this (through the secret web address) to say the payment worked or failed.
# We only believe a success if the amount matches the sale. Duplicate callbacks are ignored.
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


# The cashier's screen asks this every 3 seconds: "did the customer pay yet?".
# If no callback has arrived after 15 seconds we ask Safaricom directly; after 2 minutes we give up (timeout).
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


# The numbers on the admin Overview page: today's revenue, profit and sales, the last 7 days, top products, cashier totals,
# low stock and new fraud alerts. TO ADD A NUMBER: add it to the dictionary below and show it in index.html (the 'over' tab).
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
        "fraud_new": c.execute("SELECT COUNT(*) FROM fraud_events WHERE status='new'").fetchone()[0],
    }


# The decisions a manager can record on a fraud alert. NEEDS_NOTE lists the ones that must come with a written note.
FRAUD_STATUSES = ("new", "investigating", "legitimate", "policy_violation", "suspected_fraud", "inconclusive")
NEEDS_NOTE = ("legitimate", "policy_violation", "suspected_fraud", "inconclusive")


# Shapes one fraud finding for the page.
def fraud_event(r):
    return dict(id=r["id"], created_at=r["created_at"], event_time=r["event_time"], rule=r["rule"], subject=r["subject"], score=r["score"],
                level=fraud_engine.band(r["score"]), title=r["title"], reason=r["reason"], evidence=json.loads(r["evidence_json"] or "{}"),
                status=r["status"], reviewed_by=r["reviewed_by"], reviewed_at=r["reviewed_at"], note=r["note"])


# Everything behind the admin Fraud tab: the summary, the list of alerts, "Scan now", and recording a decision.
# ADMIN ONLY. Nothing here can change a sale, a payment or stock: alerts are advice for a manager.
def fraud_api(c, m, a, u, b):
    """Admin only. Alerts are advice for a manager: nothing here can change a sale, payment or stock record."""
    if a == ["fraud", "summary"] and m == "GET":
        n = dict(c.execute("SELECT status, COUNT(*) FROM fraud_events GROUP BY status").fetchall())
        return 200, {"new": n.get("new", 0), "open": n.get("new", 0) + n.get("investigating", 0), "by_status": n, "risk": fraud_engine.risk(c)}
    if len(a) == 3 and a[1] == "events" and m == "GET":
        f = a[2]
        if f == "open":
            where, args = "WHERE status IN ('new','investigating')", ()
        elif f == "all":
            where, args = "", ()
        elif f in FRAUD_STATUSES:
            where, args = "WHERE status=?", (f,)
        else:
            return 400, {"error": "Unknown filter"}
        rows = c.execute(f"SELECT * FROM fraud_events {where} ORDER BY event_time DESC, rowid DESC LIMIT 200", args).fetchall()
        return 200, [fraud_event(r) for r in rows]
    if a == ["fraud", "scan"] and m == "POST":
        n = fraud_engine.run(c)
        audit(c, u, "fraud scan", f"{n} new finding(s)")
        return 200, {"new": n}
    if len(a) == 3 and a[2] == "decision" and m == "POST":
        row = c.execute("SELECT * FROM fraud_events WHERE id=?", (a[1],)).fetchone()
        if not row:
            return 404, {"error": "Alert not found"}
        status, note = str(b.get("status", "")), str(b.get("note", "")).strip()[:300]
        if status not in FRAUD_STATUSES:
            return 400, {"error": "Choose a valid decision"}
        if status in NEEDS_NOTE and len(note) < 3:
            return 400, {"error": "Add a short note explaining the decision"}
        c.execute("UPDATE fraud_events SET status=?, note=?, reviewed_by=?, reviewed_at=? WHERE id=?", (status, note, u["name"], now(), a[1]))
        audit(c, u, "fraud decision", f"{row['title']} / {row['subject']}: {status}")
        return 200, fraud_event(c.execute("SELECT * FROM fraud_events WHERE id=?", (a[1],)).fetchone())
    return 404, {"error": "Not found"}


# Prints a receipt on the thermal printer. The rules:
#   - a cashier may print only their OWN sale, within 10 minutes; an admin may print any receipt at any time
#   - the 2nd and later copies say DUPLICATE COPY and never open the cash drawer
#   - every successful print is logged in the audit log
def print_receipt(c, u, receipt_no):
    s = c.execute("SELECT * FROM sales WHERE receipt_no=?", (receipt_no,)).fetchone()
    if not s:
        return 404, {"error": "Receipt not found"}
    if u["role"] != "admin":  # a cashier may print their own fresh sale; older reprints are for a manager
        age = (datetime.now() - datetime.fromisoformat(s["created_at"])).total_seconds()
        if s["cashier_id"] != u["id"] or age > 600:
            return 403, {"error": "Only an admin can print this receipt"}
    cfg = printer.settings()
    cfg["name"] = setting(c, "business_name") or cfg["name"]  # the name entered at set-up wins over the .env one
    copies = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='receipt printed' AND details=?", (receipt_no,)).fetchone()[0]
    doc = escpos.receipt(receipt(c, s), cfg, copy=copies, kick=cfg["drawer"] and s["method"] == "cash" and copies == 0)
    try:
        printer.send(doc.bytes())
    except printer.PrinterError as e:
        return 503, {"error": str(e)}
    audit(c, u, "receipt printed", receipt_no)  # every print is on record; the second one onward says DUPLICATE COPY
    return 200, {"printed": True, "copy": copies + 1}


# Behind the admin Hardware tab: show the printer settings, print a test page, open the cash drawer.
def hardware_api(c, m, a, u, b):
    cfg = printer.settings()
    cfg["name"] = setting(c, "business_name") or cfg["name"]
    if a == ["hardware"] and m == "GET":
        kind, _, target = cfg["spec"].partition(":")
        return 200, dict(printer=dict(configured=printer.configured(), type=kind.lower() or "none", target=target, width=cfg["width"]),
                         drawer=dict(enabled=cfg["drawer"], pin=cfg["pin"]), autoprint=cfg["autoprint"], shop=dict(name=cfg["name"], lines=cfg["lines"]))
    try:
        if a == ["hardware", "test-print"] and m == "POST":
            printer.send(escpos.test_page(cfg).bytes())
            audit(c, u, "printer test")
            return 200, {"printed": True}
        if a == ["hardware", "test-drawer"] and m == "POST":
            d = escpos.Doc(cfg["width"])
            d.kick(cfg["pin"])
            printer.send(d.bytes())
            audit(c, u, "drawer test")
            return 200, {"opened": True}
    except printer.PrinterError as e:
        return 503, {"error": str(e)}
    return 404, {"error": "Not found"}


# ======================================== MANUAL M-PESA CHECKOUT (when there is no internet) ========================================
# Online, an M-Pesa payment is confirmed by SAFARICOM (the prompt, then the callback). With no internet that cannot happen, so the cashier
# may confirm it BY EYE: the customer shows the M-Pesa message, the cashier types the 10-character code and the amount, and presses Checkout.
# Because this trusts a person instead of Safaricom, it has safeguards:
#   - the sale is recorded as "unverified" until a manager compares the code with the M-Pesa statement (Verified or Disputed)
#   - the code format is checked, the amount typed must equal the total, and the SAME code can never be used twice
#   - it is limited to SMARTPOS_MANUAL_MPESA_MAX shillings per payment (default 20000) and can be switched off (SMARTPOS_MANUAL_MPESA=off)
#   - if the system could reach Safaricom and the cashier chose Offline anyway, that is recorded and raises a fraud alert
#   - the fraud engine also flags unverified payments left too long, disputed ones, and cashiers who use it very often
MANUAL_CODE = re.compile(r"^[A-Z0-9]{10}$")


def mpesa_manual(c, u, b):
    if os.environ.get("SMARTPOS_MANUAL_MPESA", "on").lower() == "off":
        return 403, {"error": "Manual M-Pesa confirmation is switched off."}
    ref = str(b.get("client_ref") or uid())
    old = c.execute("SELECT * FROM sales WHERE client_ref=?", (ref,)).fetchone()
    if old:  # the same request twice (a double click) never records two sales
        return 200, receipt(c, old)
    online = connectivity.online()
    if online and not b.get("override"):  # "override" is true only when the cashier pressed the Offline button
        return 409, {"error": "The system is online, so send the M-Pesa prompt instead. If the customer's phone cannot receive it, press Offline first."}
    code = re.sub(r"\s", "", str(b.get("code", ""))).upper()
    if not (MANUAL_CODE.match(code) and any(ch.isdigit() for ch in code) and any(ch.isalpha() for ch in code)):
        return 400, {"error": "Type the 10-character M-Pesa code exactly as it appears in the customer's message."}
    phone = ""
    if str(b.get("phone", "")).strip():
        phone = mpesa.norm(b.get("phone"))
        if not phone:
            return 400, {"error": "That phone number does not look right."}
    status, res = sell(c, u, {**b, "method": "mpesa"}, dry=True)  # checks the items and stock, and gives the total from the database prices
    if status != 200:
        return status, res
    total = res["total_cents"]
    try:
        typed = cents(b.get("amount"))
    except Exception:
        return 400, {"error": "Type the amount shown in the customer's M-Pesa message."}
    if typed != total:
        return 400, {"error": f"The amount in the message must equal the total, KES {money(total)}."}
    cap = float(os.environ.get("SMARTPOS_MANUAL_MPESA_MAX", "20000"))
    if total > cap * 100:
        return 400, {"error": f"Manual confirmation is limited to KES {cap:,.0f} per payment. Please take cash or wait for the internet."}
    if c.execute("SELECT 1 FROM mpesa_manual WHERE code=? UNION SELECT 1 FROM mpesa_payments WHERE receipt=?", (code, code)).fetchone():
        return 409, {"error": "That M-Pesa code has already been used for another sale."}
    with tx(c):  # the sale and the "to be verified" record are saved together, or not at all
        status, res = _sell(c, u, {"client_ref": ref, "method": "mpesa", "items": b.get("items")}, paid_ref=code)
        if status >= 300:
            return status, res
        sale = c.execute("SELECT id FROM sales WHERE client_ref=?", (ref,)).fetchone()
        c.execute("INSERT INTO mpesa_manual(id,created_at,sale_id,cashier_id,code,phone,amount_cents,online_when_taken) VALUES(?,?,?,?,?,?,?,?)",
                  (uid(), now(), sale["id"], u["id"], code, phone, total, 1 if online else 0))
        audit(c, u, "mpesa manual", f"{code} {money(total)}" + (" (system was online)" if online else ""))
    return 201, res


# The admin's side (M-Pesa tab): list the manually confirmed payments, and mark each one Verified (it is on the M-Pesa statement) or Disputed.
def manual_admin(c, m, a, u, b):
    if a == ["mpesa", "manual"] and m == "GET":
        rows = c.execute("SELECT m.*, u.name AS cashier, s.receipt_no FROM mpesa_manual m JOIN users u ON u.id=m.cashier_id JOIN sales s ON s.id=m.sale_id "
                         "ORDER BY m.rowid DESC LIMIT 200").fetchall()
        return 200, [dict(id=r["id"], created_at=r["created_at"], cashier=r["cashier"], receipt_no=r["receipt_no"], code=r["code"], phone=r["phone"],
                          amount=r["amount_cents"] / 100, online_when_taken=bool(r["online_when_taken"]), status=r["status"], checked_by=r["checked_by"],
                          checked_at=r["checked_at"], note=r["note"]) for r in rows]
    if a == ["mpesa", "manual"] and m == "POST":
        return 403, {"error": "Only cashiers can process sales"}
    if len(a) == 4 and a[3] == "review" and m == "POST":
        row = c.execute("SELECT * FROM mpesa_manual WHERE id=?", (a[2],)).fetchone()
        if not row:
            return 404, {"error": "Payment not found"}
        status, note = str(b.get("status", "")), str(b.get("note", "")).strip()[:300]
        if status not in ("verified", "disputed"):
            return 400, {"error": "Choose Verified or Disputed."}
        if status == "disputed" and len(note) < 3:
            return 400, {"error": "Add a short note saying why it does not match the statement."}
        c.execute("UPDATE mpesa_manual SET status=?, checked_by=?, checked_at=?, note=? WHERE id=?", (status, u["name"], now(), note, a[2]))
        audit(c, u, "mpesa manual " + status, f"{row['code']} {money(row['amount_cents'])}")
        return 200, {"ok": True}
    return 404, {"error": "Not found"}


# THE ROUTER. Every request (after sign-in) arrives here.
#   a = the web address split into parts, e.g. /api/products/123 becomes ["products", "123"]
#   m = GET / POST / PUT / DELETE      u = the signed-in user      b = the data the page sent
# Routes are checked from top to bottom. Routes ABOVE "if not admin" can be used by cashiers; routes BELOW are ADMIN ONLY.
# TO ADD AN ENDPOINT: add an  if a == [...] and m == "...":  block in the right place (above or below the admin line).
def api(c, m, p, u, b):
    a = p.split("/")[2:]
    admin = u["role"] == "admin"
    if a == ["me"]:
        return 200, user_info(c, u)
    if a == ["password"] and m == "POST":  # any signed-in person may change their own password
        return change_password(c, u, b)
    if a == ["health"]:  # the cashier screen asks this every few seconds to show "Online" or "No internet"
        return 200, {"online": connectivity.online(), "mode": mpesa.mode()}
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
        if a == ["mpesa", "manual"] and m == "POST":  # the Checkout button when there is no internet
            return mpesa_manual(c, u, b)
        if len(a) == 3 and a[1] == "status":
            return mpesa_status(c, u, a[2])
        return 404, {"error": "Not found"}
    if a[:2] == ["print", "receipt"] and len(a) == 3 and m == "POST":
        return print_receipt(c, u, a[2])
    # Everything below this line is ADMIN ONLY.
    if not admin:
        return 403, {"error": "Admin access required"}
    if a[:1] == ["hardware"]:
        return hardware_api(c, m, a, u, b)
    if a[:1] == ["users"]:
        return users_api(c, m, a, u, b)
    if a[:2] == ["mpesa", "manual"]:
        return manual_admin(c, m, a, u, b)
    if a == ["dashboard"]:
        return dashboard(c)
    if a[:1] == ["fraud"]:
        return fraud_api(c, m, a, u, b)
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
        if m == "POST" and len(a) == 1:
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
            pid = uid()
            with tx(c):
                c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?)", (pid, d["name"], d["category"], d["price_cents"], d["cost_cents"],
                                                                              d["stock"], d["reorder_level"], d["barcode"], now()))
                move(c, u, pid, d["name"], "opening", d["stock"], d["stock"], "New product")
                audit(c, u, "product created", d["name"])
            return 201, prod(c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
        row = c.execute("SELECT * FROM products WHERE id=?", (a[1] if len(a) == 2 else "",)).fetchone()
        if not row:
            return 404, {"error": "Product not found"}
        if m == "PUT":
            d = clean(b)
            if not d:
                return 400, {"error": "Check the product details and try again"}
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
            return 200, {"ok": True}
    return 404, {"error": "Not found"}


# The web server part. Python's built-in server calls these methods for each request.
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

    # Handles EVERY request. Anything that is not /api/... gets the page (index.html). API calls need a valid sign-in token and are then
    # passed to api(). If something goes wrong the real error is printed in THIS window (traceback) and the browser gets a friendly message.
    def handle_any(self, m):
        p = urlparse(self.path).path
        if not p.startswith("/api/"):
            with open(os.path.join(ROOT, "index.html"), "rb") as fh:
                b = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        c = new_conn()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if p == "/api/login" and m == "POST":
                return self.reply(*login(c, body))
            if p == "/api/setup/status" and m == "GET":  # public: does this install still need its first admin?
                return self.reply(*setup_status(c))
            if p == "/api/setup" and m == "POST":  # public, but only works while there are no accounts at all
                return self.reply(*setup(c, body))
            if p.startswith("/api/mpesa/callback/") and m == "POST":  # called by Safaricom, protected by the secret in the URL
                sec = mpesa.cfg("CALLBACK_SECRET")
                if not sec or not secrets.compare_digest(p.rsplit("/", 1)[1], sec):
                    return self.reply(404, {"error": "Not found"})
                return self.reply(*mpesa_callback(c, body))
            tok = self.headers.get("Authorization", "").replace("Bearer ", "")
            u = session_user(c, tok)  # who is this? (None if the token is unknown, expired, or the person has been switched off)
            if not u:
                return self.reply(401, {"error": "Please sign in again"})
            if p == "/api/logout":
                TOKENS.pop(tok, None)
                return self.reply(200, {"ok": True})
            if u["must_change_password"] and p not in ("/api/password", "/api/me"):  # until they choose their own password, nothing else works
                return self.reply(403, {"error": "Please choose a new password first.", "must_change": True})
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

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_DELETE = lambda self: self.handle_any("DELETE")


# Background thread: runs the fraud scan when the server starts and then every N minutes (SMARTPOS_SCAN_MINUTES, default 15).
def scan_loop(minutes):
    """Runs the fraud scan at start-up and then every few minutes while the server is running."""
    while True:
        try:
            conn = new_conn()
            try:
                fraud_engine.run(conn)
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        time.sleep(minutes * 60)


# Background thread: while the server runs, finish any M-Pesa payment that lost its callback (see recover_payments).
# SMARTPOS_RECOVERY_MINUTES in .env sets how often it runs (default 2).
def recovery_loop(minutes):
    while True:
        try:
            conn = new_conn()
            try:
                if connectivity.online():
                    recover_payments(conn)
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        time.sleep(minutes * 60)


# START-UP: prepare the database, start the fraud scanner, then serve on http://localhost:8000.
# 127.0.0.1 means only THIS computer can connect. To let tills on the shop network connect you would change it (and add HTTPS first).
if __name__ == "__main__":
    init()
    threading.Thread(target=scan_loop, args=(int(os.environ.get("SMARTPOS_SCAN_MINUTES", "15")),), daemon=True).start()
    threading.Thread(target=recovery_loop, args=(float(os.environ.get("SMARTPOS_RECOVERY_MINUTES", "2")),), daemon=True).start()
    print("SmartPOS running at http://localhost:8000  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
