# SmartPOS code guide

How the code fits together and how to change it safely. Every script also has plain-English comments above each function.

## 1. The map

| File or folder | What it is |
|---|---|
| `server.py` | The program you run. Web server, sign-in, sales, stock, M-Pesa, dashboard, and the router `api()`. |
| `index.html` | The whole screen: cashier and admin. Styles first, then the script. |
| `db.py` | The database design (tables) and its upgrades (`MIGRATIONS`). |
| `mpesa.py` | Talks to Safaricom Daraja (payment prompt, status check). |
| `exporter.py` | Builds the Excel/CSV files on the Export tab. |
| `fraud/engine.py` | The fraud rules, scoring and risk levels. |
| `accounts.py` | The rules for staff accounts: password strength, usernames, lockout. Change a number here to change the rule. |
| `reset_admin.py` | Emergency tool for a forgotten admin password (run on the shop computer). |
| `connectivity.py` | Answers "can we reach Safaricom right now?" for the Online / No internet label. |
| `hardware/escpos.py` | Lays out the receipt for a thermal printer. |
| `hardware/printer.py` | Sends it to the printer (network, Linux, Windows). |
| `backup.py`, `migrate_csv.py` | Command-line tools: safe backup and restore, and the one-time CSV import. |
| `tests/` | Automated checks. Run them before every commit. |
| `.env` | Your private settings and secrets (never share or commit it). `.env.example` is the blank template. |
| `smartpos.db` | Your data. Back it up with `backup.py`, not by copying the file. |

## 2. What happens when a cashier presses Charge

1. `index.html`, function `charge()`, sends the cart to the server: `api('sales','POST',{...})`.
2. `server.py`, `H.handle_any()`, checks the sign-in token, then calls `api()`.
3. `api()` sees `POST /api/sales`, checks the user is a cashier, and calls `sell()`.
4. `sell()` opens a transaction and `_sell()` checks stock, takes prices from the database, works out the total, and saves the sale, its lines, the new stock and the stock movements.
5. The saved receipt goes back to the page, which shows the receipt and (if a printer is set up) prints it.

If anything fails in step 4, nothing is saved. That is the point of the transaction.

## 3. Rules that keep it safe (please keep following them)

1. **Money is whole cents inside the program.** Use `cents()` and `money()`. Never use decimal numbers for money.
2. **Never trust the browser.** The server takes prices and stock from the database and checks the user's role on every request.
3. **Never edit an old database upgrade.** Add a new numbered entry to `MIGRATIONS` in `db.py`.
4. **Sensitive action? Call `audit(...)`.** Stock changes also go through `move(...)`.
5. **Wrap data from the server in `esc()`** before putting it in HTML.
6. **Add a test** for every new feature, and run all tests before committing.

## 4. Recipes

### Add a field to products (example: supplier)
1. `db.py`: add `3: "ALTER TABLE products ADD COLUMN supplier TEXT NOT NULL DEFAULT '';"` to `MIGRATIONS`.
2. `server.py`: add `supplier` to `clean()` and `prod()`, and to the product INSERT and UPDATE lines in `api()`.
3. `index.html`: add `['supplier','Supplier']` to the list `F` in `prodForm()`, and a column in `stockView()` if you want it shown.
4. `exporter.py`: add it to the products export in `DATASETS`.
5. Add a test in `tests/test_smartpos.py` (class `StockManagement`).

### Add an admin tab
1. `index.html`: add `['id','Label']` to `NAV`, and an `if(tab=='id'){...}` block inside `view()`.
2. `server.py`: add the data route in `api()` **below** the `if not admin` line so only admins can use it.
3. Add a test that a cashier is refused and an admin is allowed.

### Add or tune a fraud rule
- **Tune:** change the number in `CFG` at the top of `fraud/engine.py`. Too many alerts: raise it. Missing real problems: lower it.
- **Add:** write `rule_something(c, since, cfg)` like the others (read the database, `yield finding(...)`), give it a score in `SCORES`, add it to `RULES`, and add a test in `tests/test_fraud.py`.

### Add an export
Add an entry to `DATASETS` in `exporter.py` (headings, SQL, date column), then a line with `b('name','Title','Description')` in the export tab of `index.html`.

### Change the receipt
Edit `receipt()` in `hardware/escpos.py`. It reads top to bottom, in print order. The shop name, address lines and footer come from `.env` (`SMARTPOS_BUSINESS_NAME` and so on).

### Change colours
Edit the CSS variables at the top of `index.html`: `body.pos{...}` is the cashier screen, `body.adm{...}` is the admin screen, and `PAL` in the script is the product category palette.

### Add a role (for example "manager")
1. `accounts.py`: add the role to `ROLES`.
2. `db.py`: the users table only allows 'admin' and 'cashier', so add a numbered migration that rebuilds that table with the new role allowed.
3. `server.py`: decide in `api()` which routes the role may use (routes above `if not admin` are open to every signed-in person).
4. `index.html`: add the role to the dropdowns in `staffView()` and to `route()` if it needs its own screen.
5. Add tests in `tests/test_accounts.py`.

### Change the password or lockout rules
Edit the numbers at the top of `accounts.py` (`MIN_LENGTH`, `MAX_FAILURES`, `LOCK_MINUTES`) and run the tests.

### Understand accounts and sessions
- `setup()` creates the first admin once (the set-up screen). `login()` checks the password, applies the lockout and starts a session.
- A session is a token. `session_user()` reads the person's record fresh on every request, so switching someone off or changing a password takes effect at once.
- Someone with `must_change_password` can use nothing except `/api/password` and `/api/me` until they choose their own password.
- Staff are managed by `users_api()` (admin only). People are never deleted, and there is always at least one active admin.

### Understand the outage handling
- `connectivity.online()` is asked by the cashier screen every 15 seconds (`netCheck()` in `index.html`) and by `mpesa_start()` before sending a prompt.
- `recover_payments()` in `server.py` runs every 2 minutes (`recovery_loop`). It asks Safaricom about payments that never got a result and finishes them.
- `settle()` has the **double-sale guard**: if the cashier has made any other sale since the prompt was sent, the payment is held for a manager instead of being recorded.
- Tests for all of this are in `tests/test_offline.py`.

### Understand the manual M-Pesa checkout
- `mpesa_manual()` in `server.py` is the Checkout button's endpoint. It records the sale AND a row in the `mpesa_manual` table (migration 4) in one transaction.
- `manual_admin()` lists those rows for the M-Pesa tab and lets an admin mark them Verified or Disputed.
- `rule_manual_mpesa()` in `fraud/engine.py` flags the risky patterns. Tune `manual_verify_hours` and `manual_per_day` in `CFG`.
- In `index.html`: `effOff()` says whether we are working offline, `toggleOffline()` is the Online/Offline button, `manualCheckout()` is the Checkout button.
- Tests are in `tests/test_manual_mpesa.py`.

## 5. When something goes wrong

| Where | What to do |
|---|---|
| Blank page | Press F12, open Console, and read the red error. Run the tests: one checks for leftover merge-conflict lines. |
| An action fails | Read the terminal that is running `server.py`. The real error is printed there. |
| Data looks wrong | Open `smartpos.db` in the free "DB Browser for SQLite". `audit_log` and `stock_movements` show what happened. |
| Not sure a change is safe | `python3 -m unittest discover -s tests -v` |

## 6. Words used in the code

- **Transaction:** a group of database changes that all succeed or all are undone.
- **Token:** the random code the page gets at sign-in and sends with every request.
- **Migration:** a numbered upgrade to the database design, applied once per database.
- **Callback:** Safaricom's message to your server saying a payment succeeded or failed.
- **Audit log:** the permanent record of who did what and when.
- **ESC/POS:** the command language thermal receipt printers understand.
- **Route:** one web address the server answers, for example `GET /api/products`.
