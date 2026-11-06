"""Owner dashboard - kept deliberately simple.

Home (Overview) answers "how is my shop doing today?"; every other section is one
clear job: Sales Report, Stock, Bills, Manage, History.
Only the selected section is built on each run, so the page stays fast.
"""
import html
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import api_client as api
from bill_pdf import build_bill_pdf
from ui import money, receipt_html, render_header, rerun_here, require_login, try_api

require_login("owner")
ss = st.session_state
CONFIG = {"displayModeBar": False}
GREEN, ORANGE = "#2e9e5b", "#f2a23a"
PALETTE = [GREEN, ORANGE, "#4c9be8", "#b565d9", "#e0605e", "#7a8b99"]


def RUPEE(label):
    return st.column_config.NumberColumn(label, format="₹%.2f")


SECTIONS = ["🏠 Overview", "📈 Sales Report", "📦 Stock", "🧾 Bills", "⚙️ Manage", "📜 History"]
ss.setdefault("owner_section", SECTIONS[0])


def section_menu():
    st.radio("Section", SECTIONS, horizontal=True, label_visibility="collapsed", key="owner_section")


render_header(section_menu, middle_weight=7)
section = ss["owner_section"]
st.divider()


# ------------------------------------------------------------------ helpers
def goto(target):
    """Button callback: runs before the section selector is drawn, so it may change it."""
    ss["owner_section"] = target


def pct(cur, prev):
    return None if not prev else f"{(cur - prev) / prev * 100:+.0f}%"


def period_picker(prefix, default="Last 7 days"):
    """Quick period buttons. Returns (start, end) or None while a custom range is incomplete."""
    today, d = date.today(), timedelta
    presets = {
        "Today": (today, today), "Yesterday": (today - d(1), today - d(1)), "Last 7 days": (today - d(6), today),
        "Last 30 days": (today - d(29), today), "This month": (today.replace(day=1), today),
    }
    choice = st.segmented_control("Period", list(presets) + ["Custom"], default=default, key=f"{prefix}_period",
                                 label_visibility="collapsed") or default
    if choice == "Custom":
        rng = st.date_input("Choose dates", value=(today - d(6), today), max_value=today, key=f"{prefix}_custom")
        if not isinstance(rng, tuple) or len(rng) != 2:
            st.caption("Pick both a start date and an end date.")
            return None
        return rng
    return presets[choice]


def with_status(inv):
    df = pd.DataFrame(inv)
    df["is_active"] = df["is_active"].astype(bool)
    df["status"] = "🟢 OK"
    df.loc[df["qty"] <= df["reorder_level"], "status"] = "🟠 Running low"
    df.loc[df["qty"] <= 0, "status"] = "🔴 Out of stock"
    return df


def daily_frame(data, start, end):
    daily = pd.DataFrame(data["daily"])
    if daily.empty:
        return pd.DataFrame({"day": pd.date_range(start, end), "revenue": 0.0, "orders": 0})
    daily["day"] = pd.to_datetime(daily["day"])
    return daily.set_index("day").reindex(pd.date_range(start, end), fill_value=0).rename_axis("day").reset_index()


def sales_bar(daily, height=300):
    daily = daily.assign(label=daily["day"].dt.strftime("%a %d %b"))
    fig = px.bar(daily, x="label", y="revenue", hover_data={"orders": True, "label": False}, labels={"label": "", "revenue": "Sales (₹)", "orders": "Bills"})
    fig.update_traces(marker_color=GREEN)
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=10, b=0), xaxis_title=None)
    return fig


# ================================================================== 1. overview
def overview_section():  # not a fragment: its button must refresh the whole page to switch section
    today = date.today()
    ok, t = try_api(api.get_analytics, today, today, spinner="Loading today's numbers…")
    if not ok:
        return
    ok, w = try_api(api.get_analytics, today - timedelta(days=6), today)
    if not ok:
        return
    ok, inv = try_api(api.get_inventory, include_inactive=True)
    if not ok:
        return
    stock = with_status(inv) if inv else pd.DataFrame(columns=["status", "is_active"])
    need = stock[stock["is_active"] & (stock["status"] != "🟢 OK")] if len(stock) else stock

    st.subheader(f"Today · {today:%A, %d %B}")
    cur, prev = t["summary"], t["previous"]
    k = st.columns(4)
    k[0].metric("Sales today", money(cur["revenue"]), pct(cur["revenue"], prev["revenue"]), help="Compared with yesterday")
    k[1].metric("Bills today", f"{cur['orders']:,}", pct(cur["orders"], prev["orders"]))
    k[2].metric("Profit today (estimate)", money(cur["profit"]))
    k[3].metric("Products to reorder", len(need), delta="check stock" if len(need) else None, delta_color="inverse")

    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        st.subheader("Last 7 days")
        if w["summary"]["orders"] == 0:
            st.info("No sales yet. Bills your cashiers issue will show up here.")
        else:
            st.plotly_chart(sales_bar(daily_frame(w, today - timedelta(days=6), today)), width="stretch", config=CONFIG)
            st.caption(f"This week: **{money(w['summary']['revenue'])}** from **{w['summary']['orders']}** bills.")
    with c2:
        st.subheader("Best sellers this week")
        if w["top_products"]:
            for i, p in enumerate(w["top_products"][:5], 1):
                st.markdown(f"**{i}. {p['pname']}**  \n<span class='muted'>{int(p['qty'])} sold · {money(p['revenue'])}</span>", unsafe_allow_html=True)
        else:
            st.caption("Nothing sold yet this week.")

    st.subheader("Needs your attention")
    if len(need):
        st.warning(f"{len(need)} product(s) are running low or out of stock.")
        st.dataframe(need.sort_values("qty").head(6)[["pname", "qty", "status"]], hide_index=True, width="stretch",
                     column_config={"pname": "Product", "qty": "In stock", "status": "Status"})
        st.button("Open Stock to reorder →", type="primary", on_click=goto, args=("📦 Stock",))
    else:
        st.success("Everything is well stocked. Nothing needs attention right now.")


# ================================================================== 2. sales report
@st.fragment
def sales_section():
    rng = period_picker("sr")
    if not rng:
        return
    start, end = rng
    ok, data = try_api(api.get_analytics, start, end, spinner="Preparing your report…")
    if not ok:
        return
    cur, prev = data["summary"], data["previous"]
    if cur["orders"] == 0:
        st.info("There are no sales in this period. Try a longer period.")
        return

    line = f"From **{start:%d %b}** to **{end:%d %b %Y}** you sold **{money(cur['revenue'])}** from **{cur['orders']:,} bills**"
    if prev["revenue"]:
        change = (cur["revenue"] - prev["revenue"]) / prev["revenue"] * 100
        line += f" — **{abs(change):.0f}% {'more' if change >= 0 else 'less'}** than the period before."
    st.info(line)

    k = st.columns(4)
    k[0].metric("Total sales", money(cur["revenue"]), pct(cur["revenue"], prev["revenue"]), help="Change compared with the period just before")
    k[1].metric("Bills", f"{cur['orders']:,}", pct(cur["orders"], prev["orders"]))
    k[2].metric("Average bill", money(cur["avg_bill"]), pct(cur["avg_bill"], prev["avg_bill"]))
    k[3].metric("Profit (estimate)", money(cur["profit"]), pct(cur["profit"], prev["profit"]), help="Sales minus the cost price of what was sold")
    margin = cur["profit"] / cur["revenue"] * 100 if cur["revenue"] else 0
    st.caption(f"{int(cur['items']):,} items sold · profit margin {margin:.0f}% · discounts given {money(cur['discount'])}")

    daily = daily_frame(data, start, end)
    st.markdown("#### Sales per day")
    st.plotly_chart(sales_bar(daily), width="stretch", config=CONFIG)

    c1, c2 = st.columns(2, gap="large")
    top = pd.DataFrame(data["top_products"])
    with c1:
        st.markdown("#### Best sellers")
        if not top.empty:
            f = px.bar(top.sort_values("revenue"), x="revenue", y="pname", orientation="h", labels={"revenue": "Sales (₹)", "pname": ""})
            f.update_traces(marker_color=GREEN)
            f.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(f, width="stretch", config=CONFIG)
    with c2:
        st.markdown("#### Which type sells most")
        cat = pd.DataFrame(data["categories"])
        if not cat.empty:
            f = px.pie(cat, names="category", values="revenue", hole=0.55, color_discrete_sequence=PALETTE)
            f.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=-0.1))
            st.plotly_chart(f, width="stretch", config=CONFIG)

    with st.expander("More details — busy hours, payments, staff, customers"):
        d1, d2 = st.columns(2, gap="large")
        with d1:
            st.markdown("**Busy hours** (number of bills)")
            hourly = pd.DataFrame(data["hourly"])
            hours = pd.DataFrame({"hour": range(24)}).merge(hourly, on="hour", how="left").fillna(0)
            f = px.bar(hours, x="hour", y="orders", labels={"hour": "Hour of day", "orders": "Bills"})
            f.update_traces(marker_color=ORANGE)
            f.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(f, width="stretch", config=CONFIG)
            st.markdown("**Staff**")
            st.dataframe(pd.DataFrame(data["cashiers"]), hide_index=True, width="stretch",
                         column_config={"cashier": "Name", "orders": "Bills", "revenue": RUPEE("Sales"), "avg_bill": RUPEE("Average bill")})
        with d2:
            st.markdown("**How customers paid**")
            pay = pd.DataFrame(data["payments"])
            st.dataframe(pay, hide_index=True, width="stretch",
                         column_config={"payment_method": "Method", "orders": "Bills", "revenue": RUPEE("Sales")})
            st.markdown("**Best customers**")
            cust = pd.DataFrame(data["top_customers"])
            if cust.empty:
                st.caption("No named customers in this period.")
            else:
                st.dataframe(cust, hide_index=True, width="stretch",
                             column_config={"customer": "Customer", "orders": "Bills", "revenue": RUPEE("Spent")})

    st.download_button("⬇️ Download this report (CSV)", daily[["day", "orders", "revenue"]].to_csv(index=False),
                       file_name=f"sales_{start}_{end}.csv", mime="text/csv")


# ================================================================== 3. stock
@st.fragment
def stock_section():
    ok, inv = try_api(api.get_inventory, include_inactive=True, spinner="Loading stock…")
    if not ok:
        return
    if not inv:
        st.info("No products yet. Add some in Manage → Products.")
        return
    df = with_status(inv)
    active = df[df["is_active"]]
    need = active[active["status"] != "🟢 OK"]

    m = st.columns(4)
    m[0].metric("Products", len(active))
    m[1].metric("Units in stock", f"{int(active['qty'].sum()):,}")
    m[2].metric("Stock value (at cost)", money((active["qty"] * active["cost_price"]).sum()))
    m[3].metric("Need reorder", len(need))

    f1, f2 = st.columns([2, 2])
    show = f1.segmented_control("Show", ["All", "Needs reorder", "Out of stock"], default="Needs reorder" if len(need) else "All",
                                key="st_show", label_visibility="collapsed") or "All"
    search = f2.text_input("Search", placeholder="🔍 Search product…", key="st_search", label_visibility="collapsed").strip().lower()
    view = active
    if show == "Needs reorder":
        view = view[view["status"] != "🟢 OK"]
    elif show == "Out of stock":
        view = view[view["status"] == "🔴 Out of stock"]
    if search:
        view = view[view["pname"].str.lower().str.contains(search, regex=False)]
    if view.empty:
        st.success("Nothing to show here — all good!" if show != "All" else "No products match your search.")
    else:
        st.dataframe(view.sort_values(["qty", "pname"])[["pname", "category", "qty", "status", "price"]], hide_index=True, width="stretch",
                     column_config={"pname": "Product", "category": "Type", "qty": "In stock", "status": "Status", "price": RUPEE("Price")})

    labels = {int(r.pid): f"{r.pname}  ({r.qty} in stock)" for r in active.itertuples()}
    with st.container(border=True):
        st.subheader("➕ New stock arrived")
        with st.form("restock_form", clear_on_submit=True):
            c1, c2 = st.columns([3, 1])
            pid = c1.selectbox("Product", list(labels), format_func=labels.get, index=None, placeholder="Choose the product…")
            qty = c2.number_input("Quantity", min_value=1, step=1, value=10)
            note = st.text_input("Note (optional)", placeholder="Supplier name or invoice number")
            if st.form_submit_button("Add to stock", type="primary"):
                if pid is None:
                    st.error("Please choose a product.")
                else:
                    ok, res = try_api(api.restock, pid, int(qty), note or None, spinner="Updating stock…")
                    if ok:
                        st.toast(res["message"], icon="✅")
                        rerun_here()

    with st.expander("Fix a mistake (damaged goods, spillage, wrong count)"):
        with st.form("adjust_form", clear_on_submit=True):
            c1, c2 = st.columns([3, 1])
            pid = c1.selectbox("Product", list(labels), format_func=labels.get, index=None, placeholder="Choose the product…", key="adj_pid")
            delta = c2.number_input("Change (+/−)", step=1, value=0, help="Use a minus number to reduce stock, e.g. -3")
            note = st.text_input("Reason (required)", placeholder="e.g. 2 bags damaged")
            if st.form_submit_button("Save correction"):
                if pid is None:
                    st.error("Please choose a product.")
                elif delta == 0:
                    st.error("The change can't be zero.")
                elif not note.strip():
                    st.error("Please give a reason so the change can be traced later.")
                else:
                    ok, res = try_api(api.adjust_stock, pid, int(delta), note.strip(), spinner="Updating stock…")
                    if ok:
                        st.toast(res["message"], icon="✅")
                        rerun_here()


# ================================================================== 4. bills
def void_panel(bill):
    """Owner-only: cancel a bill. Items go back to stock and the sale disappears from reports."""
    if bill["status"] == "voided":
        who = bill.get("voided_by_name") or "the owner"
        st.error(f"🚫 **Voided** on {bill.get('voided_at')} by {who}.  \nReason: {bill.get('void_reason') or '-'}")
        return
    with st.expander("🚫 Void this bill (made by mistake / goods returned)"):
        st.caption("The items go back into stock and this sale is removed from your reports. The bill stays in the list marked "
                   "**Voided**, so there is always a record. This cannot be undone.")
        oid = bill["oid"]
        reason = st.text_input("Reason (required)", key=f"void_reason_{oid}", placeholder="e.g. Customer returned the goods")
        sure = st.checkbox("I understand this cannot be undone", key=f"void_sure_{oid}")
        if st.button("Void this bill", type="primary", disabled=not (reason.strip() and sure), key=f"void_go_{oid}"):
            ok, _ = try_api(api.void_order, oid, reason.strip(), spinner="Voiding the bill…")
            if ok:
                st.toast(f"Bill {bill['bill_no']} voided. Items are back in stock.", icon="✅")
                rerun_here()


@st.fragment
def bills_section():
    rng = period_picker("bl")
    if not rng:
        return
    f1, f2 = st.columns([1, 2])
    pm = f1.selectbox("Payment", ["All payments", "Cash", "UPI", "Card"], key="bl_pay", label_visibility="collapsed")
    q = f2.text_input("Search", placeholder="🔍 Bill number, customer name or phone", key="bl_q", label_visibility="collapsed").strip()
    ok, rows = try_api(api.get_orders, rng[0], rng[1], None if pm == "All payments" else pm, q or None, spinner="Loading bills…")
    if not ok:
        return
    if not rows:
        st.info("No bills match these filters.")
        return
    df = pd.DataFrame(rows)
    df["state"] = df["status"].map({"active": "✅ Active", "voided": "🚫 Voided"})
    active = df[df["status"] == "active"]
    voided_n = len(df) - len(active)
    st.markdown(f"**{len(active):,} bills** · total **{money(active['total_amt'].sum())}**" + (f" · {voided_n} voided (not counted)" if voided_n else ""))
    st.caption("Click a row to open that bill.")
    df["offer_name"] = df["offer_name"].fillna("—")
    show = df[["bill_no", "odate", "customer", "cashier", "offer_name", "total_amt", "payment_method", "state"]]
    ev = st.dataframe(show, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="bl_table",
                      column_config={"bill_no": "Bill no", "odate": "Date", "customer": "Customer", "cashier": "Cashier",
                                     "offer_name": "Offer used", "total_amt": RUPEE("Total"), "payment_method": "Payment", "state": "Status"})
    sel = ev.selection.rows
    if sel:
        ok, bill = try_api(api.get_bill, int(df.iloc[sel[0]]["oid"]))
        if ok:
            c1, c2 = st.columns([3, 2], gap="large")
            c1.markdown(receipt_html(bill), unsafe_allow_html=True)
            with c2:
                try:
                    st.download_button("⬇️ Download PDF", build_bill_pdf(bill), file_name=f"{bill['bill_no']}.pdf", mime="application/pdf", type="primary", width="stretch")
                except Exception:
                    st.error("Could not create the PDF for this bill.")
                void_panel(bill)
    st.download_button("⬇️ Download this list (CSV)", show.to_csv(index=False), file_name="bills.csv", mime="text/csv")


# ================================================================== 5. manage
def products_manage():
    ok, cat = try_api(api.get_catalog, spinner="Loading…")
    if not ok:
        return
    var_labels = {v["vid"]: f"{v['cname']} › {v['vname']}" for v in cat["varieties"]}
    mode = st.segmented_control("What do you want to do?", ["Add a product", "Edit a product"], default="Add a product", key="prod_mode") or "Add a product"

    if mode == "Add a product":
        if not var_labels:
            st.info("Create a category and variety first (see the box below).")
        else:
            with st.form("add_product", clear_on_submit=True):
                name = st.text_input("Product name *", placeholder="e.g. India Gate Basmati 5kg")
                vid = st.selectbox("Type *", list(var_labels), format_func=var_labels.get)
                c1, c2, c3, c4 = st.columns(4)
                price = c1.number_input("Selling price (₹) *", min_value=0.0, step=1.0, format="%.2f")
                cost = c2.number_input("Cost price (₹)", min_value=0.0, step=1.0, format="%.2f", help="What you pay for it. Used to estimate profit.")
                reorder = c3.number_input("Warn me when stock is", min_value=0, step=1, value=20, help="Low-stock warning at or below this number")
                opening = c4.number_input("Stock right now", min_value=0, step=1, value=0)
                if st.form_submit_button("Add product", type="primary"):
                    if not name.strip():
                        st.error("Please enter a product name.")
                    elif price <= 0:
                        st.error("Please enter a selling price above zero.")
                    else:
                        ok, _ = try_api(api.create_product, {"pname": name.strip(), "vid": vid, "price": price, "cost_price": cost,
                                                             "reorder_level": int(reorder), "opening_qty": int(opening)}, spinner="Saving…")
                        if ok:
                            st.toast(f"Added {name.strip()}", icon="✅")
                            rerun_here()
    else:
        ok, inv = try_api(api.get_inventory, include_inactive=True)
        if not ok or not inv:
            return
        by_id = {p["pid"]: p for p in inv}
        pid = st.selectbox("Product", list(by_id), format_func=lambda i: by_id[i]["pname"] + ("" if by_id[i]["is_active"] else "  (hidden)"), key="edit_pid")
        p = by_id[pid]
        vids = list(var_labels)
        with st.form(f"edit_product_{pid}"):
            name = st.text_input("Product name", value=p["pname"])
            vid = st.selectbox("Type", vids, index=vids.index(p["vid"]) if p["vid"] in vids else 0, format_func=var_labels.get)
            c1, c2, c3 = st.columns(3)
            price = c1.number_input("Selling price (₹)", min_value=0.0, step=1.0, format="%.2f", value=float(p["price"]))
            cost = c2.number_input("Cost price (₹)", min_value=0.0, step=1.0, format="%.2f", value=float(p["cost_price"]))
            reorder = c3.number_input("Warn me when stock is", min_value=0, step=1, value=int(p["reorder_level"]))
            active = st.toggle("Available for sale", value=bool(p["is_active"]), help="Turn off to hide it from the billing counter. Old bills are kept.")
            if st.form_submit_button("Save changes", type="primary"):
                if not name.strip() or price <= 0:
                    st.error("Name and a selling price above zero are required.")
                else:
                    ok, res = try_api(api.update_product, pid, {"pname": name.strip(), "vid": vid, "price": price, "cost_price": cost,
                                                                "reorder_level": int(reorder), "is_active": active}, spinner="Saving…")
                    if ok:
                        st.toast(res.get("message", "Saved"), icon="✅")
                        rerun_here()

    with st.expander("Need a new category or variety first?"):
        c1, c2 = st.columns(2, gap="large")
        with c1, st.form("add_cat", clear_on_submit=True):
            name = st.text_input("New category", placeholder="e.g. Spices")
            if st.form_submit_button("Add category"):
                if not name.strip():
                    st.error("Please enter a name.")
                else:
                    ok, _ = try_api(api.create_category, name.strip())
                    if ok:
                        rerun_here()
        with c2, st.form("add_var", clear_on_submit=True):
            cats = {c["cid"]: c["cname"] for c in cat["categories"]}
            cid = st.selectbox("Category", list(cats), format_func=cats.get) if cats else None
            name = st.text_input("New variety", placeholder="e.g. Turmeric")
            if st.form_submit_button("Add variety"):
                if cid is None or not name.strip():
                    st.error("Choose a category and enter a name.")
                else:
                    ok, _ = try_api(api.create_variety, cid, name.strip())
                    if ok:
                        rerun_here()


def staff_manage():
    ok, users = try_api(api.get_users, spinner="Loading staff…")
    if not ok:
        return
    df = pd.DataFrame(users)
    df["is_active"] = df["is_active"].astype(bool)
    st.dataframe(df[["full_name", "username", "role", "is_active", "last_login"]], hide_index=True, width="stretch",
                 column_config={"full_name": "Name", "username": "Username", "role": "Role", "is_active": "Active", "last_login": "Last login"})
    c1, c2 = st.columns(2, gap="large")
    with c1, st.container(border=True):
        st.subheader("➕ Add a staff account")
        with st.form("new_user", clear_on_submit=True):
            full = st.text_input("Full name")
            uname = st.text_input("Username", help="3–30 letters, numbers, dot, dash or underscore")
            pw = st.text_input("Password", type="password", help="At least 6 characters")
            role = st.selectbox("Role", ["cashier", "owner"], format_func=lambda r: "Cashier (can only make bills)" if r == "cashier" else "Owner (full access)")
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                if not (full.strip() and uname.strip() and pw):
                    st.error("Please fill in every field.")
                else:
                    ok, res = try_api(api.create_user, uname.strip(), full.strip(), pw, role, spinner="Creating…")
                    if ok:
                        st.toast(res["message"], icon="✅")
                        rerun_here()
    with c2, st.container(border=True):
        st.subheader("✏️ Change an account")
        by_id = {u["user_id"]: u for u in users}
        uid = st.selectbox("Account", list(by_id), format_func=lambda i: f"{by_id[i]['full_name']} ({by_id[i]['username']})", key="mu_uid")
        u = by_id[uid]
        with st.form(f"manage_user_{uid}"):
            role = st.selectbox("Role", ["cashier", "owner"], index=0 if u["role"] == "cashier" else 1)
            active = st.toggle("Account enabled", value=bool(u["is_active"]), help="Turn off to stop someone from signing in")
            pw = st.text_input("New password (leave empty to keep the old one)", type="password")
            if st.form_submit_button("Save", width="stretch"):
                payload = {"role": role, "is_active": active}
                if pw:
                    payload["password"] = pw
                ok, res = try_api(api.update_user, uid, payload, spinner="Saving…")
                if ok:
                    st.toast(res["message"], icon="✅")
                    rerun_here()


# ---- discount offers -------------------------------------------------------
def describe_offer(f):
    """Plain-English sentence of what an offer does (shown live while the owner fills the form)."""
    n = lambda x: f"{float(x):g}"  # noqa: E731
    what = f"{n(f['value'])}% off" if f["discount_type"] == "percent" else f"₹{n(f['value'])} off"
    if f["discount_type"] == "percent" and f["max_discount"]:
        what += f" (never more than ₹{n(f['max_discount'])})"
    who = []
    if f["min_bill"] > 0:
        who.append(f"bills of ₹{n(f['min_bill'])} or more")
    if f["min_total_spent"] > 0:
        who.append(f"customers who have already shopped ₹{n(f['min_total_spent'])}+ in your shop")
    cond = ", and only for ".join(who) if who else "every bill"
    text = f"Customers get **{what}** on {cond}."
    if f.get("valid_from") and f.get("valid_to"):
        text += f" Valid {f['valid_from']:%d %b %Y} to {f['valid_to']:%d %b %Y}."
    return text


def offer_form(key, d=None):
    """Offer fields in plain language. Returns (payload_or_None, errors)."""
    d = d or {}
    today = date.today()
    name = st.text_input("Offer name (cashiers will see this)", value=d.get("name", ""), key=f"{key}_name", placeholder="e.g. Festival 10% off")
    kind = st.radio("Type of discount", ["percent", "flat"], horizontal=True, key=f"{key}_type",
                    index=0 if d.get("discount_type", "percent") == "percent" else 1,
                    format_func=lambda x: "Percentage (% of the bill)" if x == "percent" else "Fixed amount (₹ off)")
    c1, c2 = st.columns(2)
    value = c1.number_input("Percent off" if kind == "percent" else "Rupees off", min_value=0.0, step=1.0, key=f"{key}_value",
                            value=float(d.get("value", 10 if kind == "percent" else 100)))
    min_bill = c2.number_input("Bill must be at least (₹)", min_value=0.0, step=100.0, key=f"{key}_minbill", value=float(d.get("min_bill", 0)),
                               help="0 means any bill qualifies. Example: 1000 → only bills of ₹1000 or more.")
    c3, c4 = st.columns(2)
    spent = c3.number_input("Customer must have shopped before (₹)", min_value=0.0, step=500.0, key=f"{key}_spent",
                            value=float(d.get("min_total_spent", 0)),
                            help="0 means for everyone. Example: 5000 → a loyalty offer for customers who already spent ₹5000 in your shop. "
                                 "The cashier must select the saved customer.")
    cap = c4.number_input("Maximum discount (₹)", min_value=0.0, step=50.0, key=f"{key}_cap", disabled=kind != "percent",
                          value=float(d.get("max_discount") or 0), help="Only for percentage offers. 0 means no limit.")
    limit = st.checkbox("Only valid for certain dates", key=f"{key}_limit", value=bool(d.get("valid_from") or d.get("valid_to")))
    v_from = v_to = None
    errors = []
    if limit:
        rng = st.date_input("Valid from – to", key=f"{key}_dates",
                            value=(d.get("valid_from") or today, d.get("valid_to") or today + timedelta(days=30)))
        if isinstance(rng, tuple) and len(rng) == 2:
            v_from, v_to = rng
        else:
            errors.append("Pick both the first and the last date.")
    if not name.strip():
        errors.append("Give the offer a name.")
    if value <= 0:
        errors.append("The discount must be more than zero.")
    if kind == "percent" and value > 100:
        errors.append("A percentage can't be more than 100.")
    f = {"name": name.strip(), "discount_type": kind, "value": value, "min_bill": min_bill, "min_total_spent": spent,
         "max_discount": cap if kind == "percent" and cap > 0 else None, "valid_from": v_from, "valid_to": v_to}
    st.info("👀 " + describe_offer(f))
    for e in errors:
        if e.startswith("Give the offer") and not name:
            st.caption("👆 Type a name for the offer to continue.")  # gentle hint on a fresh form, not a red error
        else:
            st.error(e)
    if errors:
        return None, errors
    return {**f, "valid_from": str(v_from) if v_from else None, "valid_to": str(v_to) if v_to else None}, []


def offers_manage():
    ok, offers = try_api(api.get_offers, spinner="Loading offers…")
    if not ok:
        return
    st.caption("You decide the discounts. Cashiers can only pick an offer here when the bill qualifies — they cannot type their own discount.")
    ss.setdefault("offer_form_n", 0)
    with st.expander("➕ Create a new offer", expanded=not offers):
        payload, _ = offer_form(f"newoffer{ss.offer_form_n}")
        if st.button("Create offer", type="primary", disabled=payload is None, key=f"create_offer_{ss.offer_form_n}"):
            ok, res = try_api(api.create_offer, payload, spinner="Saving offer…")
            if ok:
                ss.offer_form_n += 1  # fresh, empty form next time
                st.toast(res["message"], icon="✅")
                rerun_here()
    if not offers:
        st.info("No offers yet. Create your first one above.")
        return
    st.markdown(f"#### Your offers ({len(offers)})")
    for o in offers:
        oid = o["offer_id"]
        with st.container(border=True):
            c1, c2 = st.columns([5, 1.3], vertical_alignment="center")
            today_s = str(date.today())
            if not o["is_active"]:
                state = "<span class='badge out'>Off</span>"
            elif o.get("valid_to") and str(o["valid_to"])[:10] < today_s:
                state = "<span class='badge low'>Expired</span>"
            elif o.get("valid_from") and str(o["valid_from"])[:10] > today_s:
                state = "<span class='badge low'>Starts later</span>"
            else:
                state = "<span class='badge ok'>On</span>"
            c1.markdown(f"**{html.escape(o['name'])}** &nbsp;{state}  \n<span class='muted'>{html.escape(o['rule_text'])}</span>  \n"
                        f"<span class='muted'>Used {int(o['times_used'])} time(s) · {money(o['total_discount'])} given as discount</span>",
                        unsafe_allow_html=True)
            c2.toggle("Active", value=bool(o["is_active"]), key=f"offer_on_{oid}_{o['is_active']}",
                      on_change=lambda oid=oid: _toggle_offer(oid), help="Switch off to stop cashiers using it. Nothing is deleted.")
            with st.expander("Edit or delete"):
                payload, _ = offer_form(f"editoffer{oid}", o)
                b1, b2 = st.columns([1, 1])
                if b1.button("Save changes", type="primary", disabled=payload is None, key=f"save_offer_{oid}", width="stretch"):
                    ok, res = try_api(api.update_offer, oid, {**payload, "is_active": bool(o["is_active"])}, spinner="Saving…")
                    if ok:
                        st.toast(res["message"], icon="✅")
                        rerun_here()
                sure = b2.checkbox("Yes, delete this offer", key=f"del_sure_{oid}")
                if b2.button("🗑️ Delete", key=f"del_offer_{oid}", disabled=not sure, width="stretch"):
                    ok, res = try_api(api.delete_offer, oid, spinner="Deleting…")
                    if ok:
                        st.toast(res["message"], icon="✅")
                        rerun_here()
                st.caption("Bills that already used this offer keep its name. Deleting never changes past bills.")


def _toggle_offer(oid):
    """Toggle callback: flip the offer on/off."""
    try:
        cur = next(o for o in api.get_offers() if o["offer_id"] == oid)
        api.update_offer(oid, {"is_active": not cur["is_active"]})
        ss["_flash_owner"] = (f"Offer '{cur['name']}' is now {'ON' if not cur['is_active'] else 'OFF'}.", "✅")
    except api.ApiError as e:
        ss["_flash_owner"] = (e.message, "⚠️")


@st.fragment
def manage_section():
    if ss.get("_flash_owner"):
        msg, icon = ss.pop("_flash_owner")
        st.toast(msg, icon=icon)
    which = st.segmented_control("Manage", ["Products", "Discount offers", "Staff accounts"], default="Products", key="mg_which",
                                 label_visibility="collapsed") or "Products"
    if which == "Products":
        products_manage()
    elif which == "Discount offers":
        offers_manage()
    else:
        staff_manage()


# ================================================================== 6. history
@st.fragment
def history_section():
    which = st.segmented_control("Log", ["Who did what", "Stock movements"], default="Who did what", key="log_which", label_visibility="collapsed") or "Who did what"
    c1, c2 = st.columns([1, 3])
    limit = c1.selectbox("Show last", [100, 300, 1000], index=1, key="log_limit")
    if which == "Who did what":
        q = c2.text_input("Search", placeholder="🔍 Search a name or action", key="log_q", label_visibility="collapsed").strip()
        ok, rows = try_api(api.get_audit_log, limit, q or None, spinner="Loading history…")
        cfg = {"created_at": "Time", "username": "User", "action": "Action", "details": "Details", "ip_address": "IP", "log_id": None}
    else:
        c2.caption("Every sale, delivery and correction changes stock — all are listed here.")
        ok, rows = try_api(api.get_stock_log, limit, spinner="Loading stock movements…")
        cfg = {"timestamp": "Time", "pname": "Product", "change_qty": "Change", "qty_after": "Stock after", "transaction_type": "Type",
               "note": "Note", "user": "By", "log_id": None}
    if not ok:
        return
    if not rows:
        st.info("Nothing recorded yet.")
        return
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, width="stretch", column_config=cfg)
    st.download_button("⬇️ Download (CSV)", df.to_csv(index=False), file_name="history.csv", mime="text/csv")


{
    SECTIONS[0]: overview_section, SECTIONS[1]: sales_section, SECTIONS[2]: stock_section,
    SECTIONS[3]: bills_section, SECTIONS[4]: manage_section, SECTIONS[5]: history_section,
}[section]()
