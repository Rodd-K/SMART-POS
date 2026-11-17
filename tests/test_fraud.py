"""Fraud engine tests. Each rule must fire on its scenario and stay quiet on normal behaviour."""
# Fraud tests. Each rule must fire on its own scenario and stay quiet on normal behaviour. Helpers like add_sale() and add_move() insert test records directly with chosen dates.
import csv, io, itertools, os, tempfile, unittest, zipfile
from datetime import datetime, timedelta

from test_smartpos import Base, db, server  # noqa: F401  (also sets up the temporary database)
import exporter
from fraud import engine as fe

_n = itertools.count(1)


def ts(days_ago=0, hour=12, minute=0):
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat(timespec="seconds")


# Helpers that create sales, stock movements and payments at a chosen time, and run the scan.
class FraudBase(Base):
    def add_sale(self, when, cents=10000, user=None):
        u, n = user or self.cashier, next(_n)
        self.c.execute("INSERT INTO sales VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"s{n}", f"RX{n}", f"rx{n}", when, u["id"], u["name"], "cash", "", cents, cents, 0))

    def add_move(self, when, change, who=None, kind="adjustment", product="Soda 500ml"):
        p, n = self.product(product), next(_n)
        self.c.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?,?,?)", (f"m{n}", when, p["id"], product, kind, change, 0, "test", who or self.admin["name"]))

    def add_payment(self, status, when=None):
        n = next(_n)
        self.c.execute("INSERT INTO mpesa_payments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"p{n}", when or ts(0, 12), f"pr{n}", self.cashier["id"], "254708374149", 5000, "[]", f"ws{n}", "m", status, "test", ""))

    def scan(self):
        return fe.run(self.c)

    def found(self, rule=None):
        q, a = "SELECT * FROM fraud_events", ()
        if rule:
            q, a = q + " WHERE rule=?", (rule,)
        return self.c.execute(q, a).fetchall()


# One test per rule: what makes it fire, and what must NOT.
class Rules(FraudBase):
    def test_a_normal_day_raises_nothing(self):
        for h in (9, 11, 14, 17):
            self.add_sale(ts(0, h))
        self.add_move(ts(0, 10), -2)  # Soda cost KES 52: a small write-off
        self.assertEqual(self.scan(), 0)

    def test_large_stock_write_off(self):
        self.add_move(ts(0, 10), -5)
        self.scan()
        self.assertEqual(self.found("big_writeoff"), [])  # 5 x KES 52 is too small to flag
        self.add_move(ts(0, 11), -30)  # 30 x KES 52 = KES 1,560
        self.scan()
        f = self.found("big_writeoff")
        self.assertEqual((len(f), f[0]["subject"], f[0]["score"]), (1, self.admin["name"], 30))
        self.assertIn("30 units", f[0]["reason"])

    def test_adding_stock_is_not_flagged(self):
        self.add_move(ts(0, 10), +500)
        self.assertEqual(self.scan(), 0)

    def test_repeated_adjustments_need_three(self):
        self.add_move(ts(0, 9), -1)
        self.add_move(ts(0, 10), -1)
        self.scan()
        self.assertEqual(self.found("repeat_adjustments"), [])
        self.add_move(ts(0, 11), -1)
        self.scan()
        self.assertEqual(len(self.found("repeat_adjustments")), 1)

    def test_after_hours_activity(self):
        self.add_sale(ts(0, 14))
        self.assertEqual(self.scan(), 0)
        self.add_sale(ts(0, 23, 30))
        self.scan()
        f = self.found("after_hours")
        self.assertEqual((len(f), f[0]["subject"]), (1, self.cashier["name"]))
        self.assertIn("23:30", f[0]["reason"])

    def test_mpesa_payment_on_hold(self):
        self.add_payment("paid")
        self.add_payment("paid_review")
        self.scan()
        f = self.found("mpesa_review")
        self.assertEqual((len(f), f[0]["score"]), (1, 40))

    def test_many_failed_mpesa_prompts(self):
        for _ in range(4):
            self.add_payment("failed")
        self.scan()
        self.assertEqual(self.found("mpesa_failures"), [])
        self.add_payment("cancelled")
        self.scan()
        self.assertEqual(len(self.found("mpesa_failures")), 1)

    def test_password_guessing(self):
        for _ in range(4):
            server.login(self.c, {"username": "cashier", "password": "nope"})
        self.scan()
        self.assertEqual(self.found("login_failures"), [])
        server.login(self.c, {"username": "cashier", "password": "nope"})
        self.scan()
        self.assertEqual(len(self.found("login_failures")), 1)
        self.assertEqual(self.found("login_failures")[0]["subject"], self.cashier["name"])  # the person's name, not the typed username

    def test_sales_spike_against_own_history(self):
        for i, amount in enumerate((95000, 100000, 105000, 98000, 102000, 100000), start=1):
            self.add_sale(ts(i, 12), amount)
        self.add_sale(ts(0, 12), 110000)  # a normal day
        self.assertEqual(self.scan(), 0)
        self.add_sale(ts(0, 13), 600000)  # KES 7,100 in total today
        self.scan()
        self.assertEqual(len(self.found("sales_spike")), 1)

    def test_no_spike_alert_without_enough_history(self):
        self.add_sale(ts(1, 12), 100000)
        self.add_sale(ts(0, 12), 900000)
        self.assertEqual(self.scan(), 0)


# How the engine behaves: no duplicates, never changes business records, risk scoring.
class Behaviour(FraudBase):
    def test_running_twice_does_not_duplicate(self):
        self.add_move(ts(0, 10), -30)
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(len(self.found()), 1)

    def test_engine_never_changes_business_records(self):
        self.add_sale(ts(0, 23))
        self.add_move(ts(0, 10), -30)
        self.add_payment("paid_review")
        tables = ("sales", "sale_items", "products", "stock_movements", "mpesa_payments", "users")
        snap = lambda: [self.c.execute(f"SELECT COUNT(*), COALESCE(SUM(rowid),0) FROM {t}").fetchone()[:] for t in tables]
        before = snap()
        self.assertGreater(self.scan(), 0)
        self.assertEqual(snap(), before)

    def test_risk_score_bands(self):
        self.assertEqual([fe.band(s) for s in (0, 29, 30, 59, 60, 79, 80, 100)],
                         ["Low", "Low", "Medium", "Medium", "High", "High", "Critical", "Critical"])

    def test_risk_adds_up_and_clears_when_marked_legitimate(self):
        self.add_move(ts(0, 23, 30), -30)  # write-off (30) + after hours (15)
        self.add_payment("paid_review")  # a different person, 40
        self.scan()
        r = {x["subject"]: x for x in fe.risk(self.c)}
        self.assertEqual((r[self.admin["name"]]["score"], r[self.admin["name"]]["band"]), (45, "Medium"))
        self.c.execute("UPDATE fraud_events SET status='legitimate' WHERE rule='big_writeoff'")
        self.assertEqual({x["subject"]: x["score"] for x in fe.risk(self.c)}[self.admin["name"]], 15)

    def test_risk_is_capped_at_100(self):
        for i in range(6):
            self.c.execute("INSERT INTO fraud_events(id,created_at,rule,subject,score,title,reason,event_time,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?)",
                           (f"e{i}", ts(0), "x", "Someone", 40, "t", "r", ts(0), f"k{i}"))
        self.assertEqual(fe.risk(self.c)[0]["score"], 100)

    def test_decision_must_be_a_known_status(self):
        import sqlite3
        self.add_move(ts(0, 10), -30)
        self.scan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE fraud_events SET status='guilty'")


# The admin Fraud tab behind the scenes: list, scan, decisions, admin-only.
class FraudScreen(FraudBase):
    """The admin Fraud tab: list, scan, decisions. Admin only, and never touches business records."""

    def make_alert(self):
        self.add_move(ts(0, 10), -30)  # a large write-off
        self.assertGreaterEqual(self.api(self.admin, "POST", "/api/fraud/scan")[1]["new"], 1)
        return next(e for e in self.api(self.admin, "GET", "/api/fraud/events/open")[1] if e["rule"] == "big_writeoff")

    def decide(self, alert, status, note="", user=None):
        return self.api(user or self.admin, "POST", f"/api/fraud/{alert['id']}/decision", {"status": status, "note": note})

    def test_alerts_are_for_admins_only(self):
        a = self.make_alert()
        for method, path in (("GET", "/api/fraud/summary"), ("GET", "/api/fraud/events/open"), ("POST", "/api/fraud/scan")):
            self.assertEqual(self.api(self.cashier, method, path)[0], 403, path)
        self.assertEqual(self.decide(a, "legitimate", "fine", user=self.cashier)[0], 403)
        self.assertEqual(self.api(self.cashier, "GET", "/api/dashboard")[0], 403)

    def test_scan_lists_an_explained_alert_without_duplicates(self):
        a = self.make_alert()
        self.assertEqual((a["rule"], a["level"], a["status"], a["subject"]), ("big_writeoff", "Medium", "new", self.admin["name"]))
        self.assertIn("30 units", a["reason"])
        s = self.api(self.admin, "GET", "/api/fraud/summary")[1]
        self.assertEqual((s["new"], s["open"], s["risk"][0]["score"]), (1, 1, 30))
        self.assertEqual(self.api(self.admin, "POST", "/api/fraud/scan")[1]["new"], 0)

    def test_decision_needs_a_valid_status_and_a_note(self):
        a = self.make_alert()
        self.assertEqual(self.decide(a, "guilty", "x")[0], 400)
        self.assertEqual(self.decide(a, "suspected_fraud", "  ")[0], 400)
        self.assertEqual(self.decide(a, "investigating")[0], 200)  # no note needed while still looking

    def test_decision_is_recorded_and_clears_the_risk(self):
        a = self.make_alert()
        code, r = self.decide(a, "legitimate", "Damaged goods, owner approved")
        self.assertEqual((code, r["status"], r["reviewed_by"], r["note"]), (200, "legitimate", self.admin["name"], "Damaged goods, owner approved"))
        self.assertTrue(r["reviewed_at"])
        self.assertEqual(self.api(self.admin, "GET", "/api/fraud/summary")[1]["risk"], [])
        self.assertEqual(self.api(self.admin, "GET", "/api/fraud/events/open")[1], [])
        self.assertEqual(len(self.api(self.admin, "GET", "/api/fraud/events/legitimate")[1]), 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='fraud decision'").fetchone()[0], 1)

    def test_unknown_alert_and_unknown_filter(self):
        self.assertEqual(self.api(self.admin, "POST", "/api/fraud/nope/decision", {"status": "legitimate", "note": "abc"})[0], 404)
        self.assertEqual(self.api(self.admin, "GET", "/api/fraud/events/bogus")[0], 400)

    def test_scanning_and_deciding_never_change_business_records(self):
        self.add_sale(ts(0, 23))
        a = self.make_alert()
        tables = ("sales", "sale_items", "products", "stock_movements", "mpesa_payments", "users")
        snap = lambda: [tuple(self.c.execute(f"SELECT COUNT(*), COALESCE(SUM(rowid),0), COALESCE(SUM(stock),0) FROM {t}" if t == "products"
                                              else f"SELECT COUNT(*), COALESCE(SUM(rowid),0), 0 FROM {t}").fetchone()) for t in tables]
        before = snap()
        self.decide(a, "suspected_fraud", "Checked the stock room")
        self.api(self.admin, "POST", "/api/fraud/scan")
        self.assertEqual(snap(), before)

    def test_dashboard_counts_new_alerts(self):
        self.assertEqual(self.api(self.admin, "GET", "/api/dashboard")[1]["fraud_new"], 0)
        a = self.make_alert()
        self.assertEqual(self.api(self.admin, "GET", "/api/dashboard")[1]["fraud_new"], 1)
        self.decide(a, "investigating")
        self.assertEqual(self.api(self.admin, "GET", "/api/dashboard")[1]["fraud_new"], 0)


# The fraud report and summary on the Export page.
class FraudReports(FraudBase):
    """The fraud report and summary on the Export page."""

    def sheet(self, name, frm=None, to=None):
        q = {k: [v] for k, v in (("from", frm), ("to", to)) if v}
        res = exporter.run(self.c, name, q)
        self.assertIsNotNone(res)
        return list(csv.reader(io.StringIO(res[1].decode("utf-8-sig"))))

    def test_report_lists_findings_with_the_managers_decision(self):
        self.add_move(ts(0, 10), -30)
        self.scan()
        eid = self.found("big_writeoff")[0]["id"]
        self.c.execute("UPDATE fraud_events SET status='legitimate', reviewed_by='Boss', reviewed_at=?, note='=SUM(1)' WHERE id=?", (ts(0, 15), eid))
        rows = self.sheet("fraud")
        self.assertEqual(rows[0][2:8], ["Person", "Rule", "Finding", "Score", "Risk level", "Reason"])
        self.assertEqual(len(rows), 2)
        r = dict(zip(rows[0], rows[1]))
        self.assertEqual((r["Person"], r["Rule"], r["Score"], r["Risk level"], r["Status"], r["Decided by"]), (self.admin["name"], "big_writeoff", "30", "Medium", "legitimate", "Boss"))
        self.assertEqual(r["Manager note"], "'=SUM(1)")  # a spreadsheet cannot run it as a formula

    def test_report_respects_the_date_range(self):
        self.add_move(ts(40, 10), -30)
        self.add_move(ts(0, 10), -30)
        fe.run(self.c, days=60)
        day = lambda n: (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
        self.assertEqual(len(self.sheet("fraud")) - 1, 2)  # no dates: everything
        self.assertEqual(len(self.sheet("fraud", day(7), day(0))) - 1, 1)  # this week only
        self.assertEqual(len(self.sheet("fraud", day(45), day(35))) - 1, 1)  # the old one only
        self.assertEqual(len(self.sheet("fraud", day(20), day(10))) - 1, 0)  # a quiet stretch

    def test_summary_by_person(self):
        self.add_move(ts(0, 23, 30), -30)  # a write-off (30) and after-hours activity (15) by the same admin
        self.scan()
        r = dict(zip(*self.sheet("fraud_summary")[:2]))
        self.assertEqual((r["Person"], r["Findings"], r["Risk level"], r["Still to review"]), (self.admin["name"], "2", "Medium", "2"))
        self.assertEqual(r["Risk score (max 100, cleared findings excluded)"], "45")
        self.c.execute("UPDATE fraud_events SET status='legitimate' WHERE rule='big_writeoff'")
        r = dict(zip(*self.sheet("fraud_summary")[:2]))
        self.assertEqual((r["Risk score (max 100, cleared findings excluded)"], r["Risk level"], r["Legitimate"], r["Still to review"]), ("15", "Low", "1", "1"))

    def test_everything_zip_includes_the_fraud_files(self):
        z = zipfile.ZipFile(io.BytesIO(exporter.run(self.c, "all", {})[1]))
        self.assertTrue({"fraud.csv", "fraud_summary.csv"} <= set(z.namelist()))

    def test_bad_dates_are_refused(self):
        self.assertIsNone(exporter.run(self.c, "fraud", {"from": ["2026-13-40"]}))
        self.assertIsNone(exporter.run(self.c, "fraud_summary", {"from": ["2026-12-01"], "to": ["2026-01-01"]}))


# An older database receives the fraud table when the software is updated.
class DatabaseUpgrade(unittest.TestCase):
    def test_a_version_1_database_receives_every_later_upgrade(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = db.connect(path)
        c.executescript(db.SCHEMA)  # exactly what a database from before the fraud module looked like
        c.execute("PRAGMA user_version=1")
        c.close()
        c = db.init(path)  # the software is updated: the missing upgrades run, once each
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM fraud_events").fetchone()[0], 0)  # upgrade 2
        self.assertEqual(c.execute("SELECT COUNT(*) FROM settings").fetchone()[0], 0)  # upgrade 3
        self.assertEqual(c.execute("SELECT COUNT(*) FROM mpesa_manual").fetchone()[0], 0)  # upgrade 4
        self.assertIn("must_change_password", [r[1] for r in c.execute("PRAGMA table_info(users)")])
        db.init(path).close()  # starting again must change nothing and must not fail

    def test_clear_message_when_db_py_is_out_of_date(self):
        c = db.connect(os.path.join(tempfile.mkdtemp(), "v1.db"))
        c.executescript(db.SCHEMA)  # a database with only the original tables, as an old db.py would make
        with self.assertRaises(RuntimeError) as cm:
            fe.run(c)
        self.assertIn("Replace db.py", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
