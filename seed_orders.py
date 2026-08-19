"""Generate realistic synthetic data for the normalized Supabase order schema.

By default this script only writes a JSON file. Add --insert after reviewing the
output to insert the generated records into Supabase.

Examples:
    python seed_orders.py --customers 100 --orders-per-customer 3
    python seed_orders.py --customers 100 --orders-per-customer 3 --insert
"""

import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from faker import Faker


STATUSES = ["Processing", "In Transit", "Out for Delivery", "Delivered", "Cancelled", "Failed Delivery"]
STATUS_WEIGHTS = [12, 16, 8, 54, 6, 4]
ITEMS = [
    "Wireless Headphones", "USB-C Charger", "Running Shoes", "Coffee Maker",
    "Laptop Stand", "Water Bottle", "Backpack", "Desk Lamp", "Phone Case",
    "Bluetooth Speaker", "Yoga Mat", "Smart Watch",
]
TIME_SLOTS = ["9 AM - 12 PM", "12 PM - 3 PM", "3 PM - 6 PM", "6 PM - 9 PM"]
PRIORITIES = ["Standard", "Standard", "Standard", "Express", "Priority"]


def normalized_phone(fake: Faker) -> str:
    """Return a 10-digit phone number compatible with the app's current lookup."""
    return "".join(character for character in fake.numerify("##########") if character.isdigit())


def make_order(fake: Faker, customer_id: int, order_id: int) -> tuple[dict, list[dict], dict | None, dict, dict, dict]:
    status = random.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
    order_date = fake.date_between(start_date="-90d", end_date="today")
    delivery_date = order_date + timedelta(days=random.randint(2, 7))
    estimated = datetime.combine(delivery_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=random.randint(9, 20), minutes=random.choice([0, 15, 30, 45])
    )
    quantity = random.randint(1, 4)
    selected_items = random.sample(ITEMS, k=min(random.randint(1, 3), len(ITEMS)))
    item_prices = [Decimal(str(random.randint(12, 120))) for _ in selected_items]
    subtotal = sum((price * quantity for price in item_prices), Decimal("0"))
    shipping_fee = Decimal("0") if status == "Cancelled" else Decimal(str(random.choice([0, 4, 7, 12])))
    tax_amount = (subtotal * Decimal("0.08")).quantize(Decimal("0.01"))
    order = {
        "public_order_id": order_id,
        "customer_id": customer_id,
        "status": status,
        "priority": random.choice(PRIORITIES),
        "payment_status": "Refunded" if status == "Cancelled" else "Paid",
        "currency": "USD",
        "subtotal": str(subtotal),
        "shipping_fee": str(shipping_fee),
        "tax_amount": str(tax_amount),
        "special_instructions": random.choice([None, "Leave at reception", "Call on arrival", "Leave at front door"]),
        "ordered_at": datetime.combine(order_date, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "cancelled_at": datetime.combine(order_date, datetime.min.time(), tzinfo=timezone.utc).isoformat() if status == "Cancelled" else None,
    }
    items = [
        {
            "order_id": order_id,
            "product_sku": f"DEMO-{index + 1:03d}",
            "product_name": item,
            "quantity": quantity,
            "unit_price": str(price),
        }
        for index, (item, price) in enumerate(zip(selected_items, item_prices))
    ]
    shipment = None
    if status not in {"Cancelled", "Processing"}:
        shipment = {
            "order_id": order_id,
            "carrier": "DemoCarrier",
            "tracking_number": f"DEMO{order_id}",
            "delivery_driver_name": fake.name(),
            "estimated_delivery_at": estimated.isoformat(),
            "delivered_at": (estimated + timedelta(minutes=random.randint(-45, 120))).isoformat() if status == "Delivered" else None,
            "delivery_time_slot": random.choice(TIME_SLOTS),
        }
    event_type = {
        "Processing": "processing", "In Transit": "dispatched", "Out for Delivery": "out_for_delivery",
        "Delivered": "delivered", "Cancelled": "cancelled", "Failed Delivery": "failed_delivery",
    }[status]
    event = {
        "order_id": order_id,
        "event_type": "created",
        "event_at": order["ordered_at"],
        "message": "Order created",
    }
    status_event = {
        "order_id": order_id,
        "event_type": event_type,
        "event_at": (shipment or {"estimated_delivery_at": order["ordered_at"]})["estimated_delivery_at"],
        "message": f"Order status: {status}",
    }
    payment = {
        "order_id": order_id,
        "provider": "demo-payment-provider",
        "provider_payment_id": f"demo_payment_{order_id}",
        "amount": str(subtotal + shipping_fee + tax_amount),
        "currency": "USD",
        "status": "Refunded" if status == "Cancelled" else "Paid",
        "paid_at": order["ordered_at"],
    }

    return order, items, shipment, event, status_event, payment


def make_records(customers: int, orders_per_customer: int, start_order_id: int, seed: int) -> dict[str, list[dict]]:
    random.seed(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    records = {table: [] for table in (
        "customers", "customer_addresses", "orders", "order_delivery_addresses",
        "order_items", "shipments", "order_events", "payments",
    )}
    for customer_number in range(customers):
        full_name = fake.name()
        customer_id = customer_number + 1
        customer = {
            "id": customer_id,
            "customer_name": full_name,
            "full_name": full_name,
            "email": f"demo.{customer_number + 1}.{fake.user_name()}@example.test".lower(),
            "phone": normalized_phone(fake),
        }
        customer.pop("customer_name")
        records["customers"].append(customer)
        address = {
            "customer_id": customer_id, "label": "Home", "line1": fake.street_address(),
            "city": fake.city(), "state_or_region": fake.state_abbr(), "postal_code": fake.postcode(),
            "country_code": "US", "is_default": True,
        }
        records["customer_addresses"].append(address)
        for order_number in range(orders_per_customer):
            order_id = start_order_id + customer_number * orders_per_customer + order_number
            order, items, shipment, created_event, status_event, payment = make_order(fake, customer_id, order_id)
            records["orders"].append(order)
            records["order_delivery_addresses"].append({
                "order_id": order["public_order_id"], "recipient_name": full_name, "line1": address["line1"],
                "city": address["city"], "state_or_region": address["state_or_region"],
                "postal_code": address["postal_code"], "country_code": address["country_code"],
            })
            records["order_items"].extend(items)
            if shipment:
                records["shipments"].append(shipment)
            records["order_events"].extend([created_event, status_event])
            records["payments"].append(payment)
    return records


def insert_records(records: dict[str, list[dict]], batch_size: int) -> None:
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env before using --insert. "
            "Use the Supabase service_role key only for local/admin seeding; never expose it to Streamlit clients."
        )

    from supabase import create_client

    client = create_client(url, key)
    insert_order = [
        "customers", "customer_addresses", "orders", "order_delivery_addresses",
        "order_items", "shipments", "order_events", "payments",
    ]
    for table in insert_order:
        rows = records[table]
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            client.table(table).insert(batch).execute()
            print(f"Inserted {start + 1}-{start + len(batch)} of {len(rows)} {table} records.")
    client.rpc("sync_order_number_sequence").execute()
    client.rpc("sync_simple_id_sequences").execute()
    print("Synchronized the next automatic order number.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create synthetic records for the normalized order schema using Faker.")
    parser.add_argument("--customers", type=int, default=50, help="Number of unique fake customers (default: 50).")
    parser.add_argument("--orders-per-customer", type=int, default=2, help="Orders per customer (default: 2).")
    parser.add_argument("--start-order-id", type=int, default=1, help="First synthetic order ID, from 1 to 1000 (default: 1).")
    parser.add_argument("--seed", type=int, default=20260819, help="Seed for reproducible data.")
    parser.add_argument("--output", type=Path, default=Path("data/normalized_fake_orders.json"), help="JSON output path.")
    parser.add_argument("--insert", action="store_true", help="Insert records into Supabase after writing JSON.")
    parser.add_argument("--batch-size", type=int, default=100, help="Supabase insert batch size (default: 100).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.customers < 1 or args.orders_per_customer < 1 or args.batch_size < 1:
        raise ValueError("--customers, --orders-per-customer, and --batch-size must be positive.")
    generated_order_count = args.customers * args.orders_per_customer
    if args.start_order_id < 1 or args.start_order_id + generated_order_count - 1 > 1000:
        raise ValueError("Generated order IDs must stay within the supported range of 1 to 1000.")

    records = make_records(args.customers, args.orders_per_customer, args.start_order_id, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Generated {len(records['orders'])} synthetic orders for {args.customers} customers at {args.output}.")
    print(f"Order IDs: {records['orders'][0]['public_order_id']} through {records['orders'][-1]['public_order_id']}.")

    if args.insert:
        insert_records(records, args.batch_size)


if __name__ == "__main__":
    main()
