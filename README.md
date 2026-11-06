# Grain Inventory & Billing System

Streamlit frontend + Flask JSON API + MySQL.

```
grain_inventory_system/
├── backend_api/
│   ├── app.py            # Flask API (auth, roles, billing, analytics, logging)
│   ├── db.py             # MySQL connection pool + helpers
│   └── seed_demo.py      # optional: 60 days of demo bills for analytics
├── frontend_ui/
│   ├── main.py           # login + role-based navigation
│   ├── api_client.py     # all API calls, caching, friendly errors
│   ├── ui.py             # styling, receipt preview, header, error display
│   ├── .streamlit/config.toml  # green theme, hides the Deploy bar
│   ├── bill_pdf.py       # PDF bill generator
│   └── pages/
│       ├── 1_Cashier_POS.py
│       └── 2_Owner_Dashboard.py
├── grain_inventery.sql   # database schema + starter data (NEW installs)
├── migrate_v2.sql        # upgrade for databases made with the first version (keeps your data)
├── requirements.txt
└── .env.example
```

## Setup

1. **Database** (MySQL 8.0.16+):
   * **Create Database: create database for as all command are stored in "grain_inventery.sql"
2. **Settings**: copy  `.env` and set your MySQL password.
3. **Install**: `python -m venv venv`, activate it, then `pip install -r requirements.txt`
4. **Start the backend**: `cd backend_api && python app.py`
   (on first start it creates the default users below)
5. **Start the frontend** (new terminal): `cd frontend_ui && streamlit run main.py`
6. *(Optional)* demo data for analytics: `cd backend_api && python seed_demo.py`  (Do it once)

## First login (change these passwords in Owner Dashboard → Manage → Staff accounts)

| Role    | Username   | Password   |
|---------|------------|------------|
| Owner   | `owner`    | `admin123` |
| Cashier | `cashier1` | `cash123`  |

## Who can do what

| Feature                                   | Owner | Cashier |
|-------------------------------------------|:-----:|:-------:|
| Issue bills, download/reprint own bills   |  ✅   |   ✅    |
| Give a discount (choose an owner's offer) |  ✅   |   ✅    |
| Create / edit discount offers             |  ✅   |   ❌    |
| Type a custom discount                    |  ✅   |   ❌    |
| Void a bill (items return to stock)       |  ✅   |   ❌    |
| Analytics, all bills, inventory, products |  ✅   |   ❌    |
| Restock / correct stock                   |  ✅   |   ❌    |
| Manage users, view activity logs          |  ✅   |   ❌    |

Permissions are enforced by the **backend**, not just hidden in the UI.

## Logs

* `backend_api/logs/app.log` - every request (with a short request id), errors, and audit events
* Owner Dashboard → **History** - logins, bills, stock/product/user changes, and every stock movement

## Using it on a shop network

Run the backend on the main PC, then on each cashier PC set `API_URL=http://<server-ip>:5000/api` in `.env`.

## Discount offers (owner decides, cashier applies)

Owner Dashboard → **Manage → Discount offers**. Examples:

* "10% off, up to ₹300, on bills of ₹500 or more"
* "₹100 off on bills of ₹1000 or more"
* "5% loyalty discount for customers who have already shopped ₹5000+" (cashier selects the saved customer)
* Any offer can be limited to dates, switched off, or deleted. Old bills keep the offer's name.

At the billing counter the cashier sees only the offers the bill qualifies for right now, plus a "not yet" list that
says why (e.g. "add ₹300 more"). The server re-checks the rules when the bill is created, so a cashier cannot
bypass them. Only the owner can type a custom discount.

## Void a bill

Owner Dashboard → **Bills** → click a bill → *Void this bill*. A reason is required. The items go back into stock
(shown in History → Stock movements as `VOID`), the sale disappears from all reports, and the bill stays in the list
marked **Voided** with who did it and why. The PDF/receipt shows a VOIDED banner.

## Staying signed in

A sign-in is kept in a browser cookie for 12 hours (`TOKEN_HOURS`), so refreshing the page does not log anyone out.
**Log out** removes it. Use it on shared computers.
