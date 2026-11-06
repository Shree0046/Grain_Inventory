"""Shared UI helpers: styling, receipt preview, guards, friendly error display, header."""
import html
import os

import streamlit as st

import api_client as api

SHOP_NAME = os.getenv("SHOP_NAME", "Grain Store")
SHOP_ADDRESS = os.getenv("SHOP_ADDRESS", "")
SHOP_PHONE = os.getenv("SHOP_PHONE", "")

CSS = """
<style>
/* ---- page frame: use the whole screen ---- */
.block-container {padding-top: 3.6rem; padding-bottom: 2rem; padding-left: 2rem; padding-right: 2rem; max-width: 1600px;}
#MainMenu, footer {visibility: hidden;}
h1 {font-size: 1.7rem !important; padding: .2rem 0 .6rem 0 !important;}
h2 {font-size: 1.3rem !important; padding: .2rem 0 .4rem 0 !important;}
h3 {font-size: 1.1rem !important; padding: .2rem 0 .3rem 0 !important;}
.step {font-weight: 700; font-size: 1.02rem; margin: 0 0 .5rem 0;}
.brand {font-size: 1.15rem; padding-top: .2rem;}

/* ---- text must never be cut or squeezed ---- */
.stButton button, .stDownloadButton button, [data-testid="stPopover"] button {white-space: nowrap;}
.stButton button p, .stDownloadButton button p, [data-testid="stPopover"] button p {white-space: nowrap; overflow: visible;}
.stButton button {padding-left: .6rem; padding-right: .6rem; min-height: 2.4rem;}
[data-testid="stMetric"] {background: rgba(128,128,128,.08); border: 1px solid rgba(128,128,128,.2);
    padding: 12px 14px; border-radius: 12px; min-height: 112px;}
[data-testid="stMetricValue"] {font-size: 1.4rem; line-height: 1.25;}
[data-testid="stMetricValue"] > div, [data-testid="stMetricLabel"] p, [data-testid="stMetricLabel"] > div {
    white-space: normal !important; overflow: visible !important; text-overflow: clip !important;}
[data-testid="stRadio"] [role="radiogroup"], [data-testid="stSegmentedControl"] {flex-wrap: wrap; row-gap: .25rem;}
[data-testid="stRadio"] label p {white-space: nowrap;}
.stButton > button, .stDownloadButton > button {border-radius: 10px; font-weight: 600;}

/* ---- look & feel ---- */
.total-box {display: flex; justify-content: space-between; align-items: center; padding: 12px 16px;
    border-radius: 12px; background: rgba(46,160,67,.12); border: 1px solid rgba(46,160,67,.35);
    font-size: 1.35rem; font-weight: 700; margin: 6px 0 10px 0;}
.muted {opacity: .7; font-size: .88rem;}
.price {font-weight: 700; margin-right: 6px; white-space: nowrap;}
.pname {font-weight: 600; line-height: 1.3;}
mark {background: rgba(255,200,0,.45); color: inherit; padding: 0; border-radius: 2px;}
.badge {display: inline-block; padding: 1px 8px; border-radius: 20px; font-size: .76rem; font-weight: 600; white-space: nowrap;}
.badge.ok {background: rgba(46,160,67,.15); color: #2e9e5b;}
.badge.low {background: rgba(242,162,58,.18); color: #d9822b;}
.badge.out, .badge.void {background: rgba(220,60,60,.15); color: #d94848;}
.offer-pill {display: inline-block; padding: 1px 8px; border-radius: 6px; background: rgba(46,160,67,.14); color: #2e9e5b;
    font-size: .8rem; font-weight: 600;}
.receipt {background: #fff; color: #222; max-width: 520px; margin: 0 auto; padding: 22px 24px; border-radius: 6px;
    box-shadow: 0 4px 18px rgba(0,0,0,.22); font-family: "Courier New", monospace; font-size: .92rem; position: relative;}
.receipt h3 {text-align: center; margin: 0; color: #222; font-family: inherit;}
.receipt .sub {text-align: center; font-size: .8rem; color: #777; margin-bottom: 4px;}
.receipt hr {border: none; border-top: 1px dashed #aaa; margin: 10px 0;}
.receipt table {width: 100%; border-collapse: collapse;}
.receipt th {text-align: left; border-bottom: 1px solid #333; padding: 3px 2px; font-size: .82rem;}
.receipt td {padding: 3px 2px; vertical-align: top;}
.receipt .r {text-align: right; white-space: nowrap;}
.receipt .tot td {font-weight: 700; font-size: 1.1rem; border-top: 1px solid #333; padding-top: 6px;}
.receipt .thanks {text-align: center; margin-top: 12px; color: #555;}
.receipt .voidbanner {text-align: center; color: #c62828; border: 2px solid #c62828; font-weight: 700; padding: 4px; margin-bottom: 8px;
    letter-spacing: 2px;}
</style>
"""
# The sidebar is no longer used (navigation is at the top); this just makes sure it never shows.
HIDE_SIDEBAR = "<style>[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl'] {display: none;}</style>"


def inject_css(hide_sidebar=False, top_nav=False):
    """top_nav=True leaves room for the top menu bar (owner); cashiers have no menu, so the page starts higher."""
    pad = f"<style>.block-container {{padding-top: {'3.4rem' if top_nav else '2rem'};}}</style>"
    st.markdown(CSS + pad + (HIDE_SIDEBAR if hide_sidebar else ""), unsafe_allow_html=True)


def money(x):
    try:
        return f"₹{float(x):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def receipt_html(bill):
    """On-screen preview of a bill that looks like a paper receipt."""
    e = html.escape
    rows = "".join(
        f"<tr><td>{i}</td><td>{e(it['pname'])}</td><td class='r'>{it['qty']}</td>"
        f"<td class='r'>{float(it['unit_price']):,.2f}</td><td class='r'>{float(it['line_total']):,.2f}</td></tr>"
        for i, it in enumerate(bill.get("items", []), 1)
    )
    customer = e(bill.get("customer_name") or "Walk-in customer")
    if bill.get("customer_phone"):
        customer += f" ({e(bill['customer_phone'])})"
    extra = ""
    if float(bill.get("discount") or 0) > 0:
        label = f"Discount ({e(bill['offer_name'])})" if bill.get("offer_name") else "Discount"
        extra += f"<tr><td colspan='4' class='r'>{label}</td><td class='r'>- {float(bill['discount']):,.2f}</td></tr>"
    paid = f"Paid by {e(bill.get('payment_method', ''))}"
    if bill.get("change_due") and float(bill["change_due"]) > 0:
        paid += f" · change returned {float(bill['change_due']):,.2f}"
    address = f"<div class='sub'>{e(SHOP_ADDRESS)}</div>" if SHOP_ADDRESS else ""
    phone = f"<div class='sub'>{e(SHOP_PHONE)}</div>" if SHOP_PHONE else ""
    banner = ""
    if bill.get("status") == "voided":
        why = f"<div style='font-size:.8rem;letter-spacing:0'>{e(bill['void_reason'])}</div>" if bill.get("void_reason") else ""
        banner = f"<div class='voidbanner'>VOIDED{why}</div>"
    return (
        f"<div class='receipt'>{banner}<h3>{e(SHOP_NAME)}</h3>{address}{phone}<hr>"
        f"<div><b>Bill no:</b> {e(str(bill.get('bill_no', '')))}</div>"
        f"<div><b>Date:</b> {e(str(bill.get('odate', '')))}</div>"
        f"<div><b>Customer:</b> {customer}</div>"
        f"<div><b>Cashier:</b> {e(str(bill.get('cashier_name') or '-'))}</div><hr>"
        f"<table><tr><th>#</th><th>Item</th><th class='r'>Qty</th><th class='r'>Rate</th><th class='r'>Amount</th></tr>{rows}</table><hr>"
        f"<table><tr><td colspan='4' class='r'>Subtotal</td><td class='r'>{float(bill['subtotal']):,.2f}</td></tr>{extra}"
        f"<tr class='tot'><td colspan='4' class='r'>TOTAL (₹)</td><td class='r'>{float(bill['total_amt']):,.2f}</td></tr></table>"
        f"<div style='margin-top:6px'>{paid}</div>"
        f"<div class='thanks'>Thank you! Visit again.</div></div>"
    )


def require_login(role=None):
    """Stop the page unless a (matching) user is signed in."""
    if not st.session_state.get("logged_in"):
        st.warning("🔒 Please sign in to continue.")
        st.stop()
    if role and (st.session_state.get("user") or {}).get("role") != role:
        st.error("🚫 You don't have access to this page.")
        st.stop()


def rerun_here():
    """Refresh just the current fragment when possible, otherwise the whole page."""
    try:
        st.rerun(scope="fragment")
    except st.errors.StreamlitAPIException:
        st.rerun()


def show_error(e):
    if isinstance(e, api.ServerUnreachable):
        st.error(f"🔌 {e.message}")
        return
    st.error(f"**{e.message}**", icon="🚫")
    if isinstance(e.details, list):
        for line in e.details:
            st.write(f"• {line}")
    if e.request_id:
        st.caption(f"Reference ID: `{e.request_id}` (quote this if you need help)")


def try_api(fn, *args, spinner="Loading…", **kwargs):
    """Call an API function; on failure show a friendly message and return (False, None)."""
    try:
        with st.spinner(spinner):
            return True, fn(*args, **kwargs)
    except api.SessionExpired:
        st.rerun()
    except api.ApiError as e:
        show_error(e)
    except Exception:  # never show a stack trace to a cashier
        st.error("Something unexpected happened. Please try again.", icon="⚠️")
    return False, None


# ------------------------------------------------------------------ header + logout
def do_logout(notice="You have been logged out."):
    """Button callback: end the session and forget the saved sign-in."""
    api.logout()
    st.session_state.update(logged_in=False, token=None, user=None, cart={}, last_bill=None, login_notice=notice,
                            _no_resume=True, _clear_cookie=True)


def render_header(middle=None, middle_weight=3.0, brand_weight=1.6):
    """ONE slim row at the top: shop name | (page-specific controls) | user menu. Replaces the old sidebar.

    Each page calls this itself, passing a function that draws its own controls in the middle
    (e.g. 'Recent bills' on the billing page, the section menu on the owner dashboard).
    """
    user = st.session_state.get("user") or {}
    owner = user.get("role") == "owner"
    weights = [brand_weight, middle_weight, 2.2] if middle else [5, 2.2]
    cols = st.columns(weights, vertical_alignment="center")
    cols[0].markdown(f"<div class='brand'>🌾 <b>{html.escape(SHOP_NAME)}</b></div>", unsafe_allow_html=True)
    if middle:
        with cols[1]:
            middle()
    with cols[-1]:
        with st.popover(f"👤 {user.get('full_name', '')} · {'Owner' if owner else 'Cashier'}", width="stretch"):
            st.markdown(f"**{html.escape(user.get('full_name', ''))}**  \n<span class='muted'>@{html.escape(user.get('username', ''))} · "
                        f"{'Owner (full access)' if owner else 'Cashier (billing only)'}</span>", unsafe_allow_html=True)
            if st.button("Log out", width="stretch", icon=":material/logout:", key="logout_btn"):
                do_logout()
                st.rerun()  # full-page rerun, even when this header sits inside a fragment
