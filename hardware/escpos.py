"""ESC/POS receipt builder for 58 mm and 80 mm thermal printers. Standard library only.
Only plain ASCII is printed, so every printer shows it correctly whatever its code page."""
import textwrap
import unicodedata
from datetime import datetime


def ascii_only(s):
    """Accents are dropped (e-acute becomes e); anything else a printer might misread becomes '?'."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(ch))
    return plain.encode("ascii", "replace").decode("ascii")


class Doc:
    def __init__(self, width=48):
        self.width = max(24, int(width))  # characters per line: 48 for 80 mm paper, 32 for 58 mm
        self.buf = bytearray(b"\x1b@")    # initialise the printer
        self.text = []                    # a plain-text copy of what prints (used by tests and previews)

    def _line(self, s, shown=None):
        s = ascii_only(s)
        self.text.append(shown if shown is not None else s)
        self.buf += s.encode("ascii") + b"\n"

    def style(self, align="left", bold=False, big=False):
        self.buf += b"\x1ba" + bytes([{"left": 0, "center": 1, "right": 2}[align]])
        self.buf += b"\x1bE" + bytes([1 if bold else 0])
        self.buf += b"\x1d!" + bytes([0x11 if big else 0])  # 0x11 = double width and height

    def center(self, s, bold=False, big=False):
        w = self.width // (2 if big else 1)
        self.style("center", bold, big)
        for ln in textwrap.wrap(ascii_only(s), w) or [""]:
            self._line(ln, shown=ln.center(self.width))
        self.style()

    def left(self, s, bold=False):
        self.style("left", bold)
        for ln in textwrap.wrap(ascii_only(s), self.width) or [""]:
            self._line(ln)
        self.style()

    def pair(self, left, right, bold=False):
        """'Soda x2 ........ 140.00' with the amount on the right edge. Long names wrap onto the next line."""
        left, right = ascii_only(left), ascii_only(right)
        self.style("left", bold)
        parts = textwrap.wrap(left, max(8, self.width - len(right) - 1)) or [""]
        self._line(parts[0].ljust(self.width - len(right)) + right)
        for ln in parts[1:]:
            self._line("  " + ln)
        self.style()

    def rule(self, ch="-"):
        self._line(ch * self.width)

    def feed(self, n=1):
        self.buf += b"\x1bd" + bytes([n])

    def cut(self):
        self.feed(3)
        self.buf += b"\x1dVB\x00"  # feed and partial cut

    def kick(self, pin=0):
        self.buf += b"\x1bp" + bytes([pin, 25, 250])  # open the cash drawer

    def bytes(self):
        return bytes(self.buf)


def receipt(sale, cfg, copy=0, kick=False):
    """sale: the dict the server returns for a sale. cfg: printer.settings(). copy: how many times it was printed before."""
    d = Doc(cfg["width"])
    if kick:
        d.kick(cfg["pin"])
    d.center(cfg["name"], bold=True, big=True)
    for ln in cfg["lines"]:
        d.center(ln)
    d.rule()
    d.left("Receipt: " + sale["receipt_no"])
    d.left("Date: " + sale["created_at"].replace("T", " ")[:16])
    d.left("Served by: " + sale["cashier_name"])
    d.rule()
    for i in sale["items"]:
        d.pair(f"{i['qty']} x {i['product_name']}", i["line_total"])
        if i["qty"] > 1:
            d.left(f"   @ {i['unit_price']} each")
    d.rule()
    d.pair("TOTAL (KES)", sale["total"], bold=True)
    if sale["method"] == "cash":
        d.pair("Cash received", sale["tendered"])
        d.pair("Change", sale["change"])
    else:
        d.pair("Paid by M-Pesa", sale["total"])
        d.left("M-Pesa ref: " + sale["payment_ref"])
    d.rule()
    if copy:
        d.center(f"*** DUPLICATE COPY {copy} ***", bold=True)
    d.center(cfg["footer"])
    d.cut()
    return d


def test_page(cfg):
    d = Doc(cfg["width"])
    d.center(cfg["name"], bold=True, big=True)
    d.center("PRINTER TEST", bold=True)
    d.rule()
    d.left(("1234567890" * 6)[:cfg["width"]])
    d.left("ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz")
    d.pair("Left aligned", "Right 123.45")
    d.rule()
    d.left("Printed " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    d.center("If you can read this, the printer works.")
    d.cut()
    return d
