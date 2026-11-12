"""Hardware tests: receipt layout, printer connections, print rules. A fake printer (a local network socket) stands in for the real one."""
import os, socket, tempfile, threading, time, unittest
from datetime import datetime, timedelta

from test_smartpos import Base, server  # noqa: F401
from hardware import escpos, printer

CFG = dict(width=48, name="Test Shop", lines=["Nairobi", "Tel 0700 000 000"], footer="Thank you!", drawer=True, pin=0, spec="", autoprint=False)
SALE = dict(receipt_no="R2611100001", created_at="2026-11-10T14:05:09", cashier_name="Cashier One", method="cash", payment_ref="",
            total="195.00", tendered="200.00", change="5.00",
            items=[dict(product_name="Soda 500ml", qty=2, unit_price="70.00", line_total="140.00"),
                   dict(product_name="Bread 400g", qty=1, unit_price="55.00", line_total="55.00")])


class FakePrinter:
    """Listens like a network receipt printer and keeps what it is sent."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port, self.jobs = self.srv.getsockname()[1], []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.jobs.append(data)
            conn.close()

    def wait(self, n=1):
        for _ in range(100):
            if len(self.jobs) >= n:
                break
            time.sleep(0.02)
        return self.jobs

    def close(self):
        self.srv.close()


class ReceiptLayout(unittest.TestCase):
    def test_lines_fit_the_paper_and_amounts_line_up(self):
        for width in (48, 32):
            d = escpos.receipt(SALE, {**CFG, "width": width})
            self.assertTrue(all(len(t) <= width for t in d.text), f"a line is wider than {width}")
        d = escpos.receipt(SALE, CFG)
        row = next(t for t in d.text if "2 x Soda" in t)
        self.assertEqual((len(row), row.endswith("140.00")), (48, True))
        text = "\n".join(d.text)
        for want in ("Test Shop", "R2611100001", "Cashier One", "TOTAL (KES)", "195.00", "Cash received", "Change", "@ 70.00 each", "Thank you!"):
            self.assertIn(want, text)

    def test_long_product_names_wrap(self):
        sale = {**SALE, "items": [dict(product_name="Extra Long Product Name " * 4, qty=1, unit_price="9.00", line_total="9.00")]}
        d = escpos.receipt(sale, CFG)
        self.assertTrue(all(len(t) <= 48 for t in d.text))
        self.assertTrue(any(t.rstrip().endswith("9.00") for t in d.text))

    def test_accents_and_symbols_are_made_safe(self):
        sale = {**SALE, "items": [dict(product_name="Café Crème ñ \u20ac \u2713", qty=1, unit_price="1.00", line_total="1.00")]}
        data = escpos.receipt(sale, CFG).bytes()
        self.assertIn(b"Cafe Creme n", data)
        data.decode("ascii")  # nothing a printer could misread

    def test_cut_and_drawer_commands(self):
        self.assertIn(b"\x1dVB\x00", escpos.receipt(SALE, CFG).bytes())  # paper cut
        self.assertNotIn(b"\x1bp", escpos.receipt(SALE, CFG).bytes())
        self.assertIn(b"\x1bp\x00", escpos.receipt(SALE, CFG, kick=True).bytes())
        self.assertIn(b"\x1bp\x01", escpos.receipt(SALE, {**CFG, "pin": 1}, kick=True).bytes())

    def test_duplicate_copies_are_marked(self):
        self.assertNotIn("DUPLICATE", "\n".join(escpos.receipt(SALE, CFG).text))
        self.assertIn("DUPLICATE COPY 2", "\n".join(escpos.receipt(SALE, CFG, copy=2).text))

    def test_mpesa_receipt_shows_the_reference(self):
        text = "\n".join(escpos.receipt({**SALE, "method": "mpesa", "payment_ref": "QWE123ABC"}, CFG).text)
        self.assertIn("Paid by M-Pesa", text)
        self.assertIn("QWE123ABC", text)
        self.assertNotIn("Cash received", text)


class Connections(unittest.TestCase):
    def test_network_printer_receives_the_bytes(self):
        fp = FakePrinter()
        self.addCleanup(fp.close)
        printer.send(b"hello", f"network:127.0.0.1:{fp.port}")
        self.assertEqual(fp.wait(), [b"hello"])

    def test_device_file_receives_the_bytes(self):
        path = os.path.join(tempfile.mkdtemp(), "lp0")
        printer.send(b"abc", f"file:{path}")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abc")

    def test_helpful_errors(self):
        with self.assertRaisesRegex(printer.PrinterError, "SMARTPOS_PRINTER"):
            printer.send(b"x", "")
        with self.assertRaisesRegex(printer.PrinterError, "Could not reach"):
            printer.send(b"x", "network:127.0.0.1:1")  # nothing is listening there
        with self.assertRaisesRegex(printer.PrinterError, "Unknown printer type"):
            printer.send(b"x", "carrier-pigeon:coop")


class PrintingInThePos(Base):
    def setUp(self):
        super().setUp()
        self.fp = FakePrinter()
        os.environ["SMARTPOS_PRINTER"] = f"network:127.0.0.1:{self.fp.port}"
        os.environ["SMARTPOS_BUSINESS_NAME"] = "Test Shop"

    def tearDown(self):
        os.environ.pop("SMARTPOS_PRINTER", None)
        os.environ.pop("SMARTPOS_BUSINESS_NAME", None)
        self.fp.close()
        super().tearDown()

    def sale(self, ref="p1"):
        return self.cash_sale([{"product_id": self.product("Soda 500ml")["id"], "qty": 2}], ref, tendered="200")[1]

    def print_(self, user, no):
        return self.api(user, "POST", f"/api/print/receipt/{no}")

    def test_first_print_opens_the_drawer_and_a_reprint_is_marked_a_duplicate(self):
        s = self.sale()
        self.assertEqual(self.print_(self.cashier, s["receipt_no"]), (200, {"printed": True, "copy": 1}))
        self.assertEqual(self.print_(self.cashier, s["receipt_no"])[1]["copy"], 2)
        first, second = self.fp.wait(2)
        self.assertIn(s["receipt_no"].encode(), first)
        self.assertIn(b"\x1bp", first)  # cash sale, first copy: drawer opens
        self.assertNotIn(b"\x1bp", second)  # a reprint never opens the drawer
        self.assertIn(b"DUPLICATE COPY 1", second)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='receipt printed'").fetchone()[0], 2)

    def test_mpesa_sale_does_not_open_the_drawer(self):
        code, s = server.sell(self.c, self.cashier, {"client_ref": "mp", "method": "mpesa", "items": [{"product_id": self.product("Soda 500ml")["id"], "qty": 1}]})
        self.assertEqual(self.print_(self.cashier, s["receipt_no"])[0], 200)
        self.assertNotIn(b"\x1bp", self.fp.wait()[0])

    def test_cashier_cannot_print_old_or_other_peoples_receipts(self):
        s = self.sale()
        old = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
        self.c.execute("UPDATE sales SET created_at=? WHERE receipt_no=?", (old, s["receipt_no"]))
        self.assertEqual(self.print_(self.cashier, s["receipt_no"])[0], 403)
        self.assertEqual(self.print_(self.admin, s["receipt_no"])[0], 200)  # an admin can reprint any time

    def test_unknown_receipt_and_missing_printer(self):
        self.assertEqual(self.print_(self.cashier, "R000")[0], 404)
        s = self.sale()
        os.environ["SMARTPOS_PRINTER"] = ""
        code, r = self.print_(self.cashier, s["receipt_no"])
        self.assertEqual(code, 503)
        self.assertIn("SMARTPOS_PRINTER", r["error"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='receipt printed'").fetchone()[0], 0)  # nothing printed, nothing logged

    def test_hardware_page_is_admin_only(self):
        self.assertEqual(self.api(self.cashier, "GET", "/api/hardware")[0], 403)
        self.assertEqual(self.api(self.cashier, "POST", "/api/hardware/test-print")[0], 403)
        info = self.api(self.admin, "GET", "/api/hardware")[1]
        self.assertEqual((info["printer"]["configured"], info["printer"]["type"], info["drawer"]["enabled"]), (True, "network", True))

    def test_test_page_and_drawer_test(self):
        self.assertEqual(self.api(self.admin, "POST", "/api/hardware/test-print")[0], 200)
        self.assertEqual(self.api(self.admin, "POST", "/api/hardware/test-drawer")[0], 200)
        page, drawer = self.fp.wait(2)
        self.assertIn(b"PRINTER TEST", page)
        self.assertIn(b"\x1bp", drawer)

    def test_login_tells_the_screen_whether_a_printer_is_set_up(self):
        self.assertTrue(server.login(self.c, {"username": "cashier", "password": "cashier123"})[1]["user"]["printer"])
        os.environ["SMARTPOS_PRINTER"] = "none"
        self.assertFalse(server.login(self.c, {"username": "cashier", "password": "cashier123"})[1]["user"]["printer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
