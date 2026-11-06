"""Centralised, error-friendly access to the backend API.

* One shared HTTP session (connection reuse = faster)
* Short-lived caching of read calls (pages feel instant), cleared after any change
* Every failure becomes an ApiError with a message that is safe to show to users
"""
import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API_URL = os.getenv("API_URL", "http://localhost:5000/api").rstrip("/")
TIMEOUT = (3.5, 25)
CACHE_TTL = 20


class ApiError(Exception):
    def __init__(self, message, status=None, code=None, details=None, request_id=None):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code
        self.details, self.request_id = details, request_id


class SessionExpired(ApiError):
    pass


class ServerUnreachable(ApiError):
    pass


@st.cache_resource
def _http():
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _clear_session(notice):
    st.session_state.update(logged_in=False, token=None, user=None, login_notice=notice, _no_resume=True, _clear_cookie=True)


def _request(method, path, *, params=None, json=None, auth=True):
    headers = {}
    token = st.session_state.get("token")
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = _http().request(method, f"{API_URL}{path}", params=params, json=json, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise ServerUnreachable("Can't reach the billing server. Please check that the backend is running, then try again.", code="unreachable")
    except requests.exceptions.Timeout:
        raise ApiError("The server took too long to respond. Please try again.", code="timeout")
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"Network problem: {exc}", code="network")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.ok and data.get("success", True):
        return data
    err = data.get("error") or {}
    msg = err.get("message") or f"The server returned an unexpected response (HTTP {resp.status_code})."
    args = dict(status=resp.status_code, code=err.get("code"), details=err.get("details"), request_id=err.get("request_id"))
    if auth and resp.status_code == 401:
        _clear_session("Your session has ended. Please sign in again.")
        raise SessionExpired(msg, **args)
    raise ApiError(msg, **args)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _cached_get(path, token, params):
    return _request("GET", path, params=dict(params))


def _get(path, params=None, cache=True):
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if not cache:
        return _request("GET", path, params=clean)
    return _cached_get(path, st.session_state.get("token") or "", tuple(sorted(clean.items())))


def clear_cache():
    _cached_get.clear()


def _write(method, path, payload=None):
    data = _request(method, path, json=payload)
    clear_cache()
    return data


# ------------------------------------------------------------------ auth
def health():
    try:
        return _request("GET", "/health", auth=False).get("database", False)
    except ServerUnreachable:
        return None
    except ApiError:
        return False


def login(username, password):
    return _request("POST", "/login", json={"username": username, "password": password}, auth=False)


def resume_session(token):
    """Try to restore a sign-in from a saved token (after a browser refresh). Returns the user or None."""
    st.session_state["token"] = token
    try:
        return _request("GET", "/me")["user"]
    except ApiError:
        st.session_state["token"] = None
        st.session_state.pop("login_notice", None)
        return None


def logout():
    try:
        _request("POST", "/logout")
    except ApiError:
        pass
    clear_cache()


# ------------------------------------------------------------------ billing
def get_inventory(include_inactive=False):
    return _get("/inventory", {"include_inactive": "1" if include_inactive else None})["data"]


def get_customers():
    return _get("/customers")["data"]


def create_customer(name, phone=None, email=None):
    return _write("POST", "/customers", {"cname": name, "phone_no": phone or None, "email": email or None})["customer"]


def create_order(payload):
    return _write("POST", "/orders", payload)


def get_bill(oid):
    return _get("/orders/%d" % int(oid))["bill"]


def recent_bills():
    return _get("/orders/recent")["data"]


# ------------------------------------------------------------------ owner
def get_orders(start, end, payment_method=None, q=None):
    return _get("/orders", {"date_from": str(start), "date_to": str(end), "payment_method": payment_method, "q": q})["data"]


def get_analytics(start, end):
    return _get("/analytics", {"date_from": str(start), "date_to": str(end)})


def get_catalog():
    return _get("/catalog")


def create_category(name):
    return _write("POST", "/categories", {"cname": name})


def create_variety(cid, name):
    return _write("POST", "/varieties", {"cid": cid, "vname": name})


def create_product(payload):
    return _write("POST", "/products", payload)


def update_product(pid, payload):
    return _write("PUT", f"/products/{int(pid)}", payload)


def restock(pid, qty, note=None):
    return _write("POST", "/stock/restock", {"pid": pid, "qty": qty, "note": note})


def adjust_stock(pid, delta, note):
    return _write("POST", "/stock/adjust", {"pid": pid, "delta": delta, "note": note})


def get_stock_log(limit=300):
    return _get("/stock/log", {"limit": limit}, cache=False)["data"]


def get_audit_log(limit=300, q=None, action=None):
    return _get("/audit-log", {"limit": limit, "q": q, "action": action}, cache=False)["data"]


def get_users():
    return _get("/users", cache=False)["data"]


def create_user(username, full_name, password, role):
    return _write("POST", "/users", {"username": username, "full_name": full_name, "password": password, "role": role})


def update_user(uid, payload):
    return _write("PATCH", f"/users/{int(uid)}", payload)


# ------------------------------------------------------------------ void a bill (owner)
def void_order(oid, reason):
    return _write("POST", f"/orders/{int(oid)}/void", {"reason": reason})["bill"]


# ------------------------------------------------------------------ discount offers
def get_available_offers(subtotal, cust_id=None):
    """Offers a cashier can use on this bill (each says whether it applies and why not)."""
    return _get("/offers/available", {"subtotal": f"{float(subtotal):.2f}", "cust_id": cust_id})["data"]


def get_offers():
    return _get("/offers", cache=False)["data"]


def create_offer(payload):
    return _write("POST", "/offers", payload)


def update_offer(offer_id, payload):
    return _write("PUT", f"/offers/{int(offer_id)}", payload)


def delete_offer(offer_id):
    return _write("DELETE", f"/offers/{int(offer_id)}")
