"""Sends bytes to the receipt printer. Standard library only.

Choose the connection with SMARTPOS_PRINTER (in the .env file):
  network:192.168.0.50            Ethernet or Wi-Fi printer (port 9100). Add :PORT if yours differs.
  cups:PrinterName                Linux or Mac printer set up in the system (sent raw)
  file:/dev/usb/lp0               Linux USB printer used directly
  windows:Printer Name            Windows printer, exactly as named in Printers & scanners
  none                            no printer: receipts print through the browser
Other settings: SMARTPOS_PRINTER_WIDTH (48 for 80 mm paper, 32 for 58 mm), SMARTPOS_BUSINESS_NAME, SMARTPOS_BUSINESS_LINES
(address and phone separated by |), SMARTPOS_RECEIPT_FOOTER, SMARTPOS_DRAWER (1 or 0), SMARTPOS_DRAWER_PIN (0 or 1),
SMARTPOS_AUTOPRINT (1 prints the receipt as soon as a sale completes)."""
import os
import socket
import subprocess
import sys


class PrinterError(Exception):
    pass


def settings():
    e = os.environ.get
    try:
        width = int(e("SMARTPOS_PRINTER_WIDTH", "48") or 48)
    except ValueError:
        width = 48
    return dict(spec=e("SMARTPOS_PRINTER", "").strip(), width=width, name=e("SMARTPOS_BUSINESS_NAME", "SmartPOS"),
                lines=[x.strip() for x in e("SMARTPOS_BUSINESS_LINES", "").split("|") if x.strip()],
                footer=e("SMARTPOS_RECEIPT_FOOTER", "Thank you for shopping with us!"),
                drawer=e("SMARTPOS_DRAWER", "1") != "0", pin=1 if e("SMARTPOS_DRAWER_PIN", "0") == "1" else 0,
                autoprint=e("SMARTPOS_AUTOPRINT", "0") == "1")


def configured():
    spec = settings()["spec"]
    return bool(spec) and spec.lower() != "none"


def _send_windows(name, data):
    if sys.platform != "win32":
        raise PrinterError("Windows printers can only be used on Windows.")
    import ctypes
    from ctypes import wintypes

    class DocInfo(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    h = wintypes.HANDLE()
    if not spool.OpenPrinterW(name, ctypes.byref(h), None):
        raise PrinterError(f"Windows cannot find a printer named '{name}'. Check the exact name in Printers & scanners.")
    try:
        if not spool.StartDocPrinterW(h, 1, ctypes.byref(DocInfo("SmartPOS receipt", None, "RAW"))):
            raise PrinterError("Windows would not start the print job.")
        spool.StartPagePrinter(h)
        written = wintypes.DWORD()
        ok = spool.WritePrinter(h, data, len(data), ctypes.byref(written))
        spool.EndPagePrinter(h)
        spool.EndDocPrinter(h)
        if not ok:
            raise PrinterError("Windows could not send the receipt to the printer.")
    finally:
        spool.ClosePrinter(h)


def send(data, spec=None):
    spec = settings()["spec"] if spec is None else spec
    if not spec or spec.lower() == "none":
        raise PrinterError("No receipt printer is set up. Set SMARTPOS_PRINTER in the .env file (see the README).")
    kind, _, target = spec.partition(":")
    kind = kind.lower()
    try:
        if kind == "network":
            host, _, port = target.partition(":")
            with socket.create_connection((host, int(port or 9100)), timeout=4) as s:
                s.sendall(data)
        elif kind == "file":
            with open(target, "wb") as f:
                f.write(data)
        elif kind == "cups":
            r = subprocess.run(["lp", "-d", target, "-o", "raw"], input=data, capture_output=True, timeout=15)
            if r.returncode:
                raise PrinterError("The system print service refused the job: " + r.stderr.decode(errors="replace").strip()[:120])
        elif kind == "windows":
            _send_windows(target, data)
        else:
            raise PrinterError(f"Unknown printer type '{kind}'. Use network, cups, file or windows.")
    except PrinterError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise PrinterError(f"Could not reach the printer ({spec}). Is it switched on and connected? ({e})")
