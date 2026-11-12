"""SmartPOS fraud engine (version 1). Standard library only.

It reads sales, stock movements, M-Pesa payments and login records, and raises findings with a plain-language
reason. It NEVER changes or deletes a business record: its only writes go to the fraud_events table.

  python3 -m fraud             run from the POS folder: scan the last 14 days and print the findings
  python3 -m fraud --days 30

Lives in the fraud/ folder inside the POS project and shares the POS db.py and smartpos.db.

Scores and thresholds below are STARTING values for testing. They are not proven fraud probabilities, and
must be tuned on real data from a pilot shop."""
# =====================================================================================================
# HOW THE FRAUD ENGINE WORKS
#   - It only READS sales, stock movements, payments and sign-in records. It never edits or deletes them.
#   - Each RULE (a function below) looks for one kind of unusual behaviour and yields "findings".
#   - run() saves each NEW finding in the fraud_events table. The same finding is never saved twice (dedupe_key).
#   - A person's risk = the sum of their findings from the last 30 days (max 100), ignoring ones a manager marked legitimate.
#
# TO ADD A RULE:  1) write rule_xxx(c, since, cfg) like the ones below   2) give it a score in SCORES
#                 3) add it to the RULES list   4) add a test in tests/test_fraud.py
# TO TUNE A RULE: change its number in CFG (for example raise writeoff_value if you get too many alerts).
# =====================================================================================================
import json, os, statistics, sys, uuid
from datetime import datetime, timedelta


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the POS folder: shared db.py and database
import db  # noqa: E402

# THRESHOLDS. These are STARTING guesses, not proven values. Tune them using real data from a pilot shop:
# too many false alerts -> raise the number; missing real problems -> lower it.
CFG = dict(
    writeoff_value=1000.0,    # KES. A downward stock adjustment worth at least this is flagged
    repeat_adjustments=3,     # downward adjustments by one person in one day
    open_hour=6, close_hour=22,   # sales or stock changes outside these hours are unusual
    mpesa_failures=5,         # failed, cancelled or timed-out M-Pesa prompts by one cashier in one day
    login_failures=5,         # wrong passwords for one username in one day
    spike_factor=3.0, spike_min_days=5, spike_min_value=2000.0,  # a day far above that cashier's own normal
)
# Points each kind of finding adds to a person's risk.
SCORES = dict(big_writeoff=30, repeat_adjustments=20, after_hours=15, mpesa_review=40,
              mpesa_failures=20, login_failures=25, sales_spike=15)
# Formats cents as "KES 1,560.00" for the plain-language reasons.
KES = lambda cents: f"KES {cents / 100:,.2f}"


# Builds one finding. "key" makes it unique (e.g. the movement id or person+day) so it is never reported twice.
def finding(rule, subject, title, reason, key, when, **evidence):
    return dict(rule=rule, subject=subject, score=SCORES[rule], title=title, reason=reason,
                dedupe_key=f"{rule}|{key}", event_time=when, evidence=evidence)


# RULE: stock removed by hand that is worth a lot (units x cost price). Setting: writeoff_value.
def rule_big_writeoff(c, since, cfg):
    """A manual stock adjustment that REMOVES goods worth at least writeoff_value (units x cost price)."""
    for r in c.execute("SELECT m.id, m.created_at, m.product_name, m.qty_change, m.user_name, m.reason, COALESCE(p.cost_cents,0) cost "
                       "FROM stock_movements m LEFT JOIN products p ON p.id=m.product_id "
                       "WHERE m.type='adjustment' AND m.qty_change<0 AND substr(m.created_at,1,10)>=?", (since,)):
        value = -r["qty_change"] * r["cost"]
        if value >= cfg["writeoff_value"] * 100:
            yield finding("big_writeoff", r["user_name"], "Large stock write-off",
                          f"{r['user_name']} reduced {r['product_name']} by {-r['qty_change']} units (about {KES(value)} at cost). Reason given: "
                          f"{r['reason'] or 'none'}.", r["id"], r["created_at"], product=r["product_name"], units=r["qty_change"], value_kes=value / 100)


# RULE: the same person removes stock by hand several times in one day. Setting: repeat_adjustments.
def rule_repeat_adjustments(c, since, cfg):
    """The same person removes stock by hand several times in one day."""
    for r in c.execute("SELECT user_name, substr(created_at,1,10) d, COUNT(*) n, MAX(created_at) last FROM stock_movements "
                       "WHERE type='adjustment' AND qty_change<0 AND substr(created_at,1,10)>=? GROUP BY 1,2 HAVING n>=?",
                       (since, cfg["repeat_adjustments"])):
        yield finding("repeat_adjustments", r["user_name"], "Repeated stock reductions",
                      f"{r['user_name']} reduced stock by hand {r['n']} times on {r['d']}.", f"{r['user_name']}|{r['d']}", r["last"], count=r["n"])


# RULE: sales or stock changes outside opening hours. Settings: open_hour, close_hour.
def rule_after_hours(c, since, cfg):
    """Sales or manual stock changes outside normal opening hours."""
    h = "CAST(substr(created_at,12,2) AS INTEGER)"
    out = f"({h}<? OR {h}>=?)"
    for r in c.execute(f"SELECT who, d, SUM(n) n, MIN(t) t FROM (SELECT cashier_name who, substr(created_at,1,10) d, COUNT(*) n, MIN(created_at) t FROM sales "
                       f"WHERE substr(created_at,1,10)>=? AND {out} GROUP BY 1,2 UNION ALL SELECT user_name, substr(created_at,1,10), COUNT(*), MIN(created_at) "
                       f"FROM stock_movements WHERE type='adjustment' AND substr(created_at,1,10)>=? AND {out} GROUP BY 1,2) GROUP BY who, d",
                       (since, cfg["open_hour"], cfg["close_hour"], since, cfg["open_hour"], cfg["close_hour"])):
        yield finding("after_hours", r["who"], "Activity outside opening hours",
                      f"{r['who']} made {r['n']} sale or stock change(s) on {r['d']} outside {cfg['open_hour']:02d}:00-{cfg['close_hour']:02d}:00, first at {r['t'][11:16]}.",
                      f"{r['who']}|{r['d']}", r["t"], count=r["n"])


# RULE: M-Pesa money arrived but the sale needs a manager's review (amount mismatch or stock changed).
def rule_mpesa_review(c, since, cfg):
    """M-Pesa money arrived but the sale could not be completed normally (amount mismatch or stock changed)."""
    for r in c.execute("SELECT p.id, p.created_at, p.phone, p.amount_cents, p.result_desc, u.name FROM mpesa_payments p JOIN users u ON u.id=p.cashier_id "
                       "WHERE p.status='paid_review' AND substr(p.created_at,1,10)>=?", (since,)):
        yield finding("mpesa_review", r["name"], "M-Pesa payment needs review",
                      f"A payment of {KES(r['amount_cents'])} from {r['phone']} taken by {r['name']} is on hold: {r['result_desc'] or 'needs checking'}.",
                      r["id"], r["created_at"], amount_kes=r["amount_cents"] / 100)


# RULE: one cashier has many failed or cancelled M-Pesa prompts in a day. Setting: mpesa_failures.
def rule_mpesa_failures(c, since, cfg):
    """One cashier has many failed, cancelled or timed-out M-Pesa prompts in a day."""
    for r in c.execute("SELECT u.name, substr(p.created_at,1,10) d, COUNT(*) n, MAX(p.created_at) last FROM mpesa_payments p JOIN users u ON u.id=p.cashier_id "
                       "WHERE p.status IN ('failed','cancelled','timeout') AND substr(p.created_at,1,10)>=? GROUP BY 1,2 HAVING n>=?",
                       (since, cfg["mpesa_failures"])):
        yield finding("mpesa_failures", r["name"], "Many failed M-Pesa prompts",
                      f"{r['name']} had {r['n']} failed or cancelled M-Pesa prompts on {r['d']}.", f"{r['name']}|{r['d']}", r["last"], count=r["n"])


# RULE: many wrong passwords on one account in a day (guessing). It reports the person's name, not the typed username. Setting: login_failures.
def rule_login_failures(c, since, cfg):
    """Many wrong passwords for one account in a day (password guessing). Reported under the person's name, like every other finding."""
    for r in c.execute("SELECT COALESCE(u.name, a.user_name) who, substr(a.created_at,1,10) d, COUNT(*) n, MAX(a.created_at) last FROM audit_log a "
                       "LEFT JOIN users u ON u.username=lower(a.user_name) WHERE a.action='login failed' AND substr(a.created_at,1,10)>=? "
                       "GROUP BY 1,2 HAVING n>=?", (since, cfg["login_failures"])):
        yield finding("login_failures", r["who"], "Repeated failed sign-ins",
                      f"{r['n']} wrong-password attempts on {r['who']}'s account on {r['d']}.", f"{r['who']}|{r['d']}", r["last"], count=r["n"])


# RULE: a cashier's sales for a day are far above their OWN normal. It needs some history first. Settings: spike_factor, spike_min_days, spike_min_value.
def rule_sales_spike(c, since, cfg):
    """A cashier's daily sales are far above their own normal (needs enough history first)."""
    days = {}
    for r in c.execute("SELECT cashier_name n, substr(created_at,1,10) d, SUM(total_cents) t FROM sales GROUP BY 1,2"):
        days.setdefault(r["n"], {})[r["d"]] = r["t"]
    for who, byday in days.items():
        for d, total in byday.items():
            if d < since or total < cfg["spike_min_value"] * 100:
                continue
            before = [t for dd, t in byday.items() if dd < d and dd >= (datetime.fromisoformat(d) - timedelta(days=14)).strftime("%Y-%m-%d")]
            if len(before) < cfg["spike_min_days"]:
                continue
            mean, sd = statistics.mean(before), statistics.pstdev(before)
            if total > mean + cfg["spike_factor"] * sd and total >= 1.5 * mean:
                yield finding("sales_spike", who, "Unusually high sales day",
                              f"{who} sold {KES(total)} on {d}, against a normal of about {KES(mean)} over the previous {len(before)} trading days.",
                              f"{who}|{d}", d + "T23:59:59", total_kes=total / 100, normal_kes=round(mean / 100, 2))


# The rules the scan runs. Add a new rule's name here.
RULES = [rule_big_writeoff, rule_repeat_adjustments, rule_after_hours, rule_mpesa_review, rule_mpesa_failures, rule_login_failures, rule_sales_spike]


# Runs every rule over the last N days and saves only the NEW findings (INSERT OR IGNORE skips ones already saved). Safe to run as often as you like.
def run(c, days=14, cfg=None):
    """Scan the last `days` days and save any NEW findings. Safe to run as often as you like."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='fraud_events'").fetchone():
        raise RuntimeError("The fraud_events table is missing. Your db.py is older than the fraud module. "
                           "Replace db.py with the latest version, then run again (the table is created automatically).")
    cfg = {**CFG, **(cfg or {})}
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    new = 0
    for rule in RULES:
        for f in rule(c, since, cfg):
            cur = c.execute("INSERT OR IGNORE INTO fraud_events(id,created_at,rule,subject,score,title,reason,evidence_json,event_time,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), datetime.now().isoformat(timespec="seconds"), f["rule"], f["subject"], f["score"], f["title"],
                             f["reason"], json.dumps(f["evidence"]), f["event_time"], f["dedupe_key"]))
            new += cur.rowcount
    c.commit()
    return new


# Turns a score into Low (under 30), Medium (30-59), High (60-79) or Critical (80+).
def band(score):
    return "Low" if score < 30 else "Medium" if score < 60 else "High" if score < 80 else "Critical"


# Risk per person over the last N days. Findings a manager marked 'legitimate' no longer count.
def risk(c, days=30):
    """Risk per person: the sum of their findings from the last `days` days (capped at 100), ignoring ones a manager cleared as legitimate."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = c.execute("SELECT subject, SUM(score) s, COUNT(*) n FROM fraud_events WHERE status<>'legitimate' AND substr(event_time,1,10)>=? "
                     "GROUP BY subject ORDER BY s DESC", (since,)).fetchall()
    return [dict(subject=r["subject"], score=min(100, r["s"]), band=band(min(100, r["s"])), findings=r["n"]) for r in rows]


# The command line: "python3 -m fraud" scans and prints the findings.
def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 14
    if not os.path.exists(db.DB_PATH):  # never silently create an empty database and report "all clear"
        sys.exit(f"No POS database found at {db.DB_PATH}\nPoint the engine at it, for example:\n  SMARTPOS_DB=\"/path/to/smartpos.db\" python3 -m fraud")
    conn = db.init()
    print(f"Database: {db.DB_PATH}")
    try:
        found = run(conn, days)
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"{found} new finding(s) in the last {days} days.\n")
    for r in conn.execute("SELECT * FROM fraud_events WHERE status='new' ORDER BY event_time DESC LIMIT 50"):
        print(f"[{r['score']:>3}] {r['title']} - {r['reason']}")
    print("\nRisk by person (30 days):")
    for x in risk(conn):
        print(f"  {x['subject']:20} {x['score']:>3}  {x['band']:9} ({x['findings']} finding(s))")


if __name__ == "__main__":
    main()
