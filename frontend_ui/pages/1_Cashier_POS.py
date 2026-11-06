"""Billing counter: customer -> find & add items -> bill -> offer -> review & confirm -> preview + download PDF.

The whole screen is one Streamlit fragment, so clicking buttons only refreshes
this area (no full-page reload) and product data is cached for speed.

Safety rules
* A bill is only created when EVERY value is valid. Nothing is silently "corrected":
  invalid quantities or cash amounts are listed as problems and the button stays disabled.
* Discounts come from OFFERS the owner created. A cashier can only pick an offer the bill
  qualifies for (the server checks again); only the owner may type a custom discount.
"""
import html
import re
from difflib import SequenceMatcher

import streamlit as st

import api_client as api
from bill_pdf import build_bill_pdf
from ui import money, receipt_html, render_header, rerun_here, require_login, try_api

require_login()
ss = st.session_state
IS_OWNER = (ss.get("user") or {}).get("role") == "owner"
PAYMENT_METHODS = ["Cash", "UPI", "Card"]
PAGE_SIZE = 6

ss.setdefault("cart", {})  # pid -> {pid, pname, price, qty}
ss.setdefault("last_bill", None)
ss.setdefault("last_pdf", None)
ss.setdefault("_inv_map", {})
ss.setdefault("pos_limit", PAGE_SIZE)
ss.setdefault("pos_confirming", False)
ss.setdefault("pos_offer", "none")  # "none" | an offer id | "custom" (owner only)


# ------------------------------------------------------------------ value checking
def as_int(v):
    """A whole number from a widget value, or None when empty / not a whole number."""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return int(f) if f == f and f.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def as_num(v):
    """A finite number from a widget value, or None."""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ search
def _haystack(p):
    return f"{p['pname']} {p['variety']} {p['category']}".lower()


def search_products(inventory, query, category):
    """Returns (matches, used_fuzzy). All words must match; falls back to typo-tolerant matching."""
    pool = [p for p in inventory if category == "All" or p["category"] == category]
    tokens = query.lower().split()
    fuzzy = False
    if tokens:
        hits = [p for p in pool if all(t in _haystack(p) for t in tokens)]
        if not hits:
            def close(p):
                words = re.findall(r"[a-z0-9]+", _haystack(p))
                return all(any(t in w or SequenceMatcher(None, t, w).ratio() >= 0.75 for w in words) for t in tokens)
            hits = [p for p in pool if close(p)]
            fuzzy = bool(hits)
        pool = hits

    def rank(p):
        name = p["pname"].lower()
        starts = 0 if tokens and name.startswith(tokens[0]) else (1 if tokens and any(w.startswith(tokens[0]) for w in name.split()) else 2)
        return (p["qty"] <= 0, starts, name)

    return sorted(pool, key=rank), fuzzy


def highlight(text, tokens):
    safe = html.escape(text)
    words = [re.escape(html.escape(t)) for t in tokens if t]
    return re.sub(f"({'|'.join(words)})", r"<mark>\1</mark>", safe, flags=re.I) if words else safe


def stock_badge(p):
    if p["qty"] <= 0:
        return "<span class='badge out'>Out of stock</span>"
    if p["qty"] <= p["reorder_level"]:
        return f"<span class='badge low'>Only {p['qty']} left</span>"
    return f"<span class='badge ok'>{p['qty']} in stock</span>"


# ------------------------------------------------------------------ callbacks
def unconfirm():
    """Any edit cancels a pending confirmation, so you can never confirm something you didn't see."""
    ss["pos_confirming"] = False


def ask_confirm():
    ss["pos_confirming"] = True


def add_item(pid):
    prod = ss["_inv_map"].get(pid)
    if prod is None:
        return
    qty = as_int(ss.get(f"addq_{pid}"))
    if qty is None or qty < 1:
        ss["_flash"] = ("Please enter a quantity of 1 or more.", "⚠️")
        return
    in_cart = ss.cart.get(pid, {}).get("qty", 0)
    if in_cart + qty > prod["qty"]:
        ss["_flash"] = (f"Only {prod['qty']} in stock for {prod['pname']} ({in_cart} already on the bill).", "⚠️")
        return
    ss.cart[pid] = {"pid": pid, "pname": prod["pname"], "price": float(prod["price"]), "qty": in_cart + qty}
    ss[f"cartqty_{pid}"] = in_cart + qty
    ss[f"addq_{pid}"] = 1
    if not ss.get("pos_keep_search"):
        ss["pos_search"] = ""
    unconfirm()
    ss["_flash"] = (f"Added {qty} × {prod['pname']}", "✅")


def show_more():
    ss["pos_limit"] = ss.get("pos_limit", PAGE_SIZE) + PAGE_SIZE


def remove_item(pid):
    ss.cart.pop(pid, None)
    ss.pop(f"cartqty_{pid}", None)
    unconfirm()


def clear_cart():
    # Widget keys are reset at the start of the next run (see _reset_fields),
    # because widgets can't be modified after they were drawn in the same run.
    ss.cart = {}
    ss["_reset_pos"] = True
    unconfirm()


def new_bill():
    ss.last_bill = None
    ss.last_pdf = None


def _reset_fields():
    for k in [k for k in ss if str(k).startswith("cartqty_")]:
        del ss[k]
    ss.update(pos_discount=0.0, pos_recv=0.0, pos_new_name="", pos_new_phone="", pos_cust_mode="Walk-in customer", pos_cust=None,
              pos_search="", pos_limit=PAGE_SIZE, pos_confirming=False, pos_offer="none")
    ss["_reset_pos"] = False


# ------------------------------------------------------------------ top row: title + recent bills
def recent_bills_menu():
    """'Recent bills' lives in the top row so a bill can be found and reprinted at any moment."""
    with st.popover("🕘 Recent bills", width="stretch"):
        ok, recent = try_api(api.recent_bills, spinner="Loading your recent bills…")
        if not ok:
            return
        if not recent:
            st.info("You haven't issued any bills yet.")
            return
        labels = {r["oid"]: f"{r['bill_no']} · {r['customer']} · {money(r['total_amt'])}" + (" · VOIDED" if r.get("status") == "voided" else "")
                  for r in recent}
        oid = st.selectbox("Choose a bill to view or download", list(labels), format_func=labels.get, index=None, placeholder="Select a bill…",
                           key="recent_pick")
        if oid:
            ok, bill = try_api(api.get_bill, oid)
            if ok:
                st.markdown(receipt_html(bill), unsafe_allow_html=True)
                try:
                    st.download_button("⬇️ Download PDF", build_bill_pdf(bill), file_name=f"{bill['bill_no']}.pdf", mime="application/pdf",
                                       type="primary", width="stretch")
                except Exception:
                    st.error("Could not create the PDF for this bill.")


# ------------------------------------------------------------------ bill ready (preview + download)
def bill_ready_view(bill):
    st.success(f"✅ Bill **{bill['bill_no']}** generated successfully")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown(receipt_html(bill), unsafe_allow_html=True)
    with right:
        with st.container(border=True):
            st.markdown("#### What next?")
            m1, m2 = st.columns(2)
            m1.metric("Total", money(bill["total_amt"]))
            m2.metric("Paid by", bill["payment_method"])
            if bill.get("offer_name"):
                st.markdown(f"<span class='offer-pill'>🎁 {html.escape(bill['offer_name'])}</span> saved "
                            f"<b>{money(bill['discount'])}</b>", unsafe_allow_html=True)
            if bill.get("change_due") and float(bill["change_due"]) > 0:
                st.info(f"💵 Return change to customer: **{money(bill['change_due'])}**")
            if ss.last_pdf:
                st.download_button("⬇️ Download bill (PDF)", data=ss.last_pdf, file_name=f"{bill['bill_no']}.pdf",
                                   mime="application/pdf", type="primary", width="stretch")
            else:
                st.warning("The bill is saved, but its PDF could not be created. Open 'Recent bills' (top right) to try again.")
            st.button("➕ Start new bill", on_click=new_bill, width="stretch")


# ------------------------------------------------------------------ steps
def customer_step():
    """Returns (cust_id, new_customer_dict_or_None). Also remembers a label for the confirm card."""
    with st.container(border=True):
        st.markdown("<div class='step'>1 · Customer</div>", unsafe_allow_html=True)
        mode = st.radio("Customer type", ["Walk-in customer", "Existing customer", "New customer"], horizontal=True,
                        key="pos_cust_mode", label_visibility="collapsed", on_change=unconfirm)
        ss["_cust_label"] = "Walk-in customer"
        if mode == "Existing customer":
            ok, customers = try_api(api.get_customers, spinner="Loading customers…")
            if ok and customers:
                labels = {c["cust_id"]: f"{c['cname']}" + (f" · {c['phone_no']}" if c["phone_no"] else "") for c in customers}
                cid = st.selectbox("Search by name or phone", options=list(labels), format_func=labels.get, index=None,
                                   placeholder="Start typing a name or phone number…", key="pos_cust", on_change=unconfirm)
                ss["_cust_label"] = labels.get(cid, "")
                return cid, None
            if ok:
                st.info("No customers saved yet. Choose 'New customer' to add one.")
            return None, None
        if mode == "New customer":
            c1, c2 = st.columns(2)
            name = c1.text_input("Name *", key="pos_new_name", placeholder="Customer name", on_change=unconfirm)
            phone = c2.text_input("Phone (optional)", key="pos_new_phone", placeholder="10-digit mobile", on_change=unconfirm)
            ss["_cust_label"] = (name.strip() or "New customer") + (f" · {phone.strip()}" if phone.strip() else "")
            return None, {"name": name.strip(), "phone": phone.strip()}
        st.caption("No customer details will be saved on this bill.")
        return None, None


def customer_problem(cust_id, new_customer):
    mode = ss.get("pos_cust_mode", "Walk-in customer")
    if mode == "Existing customer" and cust_id is None:
        return "Choose the customer from the list, or switch to Walk-in customer."
    if mode == "New customer" and new_customer is not None:
        if not new_customer["name"]:
            return "Enter the new customer's name, or switch to Walk-in customer."
        digits = re.sub(r"[\s\-+()]", "", new_customer["phone"])
        if new_customer["phone"] and (not digits.isdigit() or not 7 <= len(digits) <= 15):
            return "The phone number is not valid (7–15 digits). Correct it or leave it empty."
    return None


def add_items_step(inventory):
    with st.container(border=True):
        st.markdown("<div class='step'>2 · Find & add items</div>", unsafe_allow_html=True)
        query = st.text_input("Search", placeholder="🔍  Type a product, brand or type (e.g. basmati, toor dal) and press Enter",
                              key="pos_search", label_visibility="collapsed").strip()
        if ss.get("_last_query") != query:  # new search -> start from the first page again
            ss["_last_query"], ss["pos_limit"] = query, PAGE_SIZE
        cats = ["All"] + sorted({p["category"] for p in inventory})
        cat = st.pills("Category", cats, default="All", key="pos_cat", label_visibility="collapsed") or "All"

        results, fuzzy = search_products(inventory, query, cat)
        tokens = query.lower().split()
        if not results:
            st.info(f"No products match “{query}”. Try fewer letters or another word." if query else "No products in this category.")
            return
        if fuzzy:
            st.caption(f"No exact match for “{query}” — showing the closest products.")
        else:
            st.caption(f"{len(results)} product(s)" + (f" for “{query}”" if query else "") + " · set the quantity and press **Add**")

        for p in results[: ss["pos_limit"]]:
            pid = p["pid"]
            # Name gets the full width of its column; price + stock sit on the second line so nothing is squeezed.
            c1, c2, c3 = st.columns([6, 1.7, 1.3], vertical_alignment="center")
            c1.markdown(
                f"<div class='pname'>{highlight(p['pname'], tokens)}</div>"
                f"<span class='price'>{money(p['price'])}</span>{stock_badge(p)} <span class='muted'>{html.escape(p['category'])}</span>",
                unsafe_allow_html=True)
            if p["qty"] > 0:
                key = f"addq_{pid}"
                ss.setdefault(key, 1)
                q = as_int(c2.number_input("Qty", step=1, key=key, label_visibility="collapsed"))
                valid = q is not None and 1 <= q <= p["qty"]
                if not valid:
                    c2.markdown(f":red[Enter 1–{p['qty']}]")
                c3.button("Add", key=f"add_{pid}", on_click=add_item, args=(pid,), type="primary", width="stretch", disabled=not valid)
            else:
                c2.caption("—")
        if len(results) > ss["pos_limit"]:
            st.button(f"Show more ({len(results) - ss['pos_limit']} more)", on_click=show_more, width="stretch")
        st.checkbox("Keep my search after adding an item", key="pos_keep_search")


def offer_step(subtotal, cust_id, errors):
    """Choose a discount offer set up by the owner. Returns (payload_fields, discount_amount, offer_name)."""
    try:
        offers = api.get_available_offers(subtotal, cust_id)
    except api.SessionExpired:
        st.rerun()
    except api.ApiError:
        offers = None
    if offers is None:
        st.caption("⚠️ Offers couldn't be loaded right now. You can still bill without a discount.")
        offers = []
    usable = [o for o in offers if o["eligible"]]
    blocked = [o for o in offers if not o["eligible"]]
    by_id = {o["offer_id"]: o for o in usable}

    options = ["none"] + list(by_id) + (["custom"] if IS_OWNER else [])
    if ss.get("pos_offer") not in options:  # e.g. the bill changed and the offer no longer applies
        if ss.get("pos_offer") not in ("none", "custom", None):
            st.toast("The selected offer no longer applies to this bill, so it was removed.", icon="ℹ️")
        ss["pos_offer"] = "none"

    def label(o):
        if o == "none":
            return "No discount"
        if o == "custom":
            return "Custom discount (owner only)"
        return f"{by_id[o]['name']} — saves {money(by_id[o]['discount'])}"

    if usable and ss.get("pos_offer") == "none":
        best = max(usable, key=lambda o: float(o["discount"]))
        st.info(f"🎁 This bill qualifies for **{best['name']}** (saves {money(best['discount'])}). Choose it below to apply.")
    choice = st.selectbox("Discount offer", options, format_func=label, key="pos_offer", on_change=unconfirm)

    fields, discount, offer_name = {}, 0.0, None
    if choice == "custom":
        ss.setdefault("pos_discount", 0.0)
        discount = as_num(st.number_input("Custom discount (₹)", step=10.0, format="%.2f", key="pos_discount", on_change=unconfirm))
        if discount is None or discount < 0:
            errors.append("Discount must be 0 or more.")
            discount = 0.0
        elif discount > subtotal:
            errors.append(f"Discount can't be more than the subtotal ({money(subtotal)}).")
            discount = 0.0
        fields = {"discount": discount}
    elif choice != "none":
        discount = float(by_id[choice]["discount"])
        fields = {"offer_id": choice}
        offer_name = by_id[choice]["name"]
    if blocked:
        with st.expander(f"Other offers that don't apply yet ({len(blocked)})"):
            for o in blocked:
                st.markdown(f"🔒 **{html.escape(o['name'])}**  \n<span class='muted'>{html.escape(o['why_not'])}</span>", unsafe_allow_html=True)
    return fields, discount, offer_name


def bill_step(inventory_map, cust_id, cust_error):
    """Renders the bill. Returns (confirmed_clicked, payload). Never returns True while anything is invalid."""
    errors = []
    with st.container(border=True):
        st.markdown("<div class='step'>3 · Bill</div>", unsafe_allow_html=True)
        if not ss.cart:
            st.info("No items yet. Search and add products on the left.")
            return False, None
        for pid, item in list(ss.cart.items()):
            stock = inventory_map.get(pid, {}).get("qty", 0)
            if stock < 1:
                remove_item(pid)
                st.toast(f"{item['pname']} is now out of stock and was removed.", icon="⚠️")
                continue
            key = f"cartqty_{pid}"
            ss.setdefault(key, item["qty"])
            # Row 1: the name on its own line. Row 2: price, quantity, amount, remove.
            st.markdown(f"<div class='pname'>{html.escape(item['pname'])}</div>", unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns([2.2, 2, 2.2, 0.9], vertical_alignment="center")
            c1.markdown(f"<span class='muted'>{money(item['price'])} each</span>", unsafe_allow_html=True)
            q = as_int(c2.number_input("Qty", step=1, key=key, label_visibility="collapsed", on_change=unconfirm, help=f"{stock} in stock"))
            if q is None or q < 1:
                errors.append(f"{item['pname']}: enter a quantity of 1 or more.")
                c3.markdown(":red[**—**]")
            elif q > stock:
                errors.append(f"{item['pname']}: only {stock} in stock, but {q} entered.")
                c3.markdown(":red[**—**]")
            else:
                item["qty"] = q
                c3.markdown(f"**{money(item['price'] * q)}**")
            c4.button(" ", key=f"rm_{pid}", on_click=remove_item, args=(pid,), help="Remove item", icon=":material/close:")
        if not ss.cart:
            return False, None

        subtotal = round(sum(i["price"] * i["qty"] for i in ss.cart.values()), 2)
        st.divider()
        offer_fields, discount, offer_name = offer_step(subtotal, cust_id, errors)
        total = round(subtotal - discount, 2)
        st.markdown(f"<div class='muted'>Subtotal {money(subtotal)}" + (f" · Discount −{money(discount)}" if discount else "") + "</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='total-box'><span>Total</span><span>{money(total)}</span></div>", unsafe_allow_html=True)

        method = st.radio("Payment method", PAYMENT_METHODS, horizontal=True, key="pos_pay", on_change=unconfirm)
        received, change = None, 0.0
        if method == "Cash":
            ss.setdefault("pos_recv", 0.0)
            r = as_num(st.number_input("Cash received (₹) — leave 0 if exact", step=10.0, format="%.2f", key="pos_recv", on_change=unconfirm))
            if r is None or r < 0:
                errors.append("Cash received must be 0 or more.")
            elif 0 < r and round(r, 2) < total:
                errors.append(f"Cash received is short by {money(total - r)}.")
            else:
                received = r
                if r > total:
                    change = round(r - total, 2)
                    st.info(f"💵 Change to return: **{money(change)}**")
        if cust_error:
            errors.append(cust_error)

        payload = {
            "items": [{"pid": i["pid"], "qty": i["qty"]} for i in ss.cart.values()],
            "payment_method": method,
            "amount_received": received if (method == "Cash" and received and received > 0) else None,
            **offer_fields,
        }

        if errors:
            st.error("**Please fix these before continuing:**  \n" + "  \n".join(f"• {e}" for e in errors))
            st.button("🧾 Review & generate", type="primary", width="stretch", disabled=True)
            return False, None

        if ss.get("pos_confirming"):
            with st.container(border=True):
                units = sum(i["qty"] for i in ss.cart.values())
                offer_line = ""
                if offer_name:
                    offer_line = f"**Offer:** {html.escape(offer_name)} (−{money(discount)})  \n"
                elif discount:
                    offer_line = f"**Custom discount:** −{money(discount)}  \n"
                st.markdown("#### ✅ Please confirm")
                st.markdown(
                    f"**Customer:** {html.escape(ss.get('_cust_label') or 'Walk-in customer')}  \n"
                    f"**Items:** {len(ss.cart)} product(s), {units} unit(s)  \n"
                    f"{offer_line}"
                    f"**Payment:** {method}" + (f" · change {money(change)}" if change else "") + "  \n"
                    f"**Amount to collect:** {money(total)}"
                )
                st.caption("The bill can't be edited after it is generated.")
                c1, c2 = st.columns([3, 2])
                go = c1.button("✅ Confirm & generate bill", type="primary", width="stretch")
                c2.button("✏️ Keep editing", on_click=unconfirm, width="stretch")
            return go, payload

        b1, b2 = st.columns([2, 1])
        b1.button("🧾 Review & generate", type="primary", width="stretch", on_click=ask_confirm)
        b2.button("🗑️ Clear", on_click=clear_cart, width="stretch")
        return False, payload


def checkout(payload, cust_id, new_customer):
    ss["pos_confirming"] = False
    if new_customer is not None and new_customer["name"]:
        ok, cust = try_api(api.create_customer, new_customer["name"], new_customer["phone"], spinner="Saving customer…")
        if not ok:
            return
        cust_id = cust["cust_id"]
    payload["cust_id"] = cust_id
    ok, res = try_api(api.create_order, payload, spinner="Issuing bill…")
    if not ok:
        api.clear_cache()  # stock or offers may have changed - refresh next time
        return
    bill = res["bill"]
    try:
        ss.last_pdf = build_bill_pdf(bill)
    except Exception:
        ss.last_pdf = None
    ss.last_bill = bill
    clear_cart()
    rerun_here()


# ------------------------------------------------------------------ page
@st.fragment
def pos():
    if ss.get("_reset_pos"):
        _reset_fields()
    if ss.get("_flash"):
        msg, icon = ss.pop("_flash")
        st.toast(msg, icon=icon)

    render_header(recent_bills_menu, middle_weight=1.7, brand_weight=6)
    if ss.last_bill:  # a bill was just issued: show its preview + download first
        bill_ready_view(ss.last_bill)
        return

    ok, inventory = try_api(api.get_inventory, spinner="Loading products…")
    if not ok:
        if st.button("🔄 Try again"):
            rerun_here()
        return
    if not inventory:
        st.warning("No products are available yet. Ask the owner to add products.")
        return
    ss["_inv_map"] = {p["pid"]: p for p in inventory}

    left, right = st.columns([1.35, 1], gap="large")
    with left:
        cust_id, new_customer = customer_step()
        add_items_step(inventory)
    with right:
        go, payload = bill_step(ss["_inv_map"], cust_id, customer_problem(cust_id, new_customer))
    if go:
        checkout(payload, cust_id, new_customer)


pos()
