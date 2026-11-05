"""SmartPOS database layer: SQLite schema and connection. Standard library only.
Money is stored as whole cents (INTEGER) so totals never suffer rounding errors."""
import os, sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SMARTPOS_DB", os.path.join(ROOT, "smartpos.db"))
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users(
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','cashier')),
  salt TEXT NOT NULL,
  hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);

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

CREATE TABLE audit_log(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  user_name TEXT NOT NULL,
  action TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '');

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


def connect(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
    return c


def init(path=None):
    """Open the database, creating the tables the first time."""
    c = connect(path)
    if c.execute("PRAGMA user_version").fetchone()[0] == 0:
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return c
