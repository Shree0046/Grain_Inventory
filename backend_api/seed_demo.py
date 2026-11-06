"""Generate ~60 days of realistic sample bills so the analytics have data.

Usage:  python seed_demo.py          (refuses to run if bills already exist)
        python seed_demo.py --force  (adds demo bills anyway)

Also lowers three products' stock so the low-stock alerts have something to show.
"""

import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal

import db


def main(force=False):
    if not force and db.fetch_one("SELECT COUNT(*) AS n FROM Orders")["n"] > 0:
        print("Bills already exist. Use --force to add demo bills anyway.")
        return
    users = db.fetch_all("SELECT user_id FROM System_Users WHERE is_active = 1")
    if not users:
        print("No users yet. Start the backend once (python app.py) so default accounts are created, then retry.")
        return
    products = db.fetch_all("SELECT pid, price, cost_price FROM Product WHERE is_active = 1")
    customers = [c["cust_id"] for c in db.fetch_all("SELECT cust_id FROM Customer")]
    random.seed(42)
    now = datetime.now()
    created = 0
    with db.transaction() as cur:
        for days_ago in range(60, -1, -1):
            day = (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
            n_orders = random.randint(4, 12) + (4 if day.weekday() >= 5 else 0)
            for _ in range(n_orders):
                when = day + timedelta(hours=random.choice([9, 10, 10, 11, 12, 13, 16, 17, 17, 18, 19, 20]), minutes=random.randint(0, 59))
                if when > now:
                    continue
                picks = random.sample(products, random.randint(1, 4))
                lines = [(p, random.choice([1, 1, 1, 2, 2, 3, 5])) for p in picks]
                subtotal = sum(Decimal(p["price"]) * q for p, q in lines)
                discount = Decimal(random.choice([0, 0, 0, 0, 10, 20, 50])) if subtotal > 100 else Decimal(0)
                total = subtotal - discount
                pay = random.choices(["Cash", "UPI", "Card"], weights=[45, 45, 10])[0]
                cur.execute(
                    """INSERT INTO Orders (cust_id, cashier_id, odate, subtotal, discount, total_amt, payment_method, amount_received)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (random.choice(customers) if random.random() < 0.6 else None, random.choice(users)["user_id"],
                     when, subtotal, discount, total, pay, total),
                )
                oid = cur.lastrowid
                cur.execute("UPDATE Orders SET bill_no = %s WHERE oid = %s", (f"GB-{when:%Y%m%d}-{oid:05d}", oid))
                for p, q in lines:
                    cur.execute(
                        "INSERT INTO Order_item (oid, pid, qty, unit_price, unit_cost, line_total) VALUES (%s,%s,%s,%s,%s,%s)",
                        (oid, p["pid"], q, p["price"], p["cost_price"], Decimal(p["price"]) * q),
                    )
                created += 1
        for pid, qty in ((1, 8), (5, 0), (15, 14)):
            cur.execute("UPDATE Stock SET qty = %s WHERE pid = %s", (qty, pid))
    print(f"Created {created} demo bills and set three products to low stock.")


if __name__ == "__main__":
    main("--force" in sys.argv)
