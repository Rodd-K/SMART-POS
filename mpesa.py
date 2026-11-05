"""Daraja helpers for M-Pesa Express (STK Push) to a Buy Goods till. Standard library only."""
import os, json, base64, time, urllib.request, urllib.error
from datetime import datetime


def _load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()
cfg = lambda k, d="": os.environ.get("MPESA_" + k, d)
REQUIRED = ["CONSUMER_KEY", "CONSUMER_SECRET", "PASSKEY", "SHORTCODE", "TILL", "CALLBACK_URL", "CALLBACK_SECRET"]


def mode():
    m = cfg("MODE", "simulated").lower()
    return m if m in ("simulated", "sandbox", "live") else "simulated"


def missing():
    return [k for k in REQUIRED if not cfg(k)]


def base():
    return "https://api.safaricom.co.ke" if mode() == "live" else "https://sandbox.safaricom.co.ke"


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


_tok = {"v": None, "exp": 0}


def token():
    if _tok["v"] and time.time() < _tok["exp"]:
        return _tok["v"]
    auth = base64.b64encode(f'{cfg("CONSUMER_KEY")}:{cfg("CONSUMER_SECRET")}'.encode()).decode()
    r = call("/oauth/v1/generate?grant_type=client_credentials", headers={"Authorization": "Basic " + auth})
    if "access_token" not in r:
        raise RuntimeError("Could not get a Daraja token. Check the consumer key and secret in .env")
    _tok.update(v=r["access_token"], exp=time.time() + int(r.get("expires_in", 3600)) - 60)
    return _tok["v"]


def norm(phone):
    """0712345678, +254712345678 or 712345678 -> 254712345678"""
    d = "".join(c for c in str(phone or "") if c.isdigit())
    if d.startswith("0") and len(d) == 10:
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "17":
        d = "254" + d
    return d if d.startswith("254") and len(d) == 12 else None


def _signed():
    ts, sc = datetime.now().strftime("%Y%m%d%H%M%S"), cfg("SHORTCODE")
    return {"BusinessShortCode": sc, "Password": base64.b64encode((sc + cfg("PASSKEY") + ts).encode()).decode(), "Timestamp": ts}


def _post(path, body):
    return call(path, body, {"Authorization": "Bearer " + token(), "Content-Type": "application/json"})


def stk_push(phone, amount, ref, desc="SmartPOS"):
    b = _signed()
    b.update(TransactionType=cfg("TRANSACTION_TYPE", "CustomerBuyGoodsOnline"), Amount=int(amount), PartyA=phone,
             PartyB=cfg("TILL"), PhoneNumber=phone, AccountReference=ref[:12], TransactionDesc=desc[:13],
             CallBackURL=cfg("CALLBACK_URL").rstrip("/") + "/api/mpesa/callback/" + cfg("CALLBACK_SECRET"))
    return _post("/mpesa/stkpush/v1/processrequest", b)


def stk_query(checkout_id):
    b = _signed()
    b["CheckoutRequestID"] = checkout_id
    return _post("/mpesa/stkpushquery/v1/query", b)
