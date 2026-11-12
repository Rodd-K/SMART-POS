"""SmartPOS automated tests. Run from the project folder:

    python3 -m unittest discover -s tests -v

They use a temporary database and a fake Daraja, so your real data and keys are never touched.
Run them before every commit. All tests must pass."""
import glob, json, os, re, shutil, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update(MPESA_MODE="sandbox", MPESA_CONSUMER_KEY="k", MPESA_CONSUMER_SECRET="s", MPESA_PASSKEY="p", MPESA_SHORTCODE="174379",
                  MPESA_TILL="174379", MPESA_CALLBACK_URL="https://example.test", MPESA_CALLBACK_SECRET="testsecret")
os.environ["SMARTPOS_DB"] = os.path.join(tempfile.mkdtemp(), "unused.db")
import db, mpesa, server, exporter  # noqa: E402


class FileHealth(unittest.TestCase):
    """Catches broken merges before they reach the browser."""

    def files(self, *patterns):
        return [f for p in patterns for f in glob.glob(os.path.join(ROOT, p))]

    def test_no_merge_conflict_markers(self):
        marker = re.compile("^(" + "<" * 7 + "|" + "=" * 7 + "|" + ">" * 7 + ")", re.M)
        for f in self.files("*.py", "*.html", "*.example", "tests/*.py", "fraud/*.py", "hardware/*.py"):
            with self.subTest(file=os.path.basename(f)):
                with open(f, encoding="utf-8") as fh:
                    self.assertIsNone(marker.search(fh.read()), "merge conflict marker found")

    def test_python_files_compile(self):
        for f in self.files("*.py", "tests/*.py", "fraud/*.py", "hardware/*.py"):
            with self.subTest(file=os.path.basename(f)):
                with open(f, encoding="utf-8") as fh:
                    compile(fh.read(), f, "exec")  # syntax check only, writes nothing

    def test_page_javascript_is_valid(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed; JavaScript check skipped")
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        path = os.path.join(tempfile.mkdtemp(), "page.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(self.dir, "test.db")
        open(db.DB_PATH, "wb").close()  # lets init() create a fresh seeded database
        server.TOKENS.clear()
        server.init()
        self.c = server.new_conn()
        self.admin, self.cashier = self.user("admin"), self.user("cashier")
        self.pushes = 0
        self._push, mpesa.stk_push = mpesa.stk_push, self.fake_push

    def tearDown(self):
        mpesa.stk_push = self._push
        self.c.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def user(self, role):
        return dict(self.c.execute("SELECT * FROM users WHERE role=?", (role,)).fetchone())

    def fake_push(self, phone, amount, ref, desc="SmartPOS"):
        self.pushes += 1
        return {"ResponseCode": "0", "CheckoutRequestID": f"ws_{self.pushes}", "MerchantRequestID": "m"}

    def api(self, user, method, path, body=None):
        return server.api(self.c, method, path, user, body or {})

    def products(self):
        return self.api(self.admin, "GET", "/api/products")[1]

    def product(self, name):
        return next(p for p in self.products() if p["name"] == name)

    def cash_sale(self, items, ref, tendered="1000"):
        return self.api(self.cashier, "POST", "/api/sales", {"client_ref": ref, "method": "cash", "tendered": tendered, "items": items})

    def stock_of(self, pid):
        return self.c.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()[0]

    def count(self, table):
        return self.c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class AccessAndRoles(Base):
    def test_login(self):
        self.assertEqual(server.login(self.c, {"username": "admin", "password": "wrong"})[0], 401)
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)

    def test_cashier_cannot_use_admin_routes(self):
        for path in ("/api/dashboard", "/api/movements", "/api/mpesa"):
            self.assertIn(self.api(self.cashier, "GET", path)[0], (403, 404), path)  # refused either way
        self.assertEqual(self.api(self.cashier, "POST", "/api/products", {"name": "x", "price": "1", "stock": 1})[0], 403)

    def test_admin_cannot_process_sales(self):
        self.assertEqual(self.api(self.admin, "POST", "/api/sales", {})[0], 403)

    def test_cashier_never_sees_cost_prices(self):
        rows = self.api(self.cashier, "GET", "/api/products")[1]
        self.assertTrue(rows and all("cost" not in r for r in rows))


class Sales(Base):
    def test_cash_sale_updates_everything(self):
        p = self.product("White Bread 400g")
        code, s = self.cash_sale([{"product_id": p["id"], "qty": 3}], "s1", tendered="500")
        self.assertEqual((code, s["total"], s["change"]), (201, "195.00", "305.00"))
        self.assertEqual(self.stock_of(p["id"]), p["stock"] - 3)
        self.assertEqual(self.c.execute("SELECT qty_change FROM stock_movements WHERE type='sale'").fetchone()[0], -3)

    def test_money_is_exact(self):
        self.api(self.admin, "POST", "/api/products", {"name": "Odd Price", "price": "19.99", "cost": "10", "stock": 10})
        p = self.product("Odd Price")
        self.assertEqual(self.cash_sale([{"product_id": p["id"], "qty": 3}], "s2")[1]["total"], "59.97")

    def test_same_request_twice_is_one_sale(self):
        p = self.product("Soda 500ml")
        items = [{"product_id": p["id"], "qty": 2}]
        first, again = self.cash_sale(items, "dup")[1], self.cash_sale(items, "dup")[1]
        self.assertEqual(first["receipt_no"], again["receipt_no"])
        self.assertEqual((self.count("sales"), self.stock_of(p["id"])), (1, p["stock"] - 2))

    def test_refuses_more_than_stock_and_changes_nothing(self):
        p = self.product("Onions 1kg")
        code, r = self.cash_sale([{"product_id": p["id"], "qty": p["stock"] + 1}], "big")
        self.assertEqual(code, 409)
        self.assertEqual((self.count("sales"), self.stock_of(p["id"])), (0, p["stock"]))

    def test_rejects_short_cash_and_empty_cart(self):
        p = self.product("Soda 500ml")
        self.assertEqual(self.cash_sale([{"product_id": p["id"], "qty": 1}], "short", tendered="10")[0], 400)
        self.assertEqual(self.cash_sale([], "empty")[0], 400)
        self.assertEqual(self.count("sales"), 0)

    def test_last_unit_cannot_be_sold_twice(self):
        self.api(self.admin, "POST", "/api/products", {"name": "Last One", "price": "10", "cost": "5", "stock": 1})
        p, results = self.product("Last One"), []

        def buy(i):
            conn = server.new_conn()
            results.append(server.api(conn, "POST", "/api/sales", self.cashier, {"client_ref": f"r{i}", "method": "cash", "tendered": "10",
                                                                                   "items": [{"product_id": p["id"], "qty": 1}]})[0])
            conn.close()
        threads = [threading.Thread(target=buy, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [201] + [409] * 11)
        self.assertEqual(self.stock_of(p["id"]), 0)


class StockManagement(Base):
    def test_create_edit_delete_are_recorded(self):
        code, p = self.api(self.admin, "POST", "/api/products", {"name": "Widget", "category": "Test", "price": "5", "cost": "2", "stock": 10})
        self.assertEqual(code, 201)
        self.assertEqual(self.api(self.admin, "PUT", f"/api/products/{p['id']}", {**p, "stock": 7, "reason": "recount"})[0], 200)
        types = [r[0] for r in self.c.execute("SELECT type FROM stock_movements WHERE product_id=? ORDER BY rowid", (p["id"],))]
        self.assertEqual(types, ["opening", "adjustment"])
        self.assertEqual(self.api(self.admin, "DELETE", f"/api/products/{p['id']}")[0], 200)
        self.assertEqual(self.count("audit_log") >= 3, True)

    def test_deleting_a_product_keeps_sales_history(self):
        p = self.product("Tissue Roll")
        self.cash_sale([{"product_id": p["id"], "qty": 1}], "hist")
        self.api(self.admin, "DELETE", f"/api/products/{p['id']}")
        self.assertEqual(self.count("sale_items"), 1)

    def test_invalid_product_is_rejected(self):
        for bad in ({"name": "", "price": "1", "stock": 1}, {"name": "x", "price": "-1", "stock": 1}, {"name": "x", "price": "1", "stock": -3}):
            self.assertEqual(self.api(self.admin, "POST", "/api/products", bad)[0], 400)

    def test_database_itself_refuses_negative_stock(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE products SET stock=-1")


class MpesaFlow(Base):
    def start(self, ref="m1", qty=2):
        p = self.product("Soda 500ml")
        code, r = self.api(self.cashier, "POST", "/api/mpesa/start", {"client_ref": ref, "phone": "0708374149", "items": [{"product_id": p["id"], "qty": qty}]})
        self.assertEqual(code, 201, r)
        return p

    def callback(self, checkout="ws_1", code=0, amount=140, receipt="QWE123"):
        items = [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt}] if code == 0 else []
        return server.mpesa_callback(self.c, {"Body": {"stkCallback": {"CheckoutRequestID": checkout, "ResultCode": code, "ResultDesc": "x",
                                                                       "CallbackMetadata": {"Item": items}}}})

    def status(self, ref="m1"):
        return self.api(self.cashier, "GET", f"/api/mpesa/status/{ref}")[1]

    def test_nothing_is_recorded_until_payment_is_confirmed(self):
        p = self.start()
        self.assertEqual((self.status()["status"], self.count("sales"), self.stock_of(p["id"])), ("pending", 0, p["stock"]))

    def test_successful_payment_records_one_sale(self):
        p = self.start()
        self.callback()
        self.callback()  # Safaricom may send the same callback twice
        s = self.status()
        self.assertEqual((s["status"], s["sale"]["payment_ref"], self.count("sales")), ("paid", "QWE123", 1))
        self.assertEqual(self.stock_of(p["id"]), p["stock"] - 2)

    def test_cancelled_payment_records_nothing(self):
        p = self.start()
        self.callback(code=1032)
        self.assertEqual((self.status()["status"], self.count("sales"), self.stock_of(p["id"])), ("cancelled", 0, p["stock"]))

    def test_wrong_amount_is_held_for_review(self):
        self.start()
        self.callback(amount=1)
        self.assertEqual((self.status()["status"], self.count("sales")), ("paid_review", 0))

    def test_client_cannot_fake_an_mpesa_sale(self):
        p = self.product("Soda 500ml")
        code, _ = self.api(self.cashier, "POST", "/api/sales", {"method": "mpesa", "items": [{"product_id": p["id"], "qty": 1}]})
        self.assertEqual((code, self.count("sales")), (400, 0))

    def test_fractional_totals_are_refused(self):
        self.api(self.admin, "POST", "/api/products", {"name": "Cents", "price": "10.50", "cost": "5", "stock": 5})
        p = self.product("Cents")
        code, _ = self.api(self.cashier, "POST", "/api/mpesa/start", {"client_ref": "f", "phone": "0708374149", "items": [{"product_id": p["id"], "qty": 1}]})
        self.assertEqual((code, self.pushes), (400, 0))

    def test_phone_numbers_are_normalised(self):
        for raw in ("0708374149", "+254708374149", "708374149", "254708374149"):
            self.assertEqual(mpesa.norm(raw), "254708374149", raw)
        self.assertIsNone(mpesa.norm("12345"))


class HttpAndExport(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.admin_tok, self.cashier_tok = self.token("admin", "admin123"), self.token("cashier", "cashier123")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token} if token else {})
        try:
            r = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            r = e
        return r.getcode(), r.read()

    def token(self, u, p):
        return json.loads(self.call("POST", "/api/login", {"username": u, "password": p})[1])["token"]

    def test_page_is_served_and_api_needs_sign_in(self):
        self.assertIn(b"SmartPOS", self.call("GET", "/")[1])
        self.assertEqual(self.call("GET", "/api/products")[0], 401)

    def test_admin_can_export_and_cashier_cannot(self):
        self.cash_sale([{"product_id": self.product("Soda 500ml")["id"], "qty": 1}], "e1")
        code, data = self.call("GET", "/api/export/sales", token=self.admin_tok)
        self.assertEqual(code, 200)
        self.assertTrue(data.startswith(b"\xef\xbb\xbfReceipt"))  # BOM so Excel reads it as UTF-8
        self.assertEqual(self.call("GET", "/api/export/all", token=self.admin_tok)[1][:2], b"PK")  # a ZIP file
        self.assertEqual(self.call("GET", "/api/export/sales", token=self.cashier_tok)[0], 403)

    def test_export_rejects_bad_dates_and_unknown_names(self):
        self.assertEqual(self.call("GET", "/api/export/sales?from=2026-13-45", token=self.admin_tok)[0], 400)
        self.assertEqual(self.call("GET", "/api/export/sales?from=2026-12-01&to=2026-01-01", token=self.admin_tok)[0], 400)
        self.assertEqual(self.call("GET", "/api/export/passwords", token=self.admin_tok)[0], 400)

    def test_export_blocks_spreadsheet_formulas(self):
        self.api(self.admin, "POST", "/api/products", {"name": "=2+2", "price": "1", "cost": "1", "stock": 1})
        text = self.call("GET", "/api/export/products", token=self.admin_tok)[1].decode("utf-8-sig")
        self.assertIn("'=2+2", text)

    def test_export_never_includes_passwords(self):
        blob = self.call("GET", "/api/export/all", token=self.admin_tok)[1]
        self.assertNotIn(b"pbkdf2", blob)
        self.assertNotIn(self.admin["hash"].encode(), blob)

    def test_fraud_reports_download_for_admins_only(self):
        for name in ("fraud", "fraud_summary"):
            code, data = self.call("GET", f"/api/export/{name}?from=2026-01-01&to=2026-12-31", token=self.admin_tok)
            self.assertEqual((code, data[:3]), (200, b"\xef\xbb\xbf"), name)
            self.assertEqual(self.call("GET", f"/api/export/{name}", token=self.cashier_tok)[0], 403)

    def test_fraud_alert_routes_refuse_cashiers_over_http(self):
        self.assertEqual(self.call("GET", "/api/fraud/events/open", token=self.cashier_tok)[0], 403)
        self.assertEqual(self.call("GET", "/api/fraud/events/open", token=self.admin_tok)[0], 200)

    def test_callback_needs_the_secret(self):
        self.assertEqual(self.call("POST", "/api/mpesa/callback/wrongsecret", {"Body": {}})[0], 404)
        ok = {"Body": {"stkCallback": {"CheckoutRequestID": "none", "ResultCode": 1, "ResultDesc": "x"}}}
        self.assertEqual(self.call("POST", "/api/mpesa/callback/testsecret", ok)[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
