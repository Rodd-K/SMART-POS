"""SmartPOS database layer: SQLite schema and connection. Standard library only.
Money is stored as whole cents (INTEGER) so totals never suffer rounding errors."""
# =====================================================================================================
# The DATABASE DESIGN lives here. SQLite keeps everything in one file (smartpos.db next to this file).
#
# Tables:  users, products, sales, sale_items, stock_movements, audit_log, mpesa_payments, fraud_events
# Money is stored as whole cents (INTEGER). Times are stored as text like 2026-11-10T14:05:09.
#
# HOW TO CHANGE THE DESIGN SAFELY: never edit an old MIGRATIONS entry. Add a NEW one with the next number
# (3, 4, ...). On start-up every database applies the ones it is missing, so shops upgrade without losing data.
# Then add a test (see tests/test_fraud.py, class DatabaseUpgrade).
# =====================================================================================================
import os, sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
# Where the database file lives. Normally "smartpos.db" next to this file; set SMARTPOS_DB to use another (the tests do).
DB_PATH = os.environ.get("SMARTPOS_DB", os.path.join(ROOT, "smartpos.db"))

# Version 1 of the design. It is only applied to a brand-new, empty database.
SCHEMA = """
-- Staff accounts. role is 'admin' or 'cashier'. hash+salt = the scrambled password.
CREATE TABLE users(
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','cashier')),
  salt TEXT NOT NULL,
  hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);

-- The catalogue. The CHECKs make the database itself refuse negative prices or stock.
CREATE TABLE products(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General',
  price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
  cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),      -- the database itself refuses negative stock
  reorder_level INTEGER NOT NULL DEFAULT 0 CHECK(reorder_level >= 0),
  barcode TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL);
CREATE INDEX idx_products_barcode ON products(barcode);

-- One row per sale. client_ref is UNIQUE so the same sale can never be saved twice.
CREATE TABLE sales(
  id TEXT PRIMARY KEY,
  receipt_no TEXT NOT NULL UNIQUE,
  client_ref TEXT NOT NULL UNIQUE,                         -- stops the same sale being recorded twice
  created_at TEXT NOT NULL,
  cashier_id TEXT NOT NULL REFERENCES users(id),
  cashier_name TEXT NOT NULL,
  method TEXT NOT NULL CHECK(method IN ('cash','mpesa')),
  payment_ref TEXT NOT NULL DEFAULT '',
  total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
  tendered_cents INTEGER NOT NULL CHECK(tendered_cents >= 0),
  change_cents INTEGER NOT NULL CHECK(change_cents >= 0));
CREATE INDEX idx_sales_created ON sales(created_at);

-- (Each line below keeps its own copy of the product name and prices, so history survives edits and deletions.)
-- product_id is deliberately not a foreign key: deleting a product must never erase sales history.
CREATE TABLE sale_items(
  id TEXT PRIMARY KEY,
  sale_id TEXT NOT NULL REFERENCES sales(id),
  product_id TEXT NOT NULL,
  product_name TEXT NOT NULL,
  category TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK(qty > 0),
  unit_price_cents INTEGER NOT NULL,
  unit_cost_cents INTEGER NOT NULL,
  line_total_cents INTEGER NOT NULL);
CREATE INDEX idx_items_sale ON sale_items(sale_id);

-- Every change to stock, with who and why. Never edited, only added to.
CREATE TABLE stock_movements(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_name TEXT NOT NULL,
  type TEXT NOT NULL,
  qty_change INTEGER NOT NULL,
  stock_after INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  user_name TEXT NOT NULL);
CREATE INDEX idx_movements_created ON stock_movements(created_at);

-- Who did what and when: sign-ins, sales, edits, exports, fraud decisions.
CREATE TABLE audit_log(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  user_name TEXT NOT NULL,
  action TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '');

-- Each M-Pesa attempt and its outcome. A receipt number can only belong to one payment.
CREATE TABLE mpesa_payments(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  client_ref TEXT NOT NULL,
  cashier_id TEXT NOT NULL REFERENCES users(id),
  phone TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  items_json TEXT NOT NULL,
  checkout_request_id TEXT NOT NULL UNIQUE,
  merchant_request_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('pending','paid','paid_review','failed','cancelled','timeout')),
  result_desc TEXT NOT NULL DEFAULT '',
  receipt TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX ux_mpesa_receipt ON mpesa_payments(receipt) WHERE receipt <> '';  -- one M-Pesa receipt, one payment
"""

# Upgrades applied after the first version. Add new ones as 3, 4, ... and never edit an old one.
# Upgrades to the design, applied in number order to any database that has not had them yet.
# 2 = the fraud findings table. TO ADD MORE: add 3: """ ... """, then (if you need it) a test.
MIGRATIONS = {
    2: """
-- One row per fraud finding, with a plain-language reason and the manager's decision.
CREATE TABLE fraud_events(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  rule TEXT NOT NULL,
  subject TEXT NOT NULL,                 -- the person (or payment) the finding is about
  score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
  title TEXT NOT NULL,
  reason TEXT NOT NULL,                  -- plain-language explanation shown to the manager
  evidence_json TEXT NOT NULL DEFAULT '{}',
  event_time TEXT NOT NULL,              -- when the suspicious activity happened
  dedupe_key TEXT NOT NULL UNIQUE,       -- the same finding is never raised twice
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN
    ('new','investigating','legitimate','policy_violation','suspected_fraud','inconclusive')),
  reviewed_by TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_fraud_status ON fraud_events(status, created_at);
CREATE INDEX idx_fraud_subject ON fraud_events(subject);
""",
}
SCHEMA_VERSION = max(MIGRATIONS)


# Opens a connection. foreign_keys=ON makes the database enforce the links between tables. WAL mode lets reading and writing
# happen at the same time; it creates side files (smartpos.db-wal and -shm). Do not copy smartpos.db by hand while the server
# runs: use backup.py instead.
def connect(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
    return c


# Creates the tables for a new database and applies any upgrades an older database is missing. Runs at every start-up.
def init(path=None):
    """Open the database. Creates the tables the first time and applies any newer upgrades."""
    c = connect(path)
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v == 0:
        c.executescript(SCHEMA)
        c.execute("PRAGMA user_version=1")
        v = 1
    for n in sorted(MIGRATIONS):  # upgrades run once each, in order, on existing databases too
        if n > v:
            c.executescript(MIGRATIONS[n])
            c.execute(f"PRAGMA user_version={n}")
    return c
