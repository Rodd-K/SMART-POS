"""Daraja helpers for M-Pesa Express (STK Push) to a Buy Goods till. Standard library only."""
# =====================================================================================================
# Talks to Safaricom's Daraja service. The life of one payment:
#   token()      get permission to use Daraja (valid about an hour)
#   stk_push()   ask Daraja to show a payment prompt on the customer's phone
#   ...          the customer enters their PIN; Safaricom then calls OUR callback address with the result
#   stk_query()  the backup: ask Daraja what happened if the callback never arrives
# This file only SENDS requests and tidies the data. server.py decides what to do with the results.
# Settings and secrets come from the .env file (MPESA_MODE, MPESA_CONSUMER_KEY ...).
# =====================================================================================================
import os, json, base64, time, urllib.request, urllib.error
from datetime import datetime


# Reads the .env file into the program's settings. A real environment variable wins over the file (setdefault).
def _load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()
# cfg("TILL") reads the setting MPESA_TILL.
cfg = lambda k, d="": os.environ.get("MPESA_" + k, d)
# Settings that must be filled in before sandbox or live mode can work.
REQUIRED = ["CONSUMER_KEY", "CONSUMER_SECRET", "PASSKEY", "SHORTCODE", "TILL", "CALLBACK_URL", "CALLBACK_SECRET"]


# simulated = fake payments (no Safaricom)   sandbox = Safaricom's test system   live = REAL MONEY.
def mode():
    m = cfg("MODE", "simulated").lower()
    return m if m in ("simulated", "sandbox", "live") else "simulated"


# Lists any required settings that are still empty, so the screen can say exactly what is missing.
def missing():
    return [k for k in REQUIRED if not cfg(k)]


# Safaricom's web address: the test one (sandbox) or the real one (live).
def base():
    return "https://api.safaricom.co.ke" if mode() == "live" else "https://sandbox.safaricom.co.ke"


# Sends one request to Safaricom and returns the answer as a Python dictionary. Error replies are returned too, so their details can be shown.
def call(path, body=None, headers=None):
    req = urllib.request.Request(base() + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # Daraja explains its errors in the response body
        try:
            return json.loads(e.read())
        except Exception:
            return {"errorMessage": f"Daraja returned HTTP {e.code}"}


# The access token lasts about an hour, so it is kept and reused until shortly before it expires.
_tok = {"v": None, "exp": 0}


# Gets (or reuses) the access token. It fails with a clear message if the consumer key or secret is wrong.
def token():
    if _tok["v"] and time.time() < _tok["exp"]:
        return _tok["v"]
    auth = base64.b64encode(f'{cfg("CONSUMER_KEY")}:{cfg("CONSUMER_SECRET")}'.encode()).decode()
    r = call("/oauth/v1/generate?grant_type=client_credentials", headers={"Authorization": "Basic " + auth})
    if "access_token" not in r:
        raise RuntimeError("Could not get a Daraja token. Check the consumer key and secret in .env")
    _tok.update(v=r["access_token"], exp=time.time() + int(r.get("expires_in", 3600)) - 60)
    return _tok["v"]


# Cleans a Kenyan phone number to 2547XXXXXXXX (accepts 07..., +254... or 7...). Returns None if it does not look valid.
def norm(phone):
    """0712345678, +254712345678 or 712345678 -> 254712345678"""
    d = "".join(c for c in str(phone or "") if c.isdigit())
    if d.startswith("0") and len(d) == 10:
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "17":
        d = "254" + d
    return d if d.startswith("254") and len(d) == 12 else None


# Daraja needs a timestamp and a "password" made from shortcode + passkey + timestamp, in base64.
def _signed():
    ts, sc = datetime.now().strftime("%Y%m%d%H%M%S"), cfg("SHORTCODE")
    return {"BusinessShortCode": sc, "Password": base64.b64encode((sc + cfg("PASSKEY") + ts).encode()).decode(), "Timestamp": ts}


def _post(path, body):
    return call(path, body, {"Authorization": "Bearer " + token(), "Content-Type": "application/json"})


# Asks Safaricom to show a payment prompt on the customer's phone.
# TransactionType: CustomerBuyGoodsOnline is for tills; the sandbox test shortcode may need CustomerPayBillOnline.
# The callback address includes our secret, so only Safaricom (who is told it) can report a payment.
def stk_push(phone, amount, ref, desc="SmartPOS"):
    b = _signed()
    b.update(TransactionType=cfg("TRANSACTION_TYPE", "CustomerBuyGoodsOnline"), Amount=int(amount), PartyA=phone,
             PartyB=cfg("TILL"), PhoneNumber=phone, AccountReference=ref[:12], TransactionDesc=desc[:13],
             CallBackURL=cfg("CALLBACK_URL").rstrip("/") + "/api/mpesa/callback/" + cfg("CALLBACK_SECRET"))
    return _post("/mpesa/stkpush/v1/processrequest", b)


# Asks Safaricom what happened to a payment. Used as the backup when the callback is late or lost.
def stk_query(checkout_id):
    b = _signed()
    b["CheckoutRequestID"] = checkout_id
    return _post("/mpesa/stkpushquery/v1/query", b)
