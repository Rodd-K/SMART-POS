"""Manual M-Pesa checkout tests: what a cashier may do when there is no internet, and how it is checked afterwards."""
import csv, io, os, unittest
from datetime import datetime, timedelta

from test_smartpos import Base, connectivity, server  # noqa: F401
from test_fraud import FraudBase
import exporter


class ManualCheckout(Base):
    def go_offline(self):
        connectivity.online = lambda *a, **k: False

    def manual(self, user=None, ref="mn1", code="SKL4X5Y6Z7", qty=2, amount="140", **extra):
        p = self.product("Soda 500ml")
        body = {"client_ref": ref, "items": [{"product_id": p["id"], "qty": qty}], "code": code, "amount": amount, "phone": "0708374149", **extra}
        return self.api(user or self.cashier, "POST", "/api/mpesa/manual", body), p

    def test_offline_checkout_records_the_sale_as_unverified(self):
        self.go_offline()
        (code, sale), p = self.manual()
        self.assertEqual((code, sale["method"], sale["payment_ref"], sale["total"]), (201, "mpesa", "SKL4X5Y6Z7", "140.00"))
        self.assertEqual(self.stock_of(p["id"]), p["stock"] - 2)
        row = self.c.execute("SELECT * FROM mpesa_manual").fetchone()
        self.assertEqual((row["status"], row["online_when_taken"], row["phone"], row["amount_cents"]), ("unverified", 0, "254708374149", 14000))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='mpesa manual'").fetchone()[0], 1)

    def test_online_the_cashier_must_use_the_prompt(self):
        (code, r), p = self.manual()  # the system is online and the cashier has NOT pressed Offline
        self.assertEqual(code, 409)
        self.assertIn("online", r["error"])
        self.assertEqual((self.count("sales"), self.count("mpesa_manual")), (0, 0))

    def test_pressing_offline_while_online_is_allowed_but_recorded(self):
        (code, sale), p = self.manual(override=True)
        self.assertEqual(code, 201)
        self.assertEqual(self.c.execute("SELECT online_when_taken FROM mpesa_manual").fetchone()[0], 1)  # a warning sign, kept on record
        self.assertIn("system was online", self.c.execute("SELECT details FROM audit_log WHERE action='mpesa manual'").fetchone()[0])

    def test_the_same_request_twice_is_one_sale(self):
        self.go_offline()
        before = self.product("Soda 500ml")["stock"]
        self.manual()
        (code, again), p = self.manual()
        self.assertEqual((code, self.count("sales"), self.count("mpesa_manual")), (200, 1, 1))
        self.assertEqual(self.stock_of(p["id"]), before - 2)  # stock went down once, not twice

    def test_a_code_can_never_pay_for_two_sales(self):
        self.go_offline()
        self.manual()
        (code, r), _ = self.manual(ref="mn2")  # same code, a different sale
        self.assertEqual(code, 409)
        self.assertIn("already been used", r["error"])
        self.assertEqual(self.count("sales"), 1)

    def test_a_code_that_belongs_to_a_real_confirmed_payment_is_refused(self):
        self.go_offline()
        self.c.execute("INSERT INTO mpesa_payments VALUES('p1','2026-01-01T10:00:00','x',?,'254700000000',1000,'[]','ws1','m','paid','','REAL123456')", (self.cashier["id"],))
        (code, r), _ = self.manual(code="REAL123456")
        self.assertEqual(code, 409)

    def test_bad_input_is_refused_and_nothing_is_recorded(self):
        self.go_offline()
        for kw, want in ((dict(code="SHORT1"), 400), (dict(code="ABCDEFGHIJ"), 400), (dict(code="1234567890"), 400), (dict(code=""), 400),
                         (dict(amount="100"), 400), (dict(amount=""), 400), (dict(amount="abc"), 400), (dict(phone="123"), 400), (dict(qty=0), 400)):
            self.assertEqual(self.manual(ref="r" + str(abs(hash(str(kw)))), **kw)[0][0], want, kw)
        self.assertEqual((self.count("sales"), self.count("mpesa_manual")), (0, 0))

    def test_the_code_is_tidied_up(self):
        self.go_offline()
        (code, sale), _ = self.manual(code=" skl4x 5y6z7 ")
        self.assertEqual((code, sale["payment_ref"]), (201, "SKL4X5Y6Z7"))

    def test_not_enough_stock_records_nothing(self):
        self.go_offline()
        p = self.product("Onions 1kg")
        code, r = self.api(self.cashier, "POST", "/api/mpesa/manual", {"client_ref": "big", "items": [{"product_id": p["id"], "qty": p["stock"] + 1}],
                                                                   "code": "SKL4X5Y6Z7", "amount": "99999"})
        self.assertEqual(code, 409)
        self.assertEqual((self.count("sales"), self.count("mpesa_manual")), (0, 0))

    def test_a_limit_per_payment(self):
        self.go_offline()
        os.environ["SMARTPOS_MANUAL_MPESA_MAX"] = "100"
        self.addCleanup(lambda: os.environ.pop("SMARTPOS_MANUAL_MPESA_MAX", None))
        (code, r), _ = self.manual()  # KES 140 is over the KES 100 limit
        self.assertEqual(code, 400)
        self.assertIn("limited", r["error"])

    def test_it_can_be_switched_off(self):
        self.go_offline()
        os.environ["SMARTPOS_MANUAL_MPESA"] = "off"
        self.addCleanup(lambda: os.environ.pop("SMARTPOS_MANUAL_MPESA", None))
        self.assertEqual(self.manual()[0][0], 403)

    def test_only_cashiers_can_checkout_and_only_admins_can_review(self):
        self.go_offline()
        self.assertEqual(self.manual(user=self.admin)[0][0], 403)
        self.manual()
        mid = self.c.execute("SELECT id FROM mpesa_manual").fetchone()[0]
        self.assertIn(self.api(self.cashier, "GET", "/api/mpesa/manual")[0], (403, 404))  # refused either way
        self.assertIn(self.api(self.cashier, "POST", f"/api/mpesa/manual/{mid}/review", {"status": "verified"})[0], (403, 404))  # refused either way
        self.assertEqual(self.c.execute("SELECT status FROM mpesa_manual WHERE id=?", (mid,)).fetchone()[0], "unverified")  # and nothing changed

    def test_a_manager_verifies_or_disputes(self):
        self.go_offline()
        self.manual()
        row = self.api(self.admin, "GET", "/api/mpesa/manual")[1][0]
        self.assertEqual((row["status"], row["code"], row["cashier"], row["amount"]), ("unverified", "SKL4X5Y6Z7", self.cashier["name"], 140))
        path = f"/api/mpesa/manual/{row['id']}/review"
        self.assertEqual(self.api(self.admin, "POST", path, {"status": "disputed", "note": ""})[0], 400)  # a note is needed
        self.assertEqual(self.api(self.admin, "POST", path, {"status": "maybe"})[0], 400)
        self.assertEqual(self.api(self.admin, "POST", path, {"status": "disputed", "note": "Not on the statement"})[0], 200)
        row = self.api(self.admin, "GET", "/api/mpesa/manual")[1][0]
        self.assertEqual((row["status"], row["checked_by"], row["note"]), ("disputed", self.admin["name"], "Not on the statement"))
        self.assertEqual(self.api(self.admin, "POST", path, {"status": "verified"})[0], 200)
        self.assertEqual(self.api(self.admin, "POST", "/api/mpesa/manual/nope/review", {"status": "verified"})[0], 404)

    def test_the_online_prompt_message_now_points_to_checkout(self):
        self.go_offline()
        p = self.product("Soda 500ml")
        code, r = self.api(self.cashier, "POST", "/api/mpesa/start", {"client_ref": "x", "phone": "0708374149", "items": [{"product_id": p["id"], "qty": 1}]})
        self.assertEqual(code, 503)
        self.assertIn("Checkout", r["error"])

    def test_the_export_lists_them_for_checking_against_the_statement(self):
        self.go_offline()
        self.manual(override=False)
        rows = list(csv.reader(io.StringIO(exporter.run(self.c, "mpesa_manual", {})[1].decode("utf-8-sig"))))
        r = dict(zip(rows[0], rows[1]))
        self.assertEqual((r["M-Pesa code"], r["Amount (KES)"], r["Status"], r["Taken while the system was online"]), ("SKL4X5Y6Z7", "140.00", "unverified", "no"))


class ManualFraudRules(FraudBase):
    def take(self, code, ref, online=False):
        connectivity.online = lambda *a, **k: False
        p = self.product("Soda 500ml")
        res = self.api(self.cashier, "POST", "/api/mpesa/manual", {"client_ref": ref, "items": [{"product_id": p["id"], "qty": 1}], "code": code, "amount": "70",
                                                                "override": online})
        self.assertEqual(res[0], 201, res)
        if online:  # make the record look like it was taken while the system was online
            self.c.execute("UPDATE mpesa_manual SET online_when_taken=1 WHERE code=?", (code,))

    def test_a_normal_offline_payment_raises_nothing(self):
        self.take("AAAA111111", "a")
        self.scan()
        self.assertEqual([r for r in self.found() if r["rule"].startswith("manual_")], [])

    def test_taken_while_online_is_flagged(self):
        self.take("AAAA111111", "a", online=True)
        self.scan()
        f = self.found("manual_online")
        self.assertEqual((len(f), f[0]["score"], f[0]["subject"]), (1, 40, self.cashier["name"]))
        self.assertIn("AAAA111111", f[0]["reason"])

    def test_left_unchecked_too_long_is_flagged(self):
        self.take("AAAA111111", "a")
        self.scan()
        self.assertEqual(self.found("manual_unverified"), [])  # still fresh
        old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
        self.c.execute("UPDATE mpesa_manual SET created_at=?", (old,))
        self.scan()
        self.assertEqual(len(self.found("manual_unverified")), 1)

    def test_a_verified_payment_is_not_flagged_for_being_old(self):
        self.take("AAAA111111", "a")
        old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
        self.c.execute("UPDATE mpesa_manual SET created_at=?, status='verified'", (old,))
        self.scan()
        self.assertEqual(self.found("manual_unverified"), [])

    def test_a_disputed_payment_is_flagged(self):
        self.take("AAAA111111", "a")
        self.c.execute("UPDATE mpesa_manual SET status='disputed'")
        self.scan()
        f = self.found("manual_disputed")
        self.assertEqual((len(f), f[0]["score"]), (1, 50))

    def test_using_it_often_in_one_day_is_flagged(self):
        self.take("AAAA111111", "a")
        self.take("BBBB222222", "b")
        self.scan()
        self.assertEqual(self.found("manual_frequent"), [])
        self.take("CCCC333333", "c")
        self.scan()
        self.assertEqual(len(self.found("manual_frequent")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
