"""Grain Inventory System - Flask JSON API.

* Token based login (signed, expiring tokens) with two roles: owner / cashier
* Every endpoint validates its input and returns errors in one consistent shape:
  {"success": false, "error": {"code", "message", "details", "request_id"}}
* Activity is written to the Audit_log table AND to logs/app.log
"""
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

import mysql.connector
from flask import Flask, g, jsonify, request
from flask.json.provider import DefaultJSONProvider
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import db
from db import DatabaseUnavailable, fetch_all, fetch_one

# --------------------------------------------------------------------------- setup
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"


def _setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("grain.api")


def _load_secret():
    env = os.getenv("SECRET_KEY")
    if env:
        return env
    path = BASE_DIR / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_hex(32)
    path.write_text(secret)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


serializer = URLSafeTimedSerializer(_load_secret(), salt="grain-auth")
TOKEN_MAX_AGE = int(os.getenv("TOKEN_HOURS", "12")) * 3600
PAYMENT_METHODS = ("Cash", "UPI", "Card")
CENT = Decimal("0.01")


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, timedelta):
        return str(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class AppJSONProvider(DefaultJSONProvider):
    default = staticmethod(_json_default)
    sort_keys = False


app = Flask(__name__)
app.json = AppJSONProvider(app)
app.config["JSON_AS_ASCII"] = False


# --------------------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def _error_response(message, status, code, details=None):
    body = {"success": False, "error": {"code": code, "message": message, "request_id": g.get("req_id")}}
    if details is not None:
        body["error"]["details"] = details
    return jsonify(body), status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return _error_response(e.message, e.status, e.code, e.details)


@app.errorhandler(DatabaseUnavailable)
def handle_db_unavailable(e):
    return _error_response("The database is not reachable right now. Please try again in a moment.", 503, "database_unavailable")


@app.errorhandler(mysql.connector.Error)
def handle_mysql_error(e):
    errno = getattr(e, "errno", None)
    if isinstance(e, mysql.connector.IntegrityError):
        if errno == 1062:
            return _error_response("That record already exists.", 409, "duplicate")
        if errno == 1452:
            return _error_response("A related record could not be found.", 400, "invalid_reference")
        if errno == 1451:
            return _error_response("This record is in use and cannot be removed.", 409, "in_use")
        if errno == 3819:
            return _error_response("A value is out of the allowed range.", 400, "validation_error")
    if errno == 1213:
        return _error_response("The system was busy with another transaction. Please try again.", 409, "deadlock")
    if isinstance(e, (mysql.connector.InterfaceError, mysql.connector.PoolError)) or errno in (2002, 2003, 2006, 2013, 2055):
        log.error("[%s] Database connection problem: %s", g.get("req_id"), e)
        return _error_response("The database connection was lost. Please try again.", 503, "database_unavailable")
    log.exception("[%s] Database error", g.get("req_id"))
    return _error_response("A database error occurred. Please contact the owner if it continues.", 500, "database_error")


@app.errorhandler(HTTPException)
def handle_http_error(e):
    messages = {
        400: "The request was not understood.",
        404: "That endpoint does not exist.",
        405: "That action is not allowed here.",
        413: "The request is too large.",
    }
    return _error_response(messages.get(e.code, e.description or "Request failed."), e.code or 500, "http_error")


@app.errorhandler(Exception)
def handle_unexpected(e):
    log.exception("[%s] Unhandled error", g.get("req_id"))
    return _error_response("Something went wrong on the server. Please try again or contact the owner.", 500, "server_error")


# --------------------------------------------------------------------------- request hooks
@app.before_request
def _before():
    g.req_id = uuid.uuid4().hex[:8]
    g.started = time.perf_counter()
    g.user = None


@app.after_request
def _after(resp):
    ms = (time.perf_counter() - g.get("started", time.perf_counter())) * 1000
    user = g.get("user")
    log.info(
        "[%s] %s %s -> %s (%.0f ms) user=%s",
        g.get("req_id"), request.method, request.path, resp.status_code, ms, user["username"] if user else "-",
    )
    resp.headers["X-Request-ID"] = g.get("req_id", "")
    return resp


# --------------------------------------------------------------------------- helpers
def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("The request body must be valid JSON.", 400, "invalid_json")
    return data


def money(v):
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def _missing(label):
    return ApiError(f"{label} is required.", 400, "validation_error")


def get_str(data, key, label, *, required=True, max_len=100, default=None):
    v = data.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise _missing(label)
        return default
    v = str(v).strip()
    if len(v) > max_len:
        raise ApiError(f"{label} must be at most {max_len} characters.", 400, "validation_error")
    return v


def get_int(data, key, label, *, required=True, minimum=None, maximum=None, default=None):
    v = data.get(key)
    if v is None or v == "":
        if required:
            raise _missing(label)
        return default
    try:
        if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
            raise ValueError
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be a whole number.", 400, "validation_error")
    if minimum is not None and n < minimum:
        raise ApiError(f"{label} must be at least {minimum}.", 400, "validation_error")
    if maximum is not None and n > maximum:
        raise ApiError(f"{label} must be at most {maximum}.", 400, "validation_error")
    return n


def get_money(data, key, label, *, required=True, minimum=Decimal("0"), maximum=Decimal("10000000"), default=None):
    v = data.get(key)
    if v is None or v == "":
        if required:
            raise _missing(label)
        return default
    try:
        d = Decimal(str(v))
        if not d.is_finite() or isinstance(v, bool):
            raise InvalidOperation
    except InvalidOperation:
        raise ApiError(f"{label} must be a valid amount.", 400, "validation_error")
    d = money(d)
    if minimum is not None and d < minimum:
        raise ApiError(f"{label} cannot be less than {minimum}.", 400, "validation_error")
    if maximum is not None and d > maximum:
        raise ApiError(f"{label} is too large.", 400, "validation_error")
    return d


def audit(action, details="", *, cur=None, user=None, username=None):
    """Write an audit record. Never lets a logging failure break the request."""
    user = user or g.get("user")
    uid = user["user_id"] if user else None
    uname = ((user["username"] if user else username) or "")[:50]
    ip = request.remote_addr if request else None
    log.info("AUDIT %s | user=%s | %s", action, uname or "-", details)
    sql = "INSERT INTO Audit_log (user_id, username, action, details, ip_address) VALUES (%s, %s, %s, %s, %s)"
    args = (uid, uname, action, str(details)[:2000], ip)
    try:
        if cur is not None:
            cur.execute(sql, args)
        else:
            db.execute(sql, args)
    except Exception:
        log.exception("Failed to write audit log entry")


def parse_range(default_days=30):
    today = date.today()

    def parse(name, default):
        raw = request.args.get(name)
        if not raw:
            return default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise ApiError(f"'{name}' must be a date like 2026-11-01.", 400, "validation_error")

    d_from = parse("date_from", today - timedelta(days=default_days - 1))
    d_to = parse("date_to", today)
    if d_from > d_to:
        raise ApiError("The start date must be on or before the end date.", 400, "validation_error")
    if (d_to - d_from).days > 730:
        raise ApiError("Please choose a date range of 2 years or less.", 400, "validation_error")
    return datetime.combine(d_from, dtime.min), datetime.combine(d_to + timedelta(days=1), dtime.min)


# --------------------------------------------------------------------------- auth
def require_auth(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or len(header) < 10:
                raise ApiError("Please log in to continue.", 401, "unauthenticated")
            try:
                payload = serializer.loads(header[7:].strip(), max_age=TOKEN_MAX_AGE)
            except SignatureExpired:
                raise ApiError("Your session has expired. Please log in again.", 401, "session_expired")
            except BadSignature:
                raise ApiError("Your session is not valid. Please log in again.", 401, "invalid_token")
            user = fetch_one(
                "SELECT user_id, username, full_name, role, is_active FROM System_Users WHERE user_id = %s",
                (payload.get("uid"),),
            )
            if not user or not user["is_active"]:
                raise ApiError("Your account is no longer active. Please contact the owner.", 401, "account_disabled")
            g.user = user
            if roles and user["role"] not in roles:
                log.warning("Forbidden: %s (%s) tried %s %s", user["username"], user["role"], request.method, request.path)
                raise ApiError("You don't have permission to do this.", 403, "forbidden")
            return fn(*args, **kwargs)

        return wrapper

    return deco


_failed_logins = {}
_failed_lock = threading.Lock()
MAX_ATTEMPTS, LOCK_WINDOW = 5, 300


def _check_lock(key):
    now = time.time()
    with _failed_lock:
        attempts = [t for t in _failed_logins.get(key, []) if now - t < LOCK_WINDOW]
        _failed_logins[key] = attempts
        if len(attempts) >= MAX_ATTEMPTS:
            wait = int((LOCK_WINDOW - (now - attempts[0])) / 60) + 1
            raise ApiError(f"Too many failed attempts. Please try again in about {wait} minute(s).", 429, "rate_limited")


def _register_failure(key):
    with _failed_lock:
        _failed_logins.setdefault(key, []).append(time.time())


def _public_user(u):
    return {"user_id": u["user_id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"]}


@app.get("/api/health")
def health():
    try:
        fetch_one("SELECT 1 AS ok")
        return jsonify(success=True, status="ok", database=True, time=datetime.now())
    except Exception:
        return jsonify(success=False, status="degraded", database=False, time=datetime.now()), 503


@app.post("/api/login")
def login():
    d = body()
    username = (d.get("username") or "").strip() if isinstance(d.get("username"), str) else ""
    password = d.get("password") if isinstance(d.get("password"), str) else ""
    if not username or not password:
        raise ApiError("Please enter both username and password.", 400, "missing_credentials")
    key = f"{username.lower()}|{request.remote_addr}"
    _check_lock(key)
    user = fetch_one(
        "SELECT user_id, username, full_name, password_hash, role, is_active FROM System_Users WHERE username = %s",
        (username,),
    )
    if not user or not check_password_hash(user["password_hash"], password):
        _register_failure(key)
        audit("LOGIN_FAILED", f"username={username}", username=username)
        raise ApiError("Incorrect username or password.", 401, "invalid_credentials")
    if not user["is_active"]:
        audit("LOGIN_BLOCKED", "disabled account", user=user)
        raise ApiError("This account has been disabled. Please contact the owner.", 403, "account_disabled")
    with _failed_lock:
        _failed_logins.pop(key, None)
    db.execute("UPDATE System_Users SET last_login = NOW() WHERE user_id = %s", (user["user_id"],))
    g.user = user
    audit("LOGIN", f"role={user['role']}")
    return jsonify(success=True, token=serializer.dumps({"uid": user["user_id"]}), user=_public_user(user))


@app.post("/api/logout")
@require_auth()
def logout():
    audit("LOGOUT")
    return jsonify(success=True)


@app.get("/api/me")
@require_auth()
def me():
    return jsonify(success=True, user=_public_user(g.user))


# --------------------------------------------------------------------------- catalogue & stock
@app.get("/api/inventory")
@require_auth()
def inventory():
    is_owner = g.user["role"] == "owner"
    include_inactive = is_owner and request.args.get("include_inactive") == "1"
    rows = fetch_all(
        """SELECT p.pid, p.pname, c.cid, c.cname AS category, v.vid, v.vname AS variety,
                  p.price, p.cost_price, p.reorder_level, p.is_active, COALESCE(s.qty, 0) AS qty
           FROM Product p
           JOIN Variety v ON v.vid = p.vid
           JOIN Category c ON c.cid = v.cid
           LEFT JOIN Stock s ON s.pid = p.pid
           WHERE (%s = 1 OR p.is_active = 1)
           ORDER BY c.cname, v.vname, p.pname""",
        (1 if include_inactive else 0,),
    )
    if not is_owner:
        for r in rows:
            r.pop("cost_price", None)
    return jsonify(success=True, data=rows)


@app.get("/api/catalog")
@require_auth("owner")
def catalog():
    cats = fetch_all("SELECT cid, cname FROM Category ORDER BY cname")
    vars_ = fetch_all(
        "SELECT v.vid, v.vname, c.cid, c.cname FROM Variety v JOIN Category c ON c.cid = v.cid ORDER BY c.cname, v.vname"
    )
    return jsonify(success=True, categories=cats, varieties=vars_)


@app.post("/api/categories")
@require_auth("owner")
def category_create():
    name = get_str(body(), "cname", "Category name")
    if fetch_one("SELECT cid FROM Category WHERE cname = %s", (name,)):
        raise ApiError(f"Category '{name}' already exists.", 409, "duplicate")
    cid, _ = db.execute("INSERT INTO Category (cname) VALUES (%s)", (name,))
    audit("CATEGORY_CREATED", f"{name} (#{cid})")
    return jsonify(success=True, cid=cid), 201


@app.post("/api/varieties")
@require_auth("owner")
def variety_create():
    d = body()
    name = get_str(d, "vname", "Variety name", max_len=150)
    cid = get_int(d, "cid", "Category", minimum=1)
    if not fetch_one("SELECT cid FROM Category WHERE cid = %s", (cid,)):
        raise ApiError("That category does not exist.", 404, "not_found")
    if fetch_one("SELECT vid FROM Variety WHERE cid = %s AND vname = %s", (cid, name)):
        raise ApiError(f"Variety '{name}' already exists in this category.", 409, "duplicate")
    vid, _ = db.execute("INSERT INTO Variety (vname, cid) VALUES (%s, %s)", (name, cid))
    audit("VARIETY_CREATED", f"{name} (#{vid}) in category #{cid}")
    return jsonify(success=True, vid=vid), 201


def _apply_stock_change(cur, pid, delta, kind, note):
    cur.execute(
        "SELECT p.pname, s.qty FROM Product p JOIN Stock s ON s.pid = p.pid WHERE p.pid = %s FOR UPDATE", (pid,)
    )
    row = cur.fetchone()
    if not row:
        raise ApiError("Product not found.", 404, "not_found")
    new_qty = row["qty"] + delta
    if new_qty < 0:
        raise ApiError(f"Stock can't go below zero ({row['pname']} has only {row['qty']}).", 409, "negative_stock")
    cur.execute("UPDATE Stock SET qty = %s WHERE pid = %s", (new_qty, pid))
    cur.execute(
        """INSERT INTO Stock_log (pid, change_qty, qty_after, transaction_type, note, user_id)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (pid, delta, new_qty, kind, note, g.user["user_id"]),
    )
    return row["pname"], row["qty"], new_qty


@app.post("/api/products")
@require_auth("owner")
def product_create():
    d = body()
    name = get_str(d, "pname", "Product name", max_len=150)
    vid = get_int(d, "vid", "Variety", minimum=1)
    price = get_money(d, "price", "Selling price")
    cost = get_money(d, "cost_price", "Cost price", required=False, default=Decimal("0"))
    reorder = get_int(d, "reorder_level", "Reorder level", required=False, minimum=0, maximum=1000000, default=20)
    opening = get_int(d, "opening_qty", "Opening stock", required=False, minimum=0, maximum=10000000, default=0)
    if not fetch_one("SELECT vid FROM Variety WHERE vid = %s", (vid,)):
        raise ApiError("That variety does not exist.", 404, "not_found")
    if fetch_one("SELECT pid FROM Product WHERE pname = %s", (name,)):
        raise ApiError(f"A product named '{name}' already exists.", 409, "duplicate")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO Product (pname, vid, price, cost_price, reorder_level) VALUES (%s, %s, %s, %s, %s)",
            (name, vid, price, cost, reorder),
        )
        pid = cur.lastrowid
        cur.execute("INSERT INTO Stock (pid, qty) VALUES (%s, %s)", (pid, opening))
        if opening:
            cur.execute(
                """INSERT INTO Stock_log (pid, change_qty, qty_after, transaction_type, note, user_id)
                   VALUES (%s, %s, %s, 'OPENING STOCK', 'New product', %s)""",
                (pid, opening, opening, g.user["user_id"]),
            )
        audit("PRODUCT_CREATED", f"{name} (#{pid}) price={price} cost={cost} opening={opening}", cur=cur)
    return jsonify(success=True, pid=pid), 201


@app.put("/api/products/<int:pid>")
@require_auth("owner")
def product_update(pid):
    d = body()
    old = fetch_one("SELECT pname, vid, price, cost_price, reorder_level, is_active FROM Product WHERE pid = %s", (pid,))
    if not old:
        raise ApiError("Product not found.", 404, "not_found")
    sets, params, changes = [], [], []

    def change(col, new, label):
        if new != old[col]:
            sets.append(f"{col} = %s")
            params.append(new)
            changes.append(f"{label}: {old[col]} -> {new}")

    if "pname" in d:
        name = get_str(d, "pname", "Product name", max_len=150)
        if name != old["pname"] and fetch_one("SELECT pid FROM Product WHERE pname = %s AND pid <> %s", (name, pid)):
            raise ApiError(f"A product named '{name}' already exists.", 409, "duplicate")
        change("pname", name, "name")
    if "vid" in d:
        vid = get_int(d, "vid", "Variety", minimum=1)
        if not fetch_one("SELECT vid FROM Variety WHERE vid = %s", (vid,)):
            raise ApiError("That variety does not exist.", 404, "not_found")
        change("vid", vid, "variety")
    if "price" in d:
        change("price", get_money(d, "price", "Selling price"), "price")
    if "cost_price" in d:
        change("cost_price", get_money(d, "cost_price", "Cost price"), "cost")
    if "reorder_level" in d:
        change("reorder_level", get_int(d, "reorder_level", "Reorder level", minimum=0, maximum=1000000), "reorder level")
    if "is_active" in d:
        change("is_active", 1 if d["is_active"] else 0, "active")
    if not sets:
        return jsonify(success=True, message="Nothing changed.")
    db.execute(f"UPDATE Product SET {', '.join(sets)} WHERE pid = %s", (*params, pid))
    audit("PRODUCT_UPDATED", f"{old['pname']} (#{pid}) | " + "; ".join(changes))
    return jsonify(success=True, message="Product updated.")


@app.post("/api/stock/restock")
@require_auth("owner")
def stock_restock():
    d = body()
    pid = get_int(d, "pid", "Product", minimum=1)
    qty = get_int(d, "qty", "Quantity", minimum=1, maximum=10000000)
    note = get_str(d, "note", "Note", required=False, max_len=255, default=None)
    with db.transaction() as cur:
        name, before, after = _apply_stock_change(cur, pid, qty, "RESTOCK", note)
        audit("STOCK_RESTOCK", f"{name} (#{pid}) +{qty} ({before} -> {after})", cur=cur)
    return jsonify(success=True, message=f"Added {qty} to {name}. New stock: {after}.", qty=after)


@app.post("/api/stock/adjust")
@require_auth("owner")
def stock_adjust():
    d = body()
    pid = get_int(d, "pid", "Product", minimum=1)
    delta = get_int(d, "delta", "Change", minimum=-10000000, maximum=10000000)
    if delta == 0:
        raise ApiError("The change cannot be zero.", 400, "validation_error")
    note = get_str(d, "note", "Reason", max_len=255)
    with db.transaction() as cur:
        name, before, after = _apply_stock_change(cur, pid, delta, "ADJUSTMENT", note)
        audit("STOCK_ADJUST", f"{name} (#{pid}) {delta:+d} ({before} -> {after}) reason={note}", cur=cur)
    return jsonify(success=True, message=f"Stock for {name} is now {after}.", qty=after)


@app.get("/api/stock/log")
@require_auth("owner")
def stock_log():
    limit = get_int(request.args, "limit", "Limit", required=False, minimum=1, maximum=5000, default=300)
    pid = get_int(request.args, "pid", "Product", required=False, minimum=1)
    sql = """SELECT l.log_id, l.timestamp, p.pname, l.change_qty, l.qty_after, l.transaction_type, l.note,
                    COALESCE(u.full_name, '-') AS user
             FROM Stock_log l JOIN Product p ON p.pid = l.pid LEFT JOIN System_Users u ON u.user_id = l.user_id"""
    params = []
    if pid:
        sql += " WHERE l.pid = %s"
        params.append(pid)
    sql += " ORDER BY l.log_id DESC LIMIT %s"
    params.append(limit)
    return jsonify(success=True, data=fetch_all(sql, params))


@app.get("/api/audit-log")
@require_auth("owner")
def audit_log():
    limit = get_int(request.args, "limit", "Limit", required=False, minimum=1, maximum=5000, default=300)
    action = (request.args.get("action") or "").strip()
    q = (request.args.get("q") or "").strip()
    sql, params, where = "SELECT log_id, created_at, username, action, details, ip_address FROM Audit_log", [], []
    if action:
        where.append("action = %s")
        params.append(action)
    if q:
        where.append("(username LIKE %s OR details LIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_id DESC LIMIT %s"
    params.append(limit)
    return jsonify(success=True, data=fetch_all(sql, params))


# --------------------------------------------------------------------------- customers
@app.get("/api/customers")
@require_auth()
def customers_list():
    q = (request.args.get("q") or "").strip()
    sql = "SELECT cust_id, cname, phone_no, email FROM Customer"
    params = []
    if q:
        sql += " WHERE cname LIKE %s OR phone_no LIKE %s"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY cname LIMIT 1000"
    return jsonify(success=True, data=fetch_all(sql, params))


@app.post("/api/customers")
@require_auth()
def customers_create():
    d = body()
    name = get_str(d, "cname", "Customer name")
    phone = get_str(d, "phone_no", "Phone number", required=False, max_len=20)
    email = get_str(d, "email", "Email", required=False, max_len=150)
    if phone:
        digits = re.sub(r"[\s\-+()]", "", phone)
        if not digits.isdigit() or not 7 <= len(digits) <= 15:
            raise ApiError("Please enter a valid phone number (7-15 digits).", 400, "validation_error")
        dup = fetch_one("SELECT cust_id, cname FROM Customer WHERE phone_no = %s", (phone,))
        if dup:
            raise ApiError(
                f"A customer with this phone number already exists ({dup['cname']}). Choose 'Existing customer' instead.",
                409, "duplicate_phone", {"cust_id": dup["cust_id"]},
            )
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError("Please enter a valid email address.", 400, "validation_error")
    cust_id, _ = db.execute("INSERT INTO Customer (cname, phone_no, email) VALUES (%s, %s, %s)", (name, phone, email))
    audit("CUSTOMER_CREATED", f"{name} (#{cust_id})")
    return jsonify(success=True, customer={"cust_id": cust_id, "cname": name, "phone_no": phone, "email": email}), 201


# --------------------------------------------------------------------------- billing
def load_bill(oid):
    order = fetch_one(
        """SELECT o.oid, o.bill_no, o.odate, o.subtotal, o.discount, o.total_amt, o.payment_method,
                  o.amount_received, o.cashier_id, o.cust_id, o.offer_name,
                  o.status, o.voided_at, o.void_reason,
                  c.cname AS customer_name, c.phone_no AS customer_phone,
                  u.full_name AS cashier_name, vu.full_name AS voided_by_name
           FROM Orders o
           LEFT JOIN Customer c ON c.cust_id = o.cust_id
           LEFT JOIN System_Users u ON u.user_id = o.cashier_id
           LEFT JOIN System_Users vu ON vu.user_id = o.voided_by
           WHERE o.oid = %s""",
        (oid,),
    )
    if not order:
        return None
    order["items"] = fetch_all(
        """SELECT p.pname, oi.qty, oi.unit_price, oi.line_total
           FROM Order_item oi JOIN Product p ON p.pid = oi.pid WHERE oi.oid = %s ORDER BY oi.id""",
        (oid,),
    )
    received = order["amount_received"]
    order["change_due"] = (
        money(received - order["total_amt"]) if order["payment_method"] == "Cash" and received is not None else None
    )
    return order


@app.post("/api/orders")
@require_auth()
def order_create():
    d = body()
    items_in = d.get("items")
    if not isinstance(items_in, list) or not items_in:
        raise ApiError("Add at least one item to the bill.", 400, "empty_cart")
    if len(items_in) > 100:
        raise ApiError("A bill can have at most 100 lines.", 400, "validation_error")
    merged = {}
    for it in items_in:
        if not isinstance(it, dict):
            raise ApiError("Items are not in the expected format.", 400, "validation_error")
        pid = get_int(it, "pid", "Product", minimum=1)
        qty = get_int(it, "qty", "Quantity", minimum=1, maximum=100000)
        merged[pid] = merged.get(pid, 0) + qty

    cust_id = get_int(d, "cust_id", "Customer", required=False, minimum=1)
    offer_id = get_int(d, "offer_id", "Offer", required=False, minimum=1)
    custom_discount = get_money(d, "discount", "Discount", required=False, default=Decimal("0"))
    if offer_id and custom_discount > 0:
        raise ApiError("Use either an offer or a custom discount, not both.", 400, "validation_error")
    if custom_discount > 0 and g.user["role"] != "owner":
        raise ApiError("Only the owner can give a custom discount. Please choose one of the offers.", 403, "forbidden")
    payment = d.get("payment_method") or "Cash"
    if payment not in PAYMENT_METHODS:
        raise ApiError(f"Payment method must be one of: {', '.join(PAYMENT_METHODS)}.", 400, "validation_error")
    received = get_money(d, "amount_received", "Amount received", required=False, default=None)

    pids = sorted(merged)
    with db.transaction() as cur:
        placeholders = ",".join(["%s"] * len(pids))
        cur.execute(
            f"""SELECT p.pid, p.pname, p.price, p.cost_price, p.is_active, COALESCE(s.qty, 0) AS qty
                FROM Product p LEFT JOIN Stock s ON s.pid = p.pid
                WHERE p.pid IN ({placeholders}) ORDER BY p.pid FOR UPDATE""",
            pids,
        )
        rows = {r["pid"]: r for r in cur.fetchall()}
        problems = []
        for pid, qty in merged.items():
            r = rows.get(pid)
            if not r or not r["is_active"]:
                problems.append(f"Product #{pid} is not available for sale.")
            elif r["qty"] < qty:
                problems.append(f"{r['pname']}: only {r['qty']} in stock (you asked for {qty}).")
        if problems:
            raise ApiError("Some items cannot be billed. " + " ".join(problems), 409, "insufficient_stock", problems)

        if cust_id is not None:
            cur.execute("SELECT cust_id FROM Customer WHERE cust_id = %s", (cust_id,))
            if not cur.fetchone():
                raise ApiError("The selected customer no longer exists.", 404, "customer_not_found")

        subtotal = sum((rows[p]["price"] * q for p, q in merged.items()), Decimal("0"))
        discount, offer_name = custom_discount, None
        if offer_id:
            cur.execute("SELECT * FROM Offer WHERE offer_id = %s", (offer_id,))
            offer = cur.fetchone()
            if not offer:
                raise ApiError("That offer no longer exists. Please choose another.", 409, "offer_not_applicable")
            eligible, amount, why = evaluate_offer(offer, subtotal, cust_id, cur=cur)
            if not eligible:
                raise ApiError(f"The offer '{offer['name']}' can't be applied: {why}", 409, "offer_not_applicable")
            discount, offer_name = amount, offer["name"]
        if discount > subtotal:
            raise ApiError("The discount cannot be more than the bill subtotal.", 400, "validation_error")
        total = subtotal - discount
        if payment == "Cash":
            if received is not None and received < total:
                raise ApiError(f"Cash received (Rs. {received}) is less than the bill total (Rs. {total}).", 400, "validation_error")
        else:
            received = total

        cur.execute(
            """INSERT INTO Orders (cust_id, cashier_id, subtotal, discount, total_amt, payment_method, amount_received,
                                    offer_id, offer_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (cust_id, g.user["user_id"], subtotal, discount, total, payment, received,
             offer_id if offer_name else None, offer_name),
        )
        oid = cur.lastrowid
        bill_no = f"GB-{datetime.now():%Y%m%d}-{oid:05d}"
        cur.execute("UPDATE Orders SET bill_no = %s WHERE oid = %s", (bill_no, oid))
        for pid, qty in merged.items():
            r = rows[pid]
            cur.execute(
                """INSERT INTO Order_item (oid, pid, qty, unit_price, unit_cost, line_total)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (oid, pid, qty, r["price"], r["cost_price"], r["price"] * qty),
            )
            cur.execute("UPDATE Stock SET qty = qty - %s WHERE pid = %s", (qty, pid))
            cur.execute(
                """INSERT INTO Stock_log (pid, change_qty, qty_after, transaction_type, note, user_id)
                   VALUES (%s, %s, %s, 'SALE', %s, %s)""",
                (pid, -qty, r["qty"] - qty, f"Bill {bill_no}", g.user["user_id"]),
            )
        audit("BILL_CREATED", f"{bill_no} total={total} items={len(merged)} payment={payment}"
              + (f" offer='{offer_name}' discount={discount}" if offer_name else (f" custom_discount={discount}" if discount > 0 else "")), cur=cur)

    return jsonify(success=True, bill=load_bill(oid)), 201


@app.get("/api/orders/recent")
@require_auth()
def orders_recent():
    rows = fetch_all(
        """SELECT o.oid, o.bill_no, o.odate, COALESCE(c.cname, 'Walk-in') AS customer, o.total_amt, o.payment_method, o.status
           FROM Orders o LEFT JOIN Customer c ON c.cust_id = o.cust_id
           WHERE o.cashier_id = %s ORDER BY o.oid DESC LIMIT 15""",
        (g.user["user_id"],),
    )
    return jsonify(success=True, data=rows)


@app.get("/api/orders/<int:oid>")
@require_auth()
def order_get(oid):
    bill = load_bill(oid)
    if not bill:
        raise ApiError("Bill not found.", 404, "not_found")
    if g.user["role"] != "owner" and bill["cashier_id"] != g.user["user_id"]:
        raise ApiError("You can only view bills that you issued.", 403, "forbidden")
    return jsonify(success=True, bill=bill)


@app.get("/api/orders")
@require_auth("owner")
def orders_list():
    start, end = parse_range()
    sql = """SELECT o.oid, o.bill_no, o.odate, COALESCE(c.cname, 'Walk-in') AS customer,
                    COALESCE(u.full_name, '(removed)') AS cashier,
                    (SELECT COALESCE(SUM(qty), 0) FROM Order_item WHERE oid = o.oid) AS items,
                    o.subtotal, o.discount, o.total_amt, o.payment_method, o.status, o.offer_name
             FROM Orders o
             LEFT JOIN Customer c ON c.cust_id = o.cust_id
             LEFT JOIN System_Users u ON u.user_id = o.cashier_id
             WHERE o.odate >= %s AND o.odate < %s"""
    params = [start, end]
    pm = (request.args.get("payment_method") or "").strip()
    if pm:
        sql += " AND o.payment_method = %s"
        params.append(pm)
    cashier_id = get_int(request.args, "cashier_id", "Cashier", required=False, minimum=1)
    if cashier_id:
        sql += " AND o.cashier_id = %s"
        params.append(cashier_id)
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (o.bill_no LIKE %s OR c.cname LIKE %s OR c.phone_no LIKE %s)"
        params += [f"%{q}%"] * 3
    limit = get_int(request.args, "limit", "Limit", required=False, minimum=1, maximum=5000, default=2000)
    sql += " ORDER BY o.oid DESC LIMIT %s"
    params.append(limit)
    return jsonify(success=True, data=fetch_all(sql, params))


# --------------------------------------------------------------------------- offers (set by owner, used by cashiers)
def _one(cur, sql, params):
    if cur is not None:
        cur.execute(sql, params)
        return cur.fetchone()
    return fetch_one(sql, params)


def evaluate_offer(offer, subtotal, cust_id, cur=None):
    """Returns (eligible, discount_amount, reason_if_not_eligible). The server is the only judge of eligibility."""
    zero, today = Decimal("0.00"), date.today()
    subtotal = money(subtotal)
    if not offer["is_active"]:
        return False, zero, "This offer is switched off."
    if offer["valid_from"] and today < offer["valid_from"]:
        return False, zero, f"Starts on {offer['valid_from']:%d %b %Y}."
    if offer["valid_to"] and today > offer["valid_to"]:
        return False, zero, f"Ended on {offer['valid_to']:%d %b %Y}."
    min_bill = money(offer["min_bill"])
    if subtotal < min_bill:
        return False, zero, f"Add \u20b9{min_bill - subtotal:,.2f} more to the bill (minimum \u20b9{min_bill:,.2f})."
    min_spent = money(offer["min_total_spent"])
    if min_spent > 0:
        if not cust_id:
            return False, zero, "Select a saved customer to check this offer."
        row = _one(cur, "SELECT COALESCE(SUM(total_amt), 0) AS s FROM Orders WHERE cust_id = %s AND status = 'active'", (cust_id,))
        spent = money(row["s"])
        if spent < min_spent:
            return False, zero, f"For customers who have shopped \u20b9{min_spent:,.0f}+ before (this customer: \u20b9{spent:,.0f})."
    value = money(offer["value"])
    if offer["discount_type"] == "percent":
        amount = money(subtotal * value / 100)
        if offer["max_discount"] is not None:
            amount = min(amount, money(offer["max_discount"]))
    else:
        amount = value
    amount = min(amount, subtotal)
    if amount <= 0:
        return False, zero, "There is nothing to discount on this bill."
    return True, amount, ""


def offer_rule_text(o):
    def num(x):
        return f"{float(x):g}"

    text = f"{num(o['value'])}% off" if o["discount_type"] == "percent" else f"\u20b9{num(o['value'])} off"
    if o["discount_type"] == "percent" and o["max_discount"] is not None:
        text += f" (up to \u20b9{num(o['max_discount'])})"
    conds = []
    if float(o["min_bill"]) > 0:
        conds.append(f"on bills of \u20b9{num(o['min_bill'])} or more")
    if float(o["min_total_spent"]) > 0:
        conds.append(f"for customers who have shopped \u20b9{num(o['min_total_spent'])}+ before")
    text += " \u2014 " + (", ".join(conds) if conds else "on every bill")
    if o["valid_from"] and o["valid_to"]:
        text += f" \u00b7 {o['valid_from']:%d %b} to {o['valid_to']:%d %b %Y}"
    elif o["valid_to"]:
        text += f" \u00b7 until {o['valid_to']:%d %b %Y}"
    elif o["valid_from"]:
        text += f" \u00b7 from {o['valid_from']:%d %b %Y}"
    return text


def _parse_date(v, label):
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(f"{label} must be a date like 2026-11-15.", 400, "validation_error")


def _parse_offer(d, old=None):
    """Validate an offer from the owner. For edits, missing fields keep their old value."""
    old = old or {}

    def pick(key, default=None):
        return d[key] if key in d else old.get(key, default)

    name = get_str({"v": pick("name")}, "v", "Offer name", max_len=100)
    dtype = pick("discount_type")
    if dtype not in ("percent", "flat"):
        raise ApiError("Choose whether the offer is a percentage or a fixed amount.", 400, "validation_error")
    value = get_money({"v": pick("value")}, "v", "Discount value", minimum=Decimal("0.01"))
    if dtype == "percent" and value > 100:
        raise ApiError("A percentage discount can't be more than 100%.", 400, "validation_error")
    min_bill = get_money({"v": pick("min_bill", 0)}, "v", "Minimum bill", required=False, default=Decimal("0"))
    min_spent = get_money({"v": pick("min_total_spent", 0)}, "v", "Minimum past purchases", required=False, default=Decimal("0"))
    cap = get_money({"v": pick("max_discount")}, "v", "Maximum discount", required=False, default=None, minimum=Decimal("0"))
    if dtype != "percent" or (cap is not None and cap <= 0):
        cap = None
    v_from, v_to = _parse_date(pick("valid_from"), "Start date"), _parse_date(pick("valid_to"), "End date")
    if v_from and v_to and v_to < v_from:
        raise ApiError("The end date must be on or after the start date.", 400, "validation_error")
    return {"name": name, "discount_type": dtype, "value": value, "min_bill": min_bill, "min_total_spent": min_spent,
            "max_discount": cap, "valid_from": v_from, "valid_to": v_to, "is_active": 1 if pick("is_active", True) else 0}


@app.get("/api/offers/available")
@require_auth()
def offers_available():
    """Offers a cashier can use right now, checked against this bill (and customer)."""
    subtotal = get_money(request.args, "subtotal", "Subtotal")
    cust_id = get_int(request.args, "cust_id", "Customer", required=False, minimum=1)
    today = date.today()
    out = []
    for o in fetch_all("SELECT * FROM Offer WHERE is_active = 1 ORDER BY name"):
        if (o["valid_from"] and today < o["valid_from"]) or (o["valid_to"] and today > o["valid_to"]):
            continue
        eligible, amount, why = evaluate_offer(o, subtotal, cust_id)
        out.append({"offer_id": o["offer_id"], "name": o["name"], "rule_text": offer_rule_text(o),
                    "eligible": eligible, "discount": amount if eligible else 0, "why_not": why})
    out.sort(key=lambda r: (not r["eligible"], -float(r["discount"])))
    return jsonify(success=True, data=out)


@app.get("/api/offers")
@require_auth("owner")
def offers_list():
    rows = fetch_all(
        """SELECT f.*,
                  (SELECT COUNT(*) FROM Orders o WHERE o.offer_id = f.offer_id AND o.status = 'active') AS times_used,
                  (SELECT COALESCE(SUM(o.discount), 0) FROM Orders o WHERE o.offer_id = f.offer_id AND o.status = 'active') AS total_discount
           FROM Offer f ORDER BY f.is_active DESC, f.name""")
    for r in rows:
        r["rule_text"] = offer_rule_text(r)
    return jsonify(success=True, data=rows)


@app.post("/api/offers")
@require_auth("owner")
def offer_create():
    f = _parse_offer(body())
    if fetch_one("SELECT offer_id FROM Offer WHERE name = %s", (f["name"],)):
        raise ApiError(f"An offer named '{f['name']}' already exists.", 409, "duplicate")
    oid, _ = db.execute(
        """INSERT INTO Offer (name, discount_type, value, min_bill, min_total_spent, max_discount, valid_from, valid_to, is_active)
           VALUES (%(name)s, %(discount_type)s, %(value)s, %(min_bill)s, %(min_total_spent)s, %(max_discount)s,
                   %(valid_from)s, %(valid_to)s, %(is_active)s)""", f)
    audit("OFFER_CREATED", f"{f['name']} (#{oid}) {f['discount_type']} {f['value']} min_bill={f['min_bill']}")
    return jsonify(success=True, message=f"Offer '{f['name']}' created.", offer_id=oid), 201


@app.put("/api/offers/<int:offer_id>")
@require_auth("owner")
def offer_update(offer_id):
    old = fetch_one("SELECT * FROM Offer WHERE offer_id = %s", (offer_id,))
    if not old:
        raise ApiError("Offer not found.", 404, "not_found")
    f = _parse_offer(body(), old)
    if fetch_one("SELECT offer_id FROM Offer WHERE name = %s AND offer_id <> %s", (f["name"], offer_id)):
        raise ApiError(f"An offer named '{f['name']}' already exists.", 409, "duplicate")
    db.execute(
        """UPDATE Offer SET name=%(name)s, discount_type=%(discount_type)s, value=%(value)s, min_bill=%(min_bill)s,
                  min_total_spent=%(min_total_spent)s, max_discount=%(max_discount)s, valid_from=%(valid_from)s,
                  valid_to=%(valid_to)s, is_active=%(is_active)s WHERE offer_id=%(id)s""", {**f, "id": offer_id})
    audit("OFFER_UPDATED", f"{f['name']} (#{offer_id}) active={f['is_active']} {f['discount_type']} {f['value']} min_bill={f['min_bill']}")
    return jsonify(success=True, message=f"Offer '{f['name']}' saved.")


@app.delete("/api/offers/<int:offer_id>")
@require_auth("owner")
def offer_delete(offer_id):
    old = fetch_one("SELECT name FROM Offer WHERE offer_id = %s", (offer_id,))
    if not old:
        raise ApiError("Offer not found.", 404, "not_found")
    db.execute("DELETE FROM Offer WHERE offer_id = %s", (offer_id,))  # old bills keep the offer name they used
    audit("OFFER_DELETED", f"{old['name']} (#{offer_id})")
    return jsonify(success=True, message=f"Offer '{old['name']}' deleted.")


# --------------------------------------------------------------------------- void a bill (owner only)
@app.post("/api/orders/<int:oid>/void")
@require_auth("owner")
def order_void(oid):
    reason = get_str(body(), "reason", "Reason", max_len=255)
    with db.transaction() as cur:
        cur.execute("SELECT oid, bill_no, status, total_amt FROM Orders WHERE oid = %s FOR UPDATE", (oid,))
        order = cur.fetchone()
        if not order:
            raise ApiError("Bill not found.", 404, "not_found")
        if order["status"] == "voided":
            raise ApiError("This bill has already been voided.", 409, "already_voided")
        cur.execute("SELECT pid, SUM(qty) AS qty FROM Order_item WHERE oid = %s GROUP BY pid ORDER BY pid", (oid,))
        for it in cur.fetchall():
            cur.execute("SELECT qty FROM Stock WHERE pid = %s FOR UPDATE", (it["pid"],))
            row = cur.fetchone()
            qty = int(it["qty"])
            if row:
                after = row["qty"] + qty
                cur.execute("UPDATE Stock SET qty = %s WHERE pid = %s", (after, it["pid"]))
            else:
                after = qty
                cur.execute("INSERT INTO Stock (pid, qty) VALUES (%s, %s)", (it["pid"], after))
            cur.execute(
                """INSERT INTO Stock_log (pid, change_qty, qty_after, transaction_type, note, user_id)
                   VALUES (%s, %s, %s, 'VOID', %s, %s)""",
                (it["pid"], qty, after, f"Bill {order['bill_no']} voided", g.user["user_id"]),
            )
        cur.execute(
            "UPDATE Orders SET status = 'voided', voided_at = NOW(), voided_by = %s, void_reason = %s WHERE oid = %s",
            (g.user["user_id"], reason, oid),
        )
        audit("BILL_VOIDED", f"{order['bill_no']} total={order['total_amt']} reason={reason}", cur=cur)
    return jsonify(success=True, message=f"Bill {order['bill_no']} voided. Items are back in stock.", bill=load_bill(oid))


# --------------------------------------------------------------------------- analytics
def _summary(a, b):
    row = fetch_one(
        """SELECT COUNT(*) AS orders, COALESCE(SUM(total_amt), 0) AS revenue,
                  COALESCE(SUM(discount), 0) AS discount, COALESCE(AVG(total_amt), 0) AS avg_bill
           FROM Orders WHERE status = 'active' AND odate >= %s AND odate < %s""",
        (a, b),
    )
    items = fetch_one(
        """SELECT COALESCE(SUM(oi.qty), 0) AS items, COALESCE(SUM(oi.unit_cost * oi.qty), 0) AS cost
           FROM Order_item oi JOIN Orders o ON o.oid = oi.oid WHERE o.status = 'active' AND o.odate >= %s AND o.odate < %s""",
        (a, b),
    )
    row["items"] = items["items"]
    row["cost"] = items["cost"]
    row["profit"] = money(Decimal(str(row["revenue"])) - Decimal(str(items["cost"])))
    return row


@app.get("/api/analytics")
@require_auth("owner")
def analytics():
    start, end = parse_range(7)
    span = end - start
    cur_sum = _summary(start, end)
    prev_sum = _summary(start - span, start)
    rng = (start, end)
    daily = fetch_all(
        """SELECT DATE(odate) AS day, COUNT(*) AS orders, SUM(total_amt) AS revenue
           FROM Orders WHERE status = 'active' AND odate >= %s AND odate < %s GROUP BY DATE(odate) ORDER BY day""", rng)
    top_products = fetch_all(
        """SELECT p.pid, p.pname, SUM(oi.qty) AS qty, SUM(oi.line_total) AS revenue,
                  SUM(oi.line_total - oi.unit_cost * oi.qty) AS gross_profit
           FROM Order_item oi JOIN Orders o ON o.oid = oi.oid JOIN Product p ON p.pid = oi.pid
           WHERE o.status = 'active' AND o.odate >= %s AND o.odate < %s GROUP BY p.pid, p.pname ORDER BY revenue DESC LIMIT 10""", rng)
    categories = fetch_all(
        """SELECT c.cname AS category, SUM(oi.qty) AS qty, SUM(oi.line_total) AS revenue
           FROM Order_item oi JOIN Orders o ON o.oid = oi.oid JOIN Product p ON p.pid = oi.pid
           JOIN Variety v ON v.vid = p.vid JOIN Category c ON c.cid = v.cid
           WHERE o.status = 'active' AND o.odate >= %s AND o.odate < %s GROUP BY c.cname ORDER BY revenue DESC""", rng)
    payments = fetch_all(
        """SELECT payment_method, COUNT(*) AS orders, SUM(total_amt) AS revenue
           FROM Orders WHERE status = 'active' AND odate >= %s AND odate < %s GROUP BY payment_method ORDER BY revenue DESC""", rng)
    hourly = fetch_all(
        """SELECT HOUR(odate) AS hour, COUNT(*) AS orders, SUM(total_amt) AS revenue
           FROM Orders WHERE status = 'active' AND odate >= %s AND odate < %s GROUP BY HOUR(odate) ORDER BY hour""", rng)
    cashiers = fetch_all(
        """SELECT COALESCE(u.full_name, '(removed)') AS cashier, COUNT(*) AS orders,
                  SUM(o.total_amt) AS revenue, AVG(o.total_amt) AS avg_bill
           FROM Orders o LEFT JOIN System_Users u ON u.user_id = o.cashier_id
           WHERE o.status = 'active' AND o.odate >= %s AND o.odate < %s GROUP BY u.user_id, u.full_name ORDER BY revenue DESC""", rng)
    customers = fetch_all(
        """SELECT c.cname AS customer, COUNT(*) AS orders, SUM(o.total_amt) AS revenue
           FROM Orders o JOIN Customer c ON c.cust_id = o.cust_id
           WHERE o.status = 'active' AND o.odate >= %s AND o.odate < %s GROUP BY c.cust_id, c.cname ORDER BY revenue DESC LIMIT 10""", rng)
    return jsonify(
        success=True, summary=cur_sum, previous=prev_sum, daily=daily, top_products=top_products,
        categories=categories, payments=payments, hourly=hourly, cashiers=cashiers, top_customers=customers,
    )


# --------------------------------------------------------------------------- users
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")


def _check_password(pw):
    if not isinstance(pw, str) or len(pw) < 6:
        raise ApiError("The password must be at least 6 characters long.", 400, "validation_error")
    if len(pw) > 128:
        raise ApiError("The password is too long.", 400, "validation_error")


@app.get("/api/users")
@require_auth("owner")
def users_list():
    rows = fetch_all(
        "SELECT user_id, username, full_name, role, is_active, last_login, created_at FROM System_Users ORDER BY role DESC, username"
    )
    return jsonify(success=True, data=rows)


@app.post("/api/users")
@require_auth("owner")
def users_create():
    d = body()
    username = get_str(d, "username", "Username", max_len=30)
    if not USERNAME_RE.match(username):
        raise ApiError("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.", 400, "validation_error")
    full_name = get_str(d, "full_name", "Full name")
    role = d.get("role")
    if role not in ("owner", "cashier"):
        raise ApiError("Role must be 'owner' or 'cashier'.", 400, "validation_error")
    _check_password(d.get("password"))
    if fetch_one("SELECT user_id FROM System_Users WHERE username = %s", (username,)):
        raise ApiError("That username is already taken. Please choose another.", 409, "duplicate")
    uid, _ = db.execute(
        "INSERT INTO System_Users (username, full_name, password_hash, role) VALUES (%s, %s, %s, %s)",
        (username, full_name, generate_password_hash(d["password"]), role),
    )
    audit("USER_CREATED", f"{username} (#{uid}) role={role}")
    return jsonify(success=True, message=f"User '{username}' created.", user_id=uid), 201


@app.patch("/api/users/<int:uid>")
@require_auth("owner")
def users_update(uid):
    d = body()
    target = fetch_one("SELECT user_id, username, full_name, role, is_active FROM System_Users WHERE user_id = %s", (uid,))
    if not target:
        raise ApiError("User not found.", 404, "not_found")
    is_self = uid == g.user["user_id"]
    sets, params, changes = [], [], []
    new_role, new_active = target["role"], bool(target["is_active"])

    if "full_name" in d:
        sets.append("full_name = %s")
        params.append(get_str(d, "full_name", "Full name"))
        changes.append("name changed")
    if "role" in d and d["role"] != target["role"]:
        if d["role"] not in ("owner", "cashier"):
            raise ApiError("Role must be 'owner' or 'cashier'.", 400, "validation_error")
        if is_self:
            raise ApiError("You can't change your own role.", 400, "validation_error")
        new_role = d["role"]
        sets.append("role = %s")
        params.append(new_role)
        changes.append(f"role -> {new_role}")
    if "is_active" in d and bool(d["is_active"]) != new_active:
        if is_self:
            raise ApiError("You can't disable your own account.", 400, "validation_error")
        new_active = bool(d["is_active"])
        sets.append("is_active = %s")
        params.append(1 if new_active else 0)
        changes.append("enabled" if new_active else "disabled")
    if d.get("password"):
        _check_password(d["password"])
        sets.append("password_hash = %s")
        params.append(generate_password_hash(d["password"]))
        changes.append("password reset")

    if target["role"] == "owner" and target["is_active"] and (new_role != "owner" or not new_active):
        others = fetch_one("SELECT COUNT(*) AS n FROM System_Users WHERE role = 'owner' AND is_active = 1 AND user_id <> %s", (uid,))
        if others["n"] == 0:
            raise ApiError("There must always be at least one active owner.", 409, "last_owner")
    if not sets:
        return jsonify(success=True, message="Nothing changed.")
    db.execute(f"UPDATE System_Users SET {', '.join(sets)} WHERE user_id = %s", (*params, uid))
    audit("USER_UPDATED", f"{target['username']} (#{uid}): " + ", ".join(changes))
    return jsonify(success=True, message=f"User '{target['username']}' updated.")


# --------------------------------------------------------------------------- bootstrap
def bootstrap():
    """Create the first owner/cashier accounts when the user table is empty."""
    try:
        row = fetch_one("SELECT COUNT(*) AS n FROM System_Users")
        if row and row["n"] == 0:
            for username, name, pw, role in (
                ("owner", "Shop Owner", "admin123", "owner"),
                ("cashier1", "Cashier One", "cash123", "cashier"),
            ):
                db.execute(
                    "INSERT INTO System_Users (username, full_name, password_hash, role) VALUES (%s, %s, %s, %s)",
                    (username, name, generate_password_hash(pw), role),
                )
            log.warning("Created default accounts owner/admin123 and cashier1/cash123 - CHANGE THESE PASSWORDS after first login.")
    except Exception as exc:
        log.error("Startup check failed (is the database set up? run grain_inventery.sql): %s", exc)


bootstrap()

if __name__ == "__main__":
    host, port = os.getenv("API_HOST", "0.0.0.0"), int(os.getenv("API_PORT", "5000"))
    log.info("Starting API on %s:%s", host, port)
    try:
        from waitress import serve

        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False)
        
        
        
        

