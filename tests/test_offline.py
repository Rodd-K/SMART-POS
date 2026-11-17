"""Offline tests: what happens when the internet drops. A cash sale must always work; an M-Pesa payment must never be lost or counted twice."""
import os, socket, unittest
from datetime import datetime, timedelta

from test_smartpos import Base, connectivity, mpesa, server  # noqa: F401
import fraud.engine as fe


class Connectivity(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("SMARTPOS_ONLINE_CHECK")
        self._real = connectivity.online
        import importlib
        importlib.reload(connectivity)  # the real functions, not the stand-in the other tests use

    def tearDown(self):
        os.environ.pop("SMARTPOS_ONLINE_CHECK", None)
        if self._env:
            os.environ["SMARTPOS_ONLINE_CHECK"] = self._env

    def test_reachable_means_online_and_unreachable_means_offline(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        os.environ["SMARTPOS_ONLINE_CHECK"] = f"127.0.0.1:{srv.getsockname()[1]}"
        connectivity._cache["t"] = 0
        self.assertTrue(connectivity.online())
        srv.close()  # nothing is listening any more: the "internet" has gone
        connectivity._cache["t"] = 0
        self.assertFalse(connectivity.online())

    def test_answer_is_remembered_for_a_few_seconds(self):
        os.environ["SMARTPOS_ONLINE_CHECK"] = "127.0.0.1:1"  # nothing listens here
        connectivity._cache.update(t=0, ok=True)
        self.assertFalse(connectivity.online())
        os.environ["SMARTPOS_ONLINE_CHECK"] = ""
        self.assertFalse(connectivity.online(max_age=60))  # still the remembered answer, no new check


class OfflineBase(Base):
    def setUp(self):
        super().setUp()
        self.queries = []
        self.answer = {"ResultCode": "0"}
        self._query, mpesa.stk_query = mpesa.stk_query, self.fake_query

    def tearDown(self):
        mpesa.stk_query = self._query
        super().tearDown()

    def fake_query(self, checkout_id):
        self.queries.append(checkout_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def prompt(self, ref="m1", age_seconds=300, qty=2):
        """A customer is sent an M-Pesa prompt, then the 'internet drops': the payment stays pending, aged by age_seconds."""
        p = self.product("Soda 500ml")
        code, r = self.api(self.cashier, "POST", "/api/mpesa/start", {"client_ref": ref, "phone": "0708374149", "items": [{"product_id": p["id"], "qty": qty}]})
        self.assertEqual(code, 201, r)
        old = (datetime.now() - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
        self.c.execute("UPDATE mpesa_payments SET created_at=? WHERE client_ref=?", (old, ref))
        return p

    def payment(self, ref="m1"):
        return self.c.execute("SELECT * FROM mpesa_payments WHERE client_ref=?", (ref,)).fetchone()

    def recover(self):
        return server.recover_payments(self.c)


class OutageBehaviour(OfflineBase):
    def test_cash_sales_work_with_no_internet(self):
        connectivity.online = lambda *a, **k: False
        p = self.product("Soda 500ml")
        self.assertEqual(self.cash_sale([{"product_id": p["id"], "qty": 1}], "cash-offline")[0], 201)

    def test_mpesa_is_refused_quickly_with_a_clear_message_when_offline(self):
        connectivity.online = lambda *a, **k: False
        p = self.product("Soda 500ml")
        code, r = self.api(self.cashier, "POST", "/api/mpesa/start", {"client_ref": "x", "phone": "0708374149", "items": [{"product_id": p["id"], "qty": 1}]})
        self.assertEqual(code, 503)
        self.assertIn("take cash", r["error"])
        self.assertEqual((self.pushes, self.count("mpesa_payments")), (0, 0))  # no request was sent, nothing was recorded

    def test_health_tells_the_screen_whether_we_are_online(self):
        self.assertTrue(self.api(self.cashier, "GET", "/api/health")[1]["online"])
        connectivity.online = lambda *a, **k: False
        self.assertFalse(self.api(self.cashier, "GET", "/api/health")[1]["online"])


class Recovery(OfflineBase):
    def test_a_payment_whose_callback_was_lost_is_recovered(self):
        p = self.prompt()
        self.assertEqual(self.recover(), 1)
        pay = self.payment()
        self.assertEqual((pay["status"], pay["result_desc"]), ("paid", "Recovered after a connection problem"))
        self.assertEqual((self.count("sales"), self.stock_of(p["id"])), (1, p["stock"] - 2))
        self.assertEqual(self.recover(), 0)  # running again changes nothing
        self.assertEqual(self.count("sales"), 1)

    def test_a_late_real_callback_after_recovery_does_nothing(self):
        self.prompt()
        self.recover()
        server.mpesa_callback(self.c, {"Body": {"stkCallback": {"CheckoutRequestID": "ws_1", "ResultCode": 0, "ResultDesc": "ok",
                                                                "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 140}, {"Name": "MpesaReceiptNumber", "Value": "REAL1"}]}}}})
        self.assertEqual(self.count("sales"), 1)

    def test_no_connection_to_safaricom_means_try_again_later(self):
        self.prompt()
        self.answer = OSError("no route to host")
        self.assertEqual(self.recover(), 0)
        self.assertEqual((self.payment()["status"], self.count("sales")), ("pending", 0))
        self.answer = {"ResultCode": "0"}  # the internet is back
        self.assertEqual(self.recover(), 1)
        self.assertEqual(self.payment()["status"], "paid")

    def test_declined_payments_are_closed_and_record_nothing(self):
        for code, status in (("1032", "cancelled"), ("2001", "failed"), ("1", "failed")):
            ref = f"d{code}"
            self.prompt(ref)
            self.answer = {"ResultCode": code}
            self.recover()
            self.assertEqual(self.payment(ref)["status"], status, code)
        self.assertEqual(self.count("sales"), 0)

    def test_no_answer_after_two_minutes_becomes_a_timeout(self):
        self.prompt("old", age_seconds=300)
        self.prompt("fresh", age_seconds=60)
        self.answer = {"errorCode": "500.001.1001"}  # Safaricom: still processing, no result code
        self.recover()
        self.assertEqual((self.payment("old")["status"], self.payment("fresh")["status"]), ("timeout", "pending"))

    def test_very_new_payments_are_left_alone(self):
        self.prompt(age_seconds=5)
        self.assertEqual(self.recover(), 0)
        self.assertEqual(self.queries, [])  # the cashier's screen is still handling this one

    def test_nothing_runs_in_simulated_mode(self):
        self.prompt()
        os.environ["MPESA_MODE"] = "simulated"
        try:
            self.assertEqual(self.recover(), 0)
        finally:
            os.environ["MPESA_MODE"] = "sandbox"


class DoubleSaleGuard(OfflineBase):
    """The cashier gave up waiting and took CASH for the same goods. If the M-Pesa payment turns up later it must NOT sell them twice."""

    def cashier_takes_cash_instead(self, p):
        self.assertEqual(self.cash_sale([{"product_id": p["id"], "qty": 2}], "cash-instead", tendered="200")[0], 201)

    def test_recovery_holds_the_payment_for_a_manager(self):
        p = self.prompt(age_seconds=300)
        self.cashier_takes_cash_instead(p)
        self.recover()
        pay = self.payment()
        self.assertEqual(pay["status"], "paid_review")
        self.assertIn("double payment", pay["result_desc"])
        self.assertEqual((self.count("sales"), self.stock_of(p["id"])), (1, p["stock"] - 2))  # only the cash sale exists

    def test_a_late_callback_is_held_the_same_way(self):
        p = self.prompt(age_seconds=300)
        self.cashier_takes_cash_instead(p)
        server.mpesa_callback(self.c, {"Body": {"stkCallback": {"CheckoutRequestID": "ws_1", "ResultCode": 0, "ResultDesc": "ok",
                                                                "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 140}, {"Name": "MpesaReceiptNumber", "Value": "LATE1"}]}}}})
        pay = self.payment()
        self.assertEqual((pay["status"], pay["receipt"], self.count("sales")), ("paid_review", "LATE1", 1))

    def test_the_manager_is_alerted(self):
        p = self.prompt(age_seconds=300)
        self.cashier_takes_cash_instead(p)
        self.recover()
        fe.run(self.c)
        alert = self.c.execute("SELECT * FROM fraud_events WHERE rule='mpesa_review'").fetchone()
        self.assertIsNotNone(alert)
        self.assertEqual(alert["subject"], self.cashier["name"])

    def test_another_cashiers_sales_do_not_block_it(self):
        p = self.prompt(age_seconds=300)
        self.c.execute("INSERT INTO users(id,username,name,role,salt,hash,active,created_at) VALUES('u2','cashier2','Second Cashier','cashier','00','00',1,'2026-01-01T00:00:00')")
        other = dict(self.c.execute("SELECT * FROM users WHERE id='u2'").fetchone())
        self.assertEqual(self.api(other, "POST", "/api/sales", {"client_ref": "o", "method": "cash", "tendered": "100", "items": [{"product_id": p["id"], "qty": 1}]})[0], 201)
        self.recover()
        self.assertEqual(self.payment()["status"], "paid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
