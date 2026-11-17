"""Account tests: first-run set-up, staff, passwords, lockout, sessions, and two businesses staying completely separate."""
import json, os, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

from test_smartpos import Base, db, server  # noqa: F401
from test_hardware import FakePrinter
import accounts
import reset_admin


class FreshInstall(unittest.TestCase):
    """A brand-new installation: no demo accounts at all, exactly as a shop would get it."""

    def setUp(self):
        self._demo = os.environ.pop("SMARTPOS_DEMO", None)
        self._path = db.DB_PATH
        self.dir = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(self.dir, "fresh.db")
        open(db.DB_PATH, "wb").close()
        server.TOKENS.clear()
        server.init()
        self.c = server.new_conn()

    def tearDown(self):
        self.c.close()
        db.DB_PATH = self._path
        if self._demo:
            os.environ["SMARTPOS_DEMO"] = self._demo

    def setup(self, **kw):
        body = dict(business="Mama Njeri Shop", name="Njeri Wanjiku", username="njeri", password="Fresh-Start-77")
        return server.setup(self.c, {**body, **kw})

    def test_a_new_install_has_no_accounts_and_no_demo_data(self):
        self.assertEqual(server.setup_status(self.c)[1], {"needed": True, "business": ""})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        self.assertEqual(server.login(self.c, {"username": "admin", "password": "admin123"})[0], 401)  # no default password exists

    def test_setup_creates_the_admin_and_signs_them_in(self):
        code, r = self.setup()
        self.assertEqual((code, r["user"]["role"], r["user"]["business"], r["user"]["must_change"]), (200, "admin", "Mama Njeri Shop", False))
        self.assertEqual(server.setup_status(self.c)[1], {"needed": False, "business": "Mama Njeri Shop"})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='setup'").fetchone()[0], 1)

    def test_setup_works_only_once(self):
        self.assertEqual(self.setup()[0], 200)
        code, r = self.setup(username="intruder", password="Another-Pass-99")
        self.assertEqual(code, 403)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_setup_refuses_weak_or_missing_details(self):
        for bad in (dict(password="short"), dict(password="password"), dict(password="njeri-njeri-1"), dict(username="N"), dict(business=""), dict(name=" ")):
            self.assertEqual(self.setup(**bad)[0], 400, bad)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_the_business_name_is_used_on_receipts(self):
        self.setup()
        fp = FakePrinter()
        self.addCleanup(fp.close)
        os.environ["SMARTPOS_PRINTER"] = f"network:127.0.0.1:{fp.port}"
        self.addCleanup(lambda: os.environ.pop("SMARTPOS_PRINTER", None))
        admin = dict(self.c.execute("SELECT * FROM users").fetchone())
        self.assertEqual(server.api(self.c, "POST", "/api/hardware/test-print", admin, {})[0], 200)
        self.assertIn(b"Mama Njeri Shop", fp.wait()[0])


class TwoBusinesses(unittest.TestCase):
    """The pilot model: each business has its OWN installation. Nothing is shared, so nothing can leak."""

    def test_two_installations_share_nothing(self):
        saved, demo = db.DB_PATH, os.environ.pop("SMARTPOS_DEMO", None)
        try:
            dbs = {}
            for name, pw in (("A", "Shop-A-secret-1"), ("B", "Shop-B-secret-2")):
                db.DB_PATH = os.path.join(tempfile.mkdtemp(), f"{name}.db")
                open(db.DB_PATH, "wb").close()
                server.init()
                c = server.new_conn()
                server.setup(c, dict(business=f"Business {name}", name=f"Owner {name}", username="owner", password=pw))
                c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,5,'',?)", (f"p{name}", f"{name} only product", "X", 1000, 500, 5, "2026-01-01T00:00:00"))
                dbs[name] = (db.DB_PATH, c)
            for name, other in (("A", "B"), ("B", "A")):
                c = dbs[name][1]
                self.assertEqual(server.setting(c, "business_name"), f"Business {name}")
                self.assertEqual([r[0] for r in c.execute("SELECT name FROM products")], [f"{name} only product"])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            # the same username on both: each password opens only its own business
            ca, cb = dbs["A"][1], dbs["B"][1]
            self.assertEqual(server.login(ca, {"username": "owner", "password": "Shop-A-secret-1"})[0], 200)
            self.assertEqual(server.login(ca, {"username": "owner", "password": "Shop-B-secret-2"})[0], 401)
            self.assertEqual(server.login(cb, {"username": "owner", "password": "Shop-B-secret-2"})[0], 200)
            self.assertEqual(server.login(cb, {"username": "owner", "password": "Shop-A-secret-1"})[0], 401)
        finally:
            db.DB_PATH = saved
            if demo:
                os.environ["SMARTPOS_DEMO"] = demo


class Passwords(unittest.TestCase):
    def test_rules(self):
        self.assertIsNone(accounts.check_password("Fresh-Start-77", "njeri"))
        for weak in ("short1", "password", "admin123", "cashier123", "aaaaaaaaaa", "njeri-is-me-1"):
            self.assertIsNotNone(accounts.check_password(weak, "njeri"), weak)
        self.assertIsNone(accounts.check_username("mary.k"))
        for bad in ("Mary", "ab", "has space", "x" * 31, ""):
            self.assertIsNotNone(accounts.check_username(bad), bad)


class Staff(Base):
    def new_person(self, username="mary", role="cashier", password="Start-Pass-123", user=None):
        return self.api(user or self.admin, "POST", "/api/users", {"name": "Mary Atieno", "username": username, "role": role, "password": password})

    def test_only_admins_manage_staff(self):
        for method, path in (("GET", "/api/users"), ("POST", "/api/users")):
            self.assertEqual(self.api(self.cashier, method, path, {})[0], 403)
        victim = self.cashier["id"]
        for method, path in (("PUT", f"/api/users/{victim}"), ("POST", f"/api/users/{victim}/reset-password"), ("POST", f"/api/users/{victim}/unlock")):
            self.assertEqual(self.api(self.cashier, method, path, {"password": "Whatever-123"})[0], 403)

    def test_adding_a_cashier_and_their_first_sign_in(self):
        code, u = self.new_person()
        self.assertEqual((code, u["role"], u["must_change"], u["active"]), (201, "cashier", True, True))
        code, r = server.login(self.c, {"username": "mary", "password": "Start-Pass-123"})
        self.assertEqual((code, r["user"]["must_change"]), (200, True))  # must choose their own password
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='user created'").fetchone()[0], 1)

    def test_bad_new_accounts_are_refused(self):
        self.assertEqual(self.new_person(password="weak")[0], 400)
        self.assertEqual(self.new_person(username="Bad Name")[0], 400)
        self.assertEqual(self.new_person(role="boss")[0], 400)
        self.assertEqual(self.new_person()[0], 201)
        self.assertEqual(self.new_person()[0], 409)  # the username is taken

    def test_listing_never_contains_password_data(self):
        self.new_person()
        blob = json.dumps(self.api(self.admin, "GET", "/api/users")[1])
        self.assertNotIn("hash", blob)
        self.assertNotIn("salt", blob)
        self.assertNotIn(self.admin["hash"], blob)

    def test_switching_someone_off_keeps_their_history_and_stops_sign_in(self):
        p = self.product("Soda 500ml")
        self.cash_sale([{"product_id": p["id"], "qty": 1}], "h1")
        self.assertEqual(self.api(self.admin, "PUT", f"/api/users/{self.cashier['id']}", {"active": False})[0], 200)
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 401)
        self.assertEqual(self.count("sales"), 1)  # the sale is still there, still linked to them
        self.api(self.admin, "PUT", f"/api/users/{self.cashier['id']}", {"active": True})
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)

    def test_there_is_always_one_active_admin(self):
        me = self.admin["id"]
        self.assertEqual(self.api(self.admin, "PUT", f"/api/users/{me}", {"active": False})[0], 400)
        self.assertEqual(self.api(self.admin, "PUT", f"/api/users/{me}", {"role": "cashier"})[0], 400)
        code, second = self.new_person("boss", "admin")
        self.assertEqual(self.api(self.admin, "PUT", f"/api/users/{me}", {"role": "cashier"})[0], 200)  # fine now: another admin exists
        boss = dict(self.c.execute("SELECT * FROM users WHERE username='boss'").fetchone())
        self.assertEqual(self.api(boss, "PUT", f"/api/users/{boss['id']}", {"active": False})[0], 400)  # but not the last one

    def test_reset_password_forces_a_new_choice_and_unlocks(self):
        for _ in range(accounts.MAX_FAILURES):
            server.login(self.c, {"username": "cashier", "password": "nope"})
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 429)  # locked
        self.assertEqual(self.api(self.admin, "POST", f"/api/users/{self.cashier['id']}/reset-password", {"password": "weak"})[0], 400)
        self.assertEqual(self.api(self.admin, "POST", f"/api/users/{self.cashier['id']}/reset-password", {"password": "Reset-Pass-456"})[0], 200)
        code, r = server.login(self.c, {"username": "cashier", "password": "Reset-Pass-456"})
        self.assertEqual((code, r["user"]["must_change"]), (200, True))
        log = " ".join(x[0] for x in self.c.execute("SELECT details FROM audit_log"))
        self.assertNotIn("Reset-Pass-456", log)  # a password is never written to the log

    def test_people_cannot_be_deleted(self):
        self.assertEqual(self.api(self.admin, "DELETE", f"/api/users/{self.cashier['id']}")[0], 404)


class SigningIn(Base):
    def test_lockout_after_repeated_wrong_passwords(self):
        for i in range(accounts.MAX_FAILURES):
            self.assertEqual(server.login(self.c, {"username": "cashier", "password": "nope"})[0], 401)
        code, r = server.login(self.c, {"username": "cashier", "password": "cashier123"})  # even the RIGHT password is refused now
        self.assertEqual(code, 429)
        self.assertIn("locked", r["error"])
        self.api(self.admin, "POST", f"/api/users/{self.cashier['id']}/unlock")
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)

    def test_a_lock_runs_out_by_itself(self):
        for _ in range(accounts.MAX_FAILURES):
            server.login(self.c, {"username": "cashier", "password": "nope"})
        self.c.execute("UPDATE users SET locked_until='2000-01-01T00:00:00' WHERE username='cashier'")
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)

    def test_a_good_sign_in_clears_the_count(self):
        for _ in range(accounts.MAX_FAILURES - 1):
            server.login(self.c, {"username": "cashier", "password": "nope"})
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)
        for _ in range(accounts.MAX_FAILURES - 1):
            server.login(self.c, {"username": "cashier", "password": "nope"})
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 200)  # never reached the limit

    def test_unknown_usernames_cannot_be_locked_out_or_discovered(self):
        for _ in range(10):
            self.assertEqual(server.login(self.c, {"username": "nobody", "password": "x"})[0], 401)

    def test_changing_your_own_password(self):
        path = "/api/password"
        self.assertEqual(self.api(self.cashier, "POST", path, {"current": "wrong", "new": "Brand-New-Pass-1"})[0], 403)
        self.assertEqual(self.api(self.cashier, "POST", path, {"current": "cashier123", "new": "weak"})[0], 400)
        self.assertEqual(self.api(self.cashier, "POST", path, {"current": "cashier123", "new": "cashier123"})[0], 400)
        self.assertEqual(self.api(self.cashier, "POST", path, {"current": "cashier123", "new": "Brand-New-Pass-1"})[0], 200)
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "cashier123"})[0], 401)
        self.assertEqual(server.login(self.c, {"username": "cashier", "password": "Brand-New-Pass-1"})[0], 200)

    def test_the_demo_passwords_are_refused_on_a_real_install(self):
        os.environ.pop("SMARTPOS_DEMO")
        try:
            code, r = server.login(self.c, {"username": "admin", "password": "admin123"})
            self.assertEqual((code, r["user"]["must_change"]), (200, True))
        finally:
            os.environ["SMARTPOS_DEMO"] = "1"

    def test_emergency_reset_tool(self):
        for _ in range(accounts.MAX_FAILURES):
            server.login(self.c, {"username": "admin", "password": "nope"})
        self.c.execute("UPDATE users SET active=0 WHERE username='admin'")
        with self.assertRaises(ValueError):
            reset_admin.reset(self.c, "admin", "weak")
        with self.assertRaises(ValueError):
            reset_admin.reset(self.c, "ghost", "Whatever-Pass-1")
        reset_admin.reset(self.c, "admin", "Recovery-Pass-9")
        code, r = server.login(self.c, {"username": "admin", "password": "Recovery-Pass-9"})
        self.assertEqual((code, r["user"]["must_change"]), (200, True))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='password reset (command line)'").fetchone()[0], 1)


class SessionsOverHttp(Base):
    """Real web requests: the rules a browser actually hits."""

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

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
        return r.getcode(), json.loads(r.read() or b"{}")

    def sign_in(self, user, pw):
        return self.call("POST", "/api/login", {"username": user, "password": pw})[1]["token"]

    def test_switching_someone_off_signs_them_out_immediately(self):
        tok = self.sign_in("cashier", "cashier123")
        self.assertEqual(self.call("GET", "/api/products", token=tok)[0], 200)
        self.api(self.admin, "PUT", f"/api/users/{self.cashier['id']}", {"active": False})
        self.assertEqual(self.call("GET", "/api/products", token=tok)[0], 401)

    def test_an_idle_session_expires(self):
        tok = self.sign_in("cashier", "cashier123")
        self.assertEqual(self.call("GET", "/api/products", token=tok)[0], 200)
        server.TOKENS[tok]["seen"] -= 10 ** 6
        self.assertEqual(self.call("GET", "/api/products", token=tok)[0], 401)

    def test_nothing_works_until_the_new_password_is_chosen(self):
        self.api(self.admin, "POST", f"/api/users/{self.cashier['id']}/reset-password", {"password": "Starter-Pass-12"})
        tok = self.sign_in("cashier", "Starter-Pass-12")
        code, r = self.call("GET", "/api/products", token=tok)
        self.assertEqual((code, r.get("must_change")), (403, True))
        self.assertEqual(self.call("GET", "/api/me", token=tok)[0], 200)  # the page can still ask who it is
        self.assertEqual(self.call("POST", "/api/password", {"current": "Starter-Pass-12", "new": "My-Own-Pass-34"}, tok)[0], 200)
        self.assertEqual(self.call("GET", "/api/products", token=tok)[0], 200)

    def test_setup_endpoints_are_closed_once_accounts_exist(self):
        code, r = self.call("GET", "/api/setup/status")
        self.assertEqual((code, r["needed"]), (200, False))
        self.assertEqual(self.call("POST", "/api/setup", {"business": "Evil", "name": "X", "username": "evil", "password": "Evil-Pass-123"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
