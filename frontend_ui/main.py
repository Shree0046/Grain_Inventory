"""Streamlit entry point: login + role-based navigation.

Owners get a top menu with the Owner Dashboard and the Billing counter; cashiers
go straight to Billing. A sign-in is remembered in a browser cookie, so a page
refresh no longer logs anyone out (until the token expires or they log out).
"""
import json
import os
from urllib.parse import unquote

import streamlit as st
import streamlit.components.v1 as components

import api_client as api
from ui import SHOP_NAME, inject_css, try_api

st.set_page_config(page_title=f"{SHOP_NAME} · Grain Inventory", page_icon="🌾", layout="wide", initial_sidebar_state="collapsed")

COOKIE = "grain_token"
COOKIE_SECONDS = int(os.getenv("TOKEN_HOURS", "12")) * 3600  # same lifetime as the server's login token

st.session_state.setdefault("logged_in", False)
st.session_state.setdefault("token", None)
st.session_state.setdefault("user", None)


# ------------------------------------------------------------------ remember the sign-in
def _write_cookie(value, max_age):
    """Streamlit can read cookies but not write them, so a tiny same-origin script does it in the browser."""
    page = ("<script>try{window.parent.document.cookie = %s + '=' + encodeURIComponent(%s) + "
            "'; path=/; max-age=%d; SameSite=Lax';}catch(e){}</script>" % (json.dumps(COOKIE), json.dumps(value), max_age))
    if hasattr(st, "iframe"):  # newer Streamlit (components.html is being retired)
        st.iframe(page, height=1)
    else:
        components.html(page, height=0)


def try_resume():
    """After a browser refresh the session is new: pick the sign-in back up from the cookie."""
    ss = st.session_state
    if ss.logged_in or ss.get("_no_resume") or ss.get("_resume_tried"):
        return
    ss["_resume_tried"] = True
    saved = st.context.cookies.get(COOKIE)
    if not saved:
        return
    user = api.resume_session(unquote(saved))
    if user:
        ss.update(logged_in=True, user=user, cart={}, last_bill=None)
    else:
        ss["_clear_cookie"] = True  # expired or invalid token: forget it


@st.cache_data(ttl=10, show_spinner=False)
def _server_status():
    return api.health()


# ------------------------------------------------------------------ login page
LOGIN_CSS = """
<style>
.block-container {padding-top: 3.5rem;}
.login-hero {min-height: 470px; padding: 44px 40px; border-radius: 20px; color: #fff;
    background: linear-gradient(145deg, #1b5e20 0%, #2e9e5b 58%, #8bd3a0 100%);
    box-shadow: 0 10px 30px rgba(27,94,32,.28);}
.login-hero .logo {font-size: 3.2rem; line-height: 1;}
.login-hero h1 {color: #fff; margin: 14px 0 4px 0; font-size: 2.1rem !important; padding: 0;}
.login-hero .tag {opacity: .9; margin-bottom: 30px; font-size: 1.05rem;}
.login-hero ul {list-style: none; padding: 0; margin: 0;}
.login-hero li {margin: 14px 0; font-size: 1.02rem; opacity: .96;}
.login-title {margin: 6px 0 2px 0; font-size: 1.7rem; font-weight: 700;}
.pill {display: inline-block; padding: 3px 12px; border-radius: 20px; font-size: .82rem; font-weight: 600;}
.pill.on {background: rgba(46,160,67,.15); color: #2e9e5b;}
.pill.off {background: rgba(220,60,60,.15); color: #d94848;}
.pill.warn {background: rgba(242,162,58,.18); color: #d9822b;}
</style>
"""


def login_page():
    inject_css(hide_sidebar=True)
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    if st.session_state.pop("_clear_cookie", False):
        _write_cookie("", 0)
    _, mid, _ = st.columns([0.3, 6, 0.3])
    with mid:
        hero, form = st.columns([1.1, 1], gap="large", vertical_alignment="center")
        with hero:
            st.markdown(
                f"<div class='login-hero'><div class='logo'>🌾</div><h1>{SHOP_NAME}</h1>"
                "<div class='tag'>Grain inventory &amp; billing</div>"
                "<ul><li>⚡&nbsp; Quick billing with a PDF receipt</li>"
                "<li>📦&nbsp; Live stock with low-stock alerts</li>"
                "<li>📊&nbsp; Simple sales reports for the owner</li>"
                "<li>🔒&nbsp; Separate access for owner and cashier</li></ul></div>",
                unsafe_allow_html=True,
            )
        with form:
            st.markdown("<div class='login-title'>Welcome back 👋</div>", unsafe_allow_html=True)
            st.markdown("<p class='muted'>Sign in with the account your owner gave you.</p>", unsafe_allow_html=True)
            notice = st.session_state.pop("login_notice", None)
            if notice:
                st.info(notice)
            with st.form("login_form", border=False):
                username = st.text_input("Username", placeholder="username")
                password = st.text_input("Password", type="password", placeholder="Your password")
                submit = st.form_submit_button("Sign in  →", type="primary", width="stretch")
            if submit:
                if not username.strip() or not password:
                    st.error("Please enter both your username and password.")
                else:
                    ok, data = try_api(api.login, username.strip(), password, spinner="Signing you in…")
                    if ok:
                        st.session_state.update(logged_in=True, token=data["token"], user=data["user"], cart={}, last_bill=None,
                                                _no_resume=False, _set_cookie=data["token"])
                        st.rerun()
            st.caption("Forgot your password? Ask the owner to reset it.")
            status = _server_status()
            if status is None:
                st.markdown("<span class='pill off'>● Server offline</span>", unsafe_allow_html=True)
                st.error("🔌 Can't reach the server. Start the backend, then refresh this page.")
            elif status is False:
                st.markdown("<span class='pill warn'>● Database problem</span>", unsafe_allow_html=True)
                st.warning("The server is running but cannot reach the database.")
            else:
                st.markdown("<span class='pill on'>● Server online</span>", unsafe_allow_html=True)


# ------------------------------------------------------------------ router
try_resume()

if not st.session_state.logged_in:
    st.navigation([st.Page(login_page, title="Sign in", icon="🔐")], position="hidden").run()
else:
    owner = (st.session_state.user or {}).get("role") == "owner"
    inject_css(hide_sidebar=True, top_nav=owner)
    billing = st.Page("pages/1_Cashier_POS.py", title="Billing Counter", icon="🧾", url_path="billing", default=not owner)
    if owner:
        dashboard = st.Page("pages/2_Owner_Dashboard.py", title="Owner Dashboard", icon="📊", url_path="dashboard", default=True)
        nav = st.navigation([dashboard, billing], position="top")
    else:
        nav = st.navigation([billing], position="hidden")  # a cashier only has one page: no menu needed
    token_to_save = st.session_state.pop("_set_cookie", None)
    if token_to_save:
        _write_cookie(token_to_save, COOKIE_SECONDS)
    nav.run()  # each page draws its own slim header row (see ui.render_header)
