# SmartPOS Intelligence

A point-of-sale system for Kenyan businesses: checkout, stock, M-Pesa till payments, reports, and (coming) fraud detection.
Python standard library only. No installs needed.

## Run it
```
python3 server.py            # then open http://localhost:8000
```
First start creates `smartpos.db` with two demo logins: `admin` / `admin123` and `cashier` / `cashier123`. **Change these before real use.**
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
| `hardware/` | Receipt printer (ESC/POS) and cash drawer |
| `fraud/` | Fraud detection engine (reads POS data, writes findings only) |
| `tests/` | Automated tests |

## Working as a team
Share code through Git, never data or secrets. Pull before you start, run the tests before you commit, and fix any
merge conflict (lines starting with seven `<` characters) before running anything. The tests catch leftover markers.
