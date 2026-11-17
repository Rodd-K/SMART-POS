# SmartPOS Intelligence

A point-of-sale system for Kenyan businesses: checkout, stock, M-Pesa till payments, reports, and (coming) fraud detection.
Python standard library only. No installs needed.

New to the code? Read **CODE_GUIDE.md** first: a map of the files, how a sale flows through them, and step-by-step recipes for common changes.

## Run it
```
python3 server.py            # then open http://localhost:8000
```
**First start of a new install:** the browser shows a **set-up screen**. Enter the business name and create the admin account with a password you choose.
There are no default accounts and no sample data. The admin then adds the cashiers in the **Staff** tab (each person gets their own sign-in and
chooses their own password at first sign-in). A lost admin password is reset with `python3 reset_admin.py` on the shop computer.

**Developing?** Set `SMARTPOS_DEMO=1` in `.env` before the first start to get demo accounts (admin/admin123, cashier/cashier123) and sample products.
If you still have old CSV data in `data/`, run `python3 migrate_csv.py` once first.

## Settings (M-Pesa)
Copy `.env.example` to `.env` and fill it in. `MPESA_MODE` is `simulated` (default, no Daraja needed), `sandbox` or `live`.
Daraja must reach your callback URL: for testing, run `cloudflared tunnel --url http://localhost:8000`.
**Never commit or share `.env`.**

## Test it (run before every commit)
```
python3 -m unittest discover -s tests -v
```
All tests must pass. They use a temporary database and a fake Daraja, so your data and keys are untouched.

## Fraud alerts
Admins see a **Fraud** tab: alerts with plain-language reasons, risk by person, and buttons to record a decision with a note.
The server scans automatically every 15 minutes (`SMARTPOS_SCAN_MINUTES` changes this) and there is a **Scan now** button.
Cashiers never see fraud alerts. The **Export** tab has a Fraud report and a Fraud summary by person for any dates (This week, This month, or custom).

## Fraud scan from the command line
```
python3 -m fraud            # run from this folder: scans the last 14 days and prints findings
python3 -m fraud --days 30
```
It reads the same `smartpos.db` and never changes sales, stock or payments.

## Printer, cash drawer and scanner
Set `SMARTPOS_PRINTER` in `.env` (see `.env.example`), restart, then use **Hardware > Print test page** in the admin screen.
`network:192.168.0.50` for an Ethernet or Wi-Fi printer, `cups:Name` or `file:/dev/usb/lp0` on Linux, `windows:Printer Name` on Windows.
Receipts print straight to the printer (no print window) and open the cash drawer on the first copy of a cash sale. Reprints say DUPLICATE COPY and never open the drawer.
Cashiers can print their own sale for 10 minutes; older reprints are for an admin. USB barcode scanners that act like a keyboard need no setup.
If no printer is set up, receipts print through the browser as before.

## When the internet drops
Cash sales, stock, printing and scanning never need the internet, so the shop keeps trading. The cashier screen shows **Online** or **No internet**;
with no internet the M-Pesa button is switched off and the cashier is told to take cash.
If a customer paid by M-Pesa just as the connection dropped, the server finishes that payment by itself once the internet returns (every 2 minutes),
and records the sale. If the cashier had already taken cash for the same goods, the payment is **held for a manager** instead (M-Pesa tab and Fraud tab),
so nothing is sold twice and nothing is lost.

## Offline M-Pesa: the Checkout button
The cashier screen has an **Online / Offline** button. It shows Offline by itself when the system cannot reach Safaricom, and the cashier can also press it
to work offline by hand (for example if a customer's phone cannot receive a prompt).
- **Online:** the cashier presses **Send M-Pesa prompt**. Safaricom confirms the payment and the system records the sale.
- **Offline:** the prompt button is off. The customer shows their M-Pesa message; the cashier checks it, types the 10-character **code** and the **amount**, and presses
  **Checkout**. The sale is recorded as *unverified*.
- **Afterwards** the admin opens **M-Pesa**, compares each code with the M-Pesa statement and marks it **Verified** or **Disputed**. The same code can never be used twice.
- The fraud engine flags: a manual payment taken while the system was online, one left unchecked for 24 hours, a disputed one, and a cashier using it 3 or more times in a day.
- Switch it off with `SMARTPOS_MANUAL_MPESA=off`; change the per-payment limit with `SMARTPOS_MANUAL_MPESA_MAX`. The Export tab has a "Manual M-Pesa confirmations" file for checking.

## Back up and export
```
python3 backup.py                       # safe backup into backups/
python3 backup.py restore backups/FILE  # stop the server first
```
Admins can also download Excel-ready CSVs from the **Export** tab.

## Files
| File | Purpose |
|---|---|
| `server.py` | Web server, API, sales, stock, M-Pesa |
| `db.py` | Database tables |
| `mpesa.py` | Daraja calls |
| `exporter.py` | Excel/CSV export |
| `index.html` | The whole interface |
| `migrate_csv.py` | One-time CSV to database import |
| `accounts.py` | Password, username and lockout rules for staff accounts |
| `reset_admin.py` | Emergency tool: set a new password for a forgotten admin |
| `connectivity.py` | Checks whether Safaricom is reachable (the Online / No internet label) |
| `hardware/` | Receipt printer (ESC/POS) and cash drawer |
| `fraud/` | Fraud detection engine (reads POS data, writes findings only) |
| `tests/` | Automated tests |

## Working as a team
Share code through Git, never data or secrets. Pull before you start, run the tests before you commit, and fix any
merge conflict (lines starting with seven `<` characters) before running anything. The tests catch leftover markers.
