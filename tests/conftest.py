"""Shared pytest fixtures.

IMPORTANT: env vars must be set before any project module is imported,
because setup.py reads them at import time via os.getenv(). conftest.py
is collected before test modules, so setting them here (at module import
time, before the first `import database`/`import query` in any test file)
is early enough.
"""
import os

os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

import pytest


@pytest.fixture
def sample_order():
    """A representative order matching the shape ORDER_SELECT returns."""
    return {
        "public_order_id": 42,
        "status": "In Transit",
        "priority": "normal",
        "payment_status": "Paid",
        "currency": "USD",
        "subtotal": 100.0,
        "shipping_fee": 5.0,
        "tax_amount": 8.0,
        "total_amount": 113.0,
        "special_instructions": None,
        "ordered_at": "2026-08-01T12:00:00Z",
        "cancelled_at": None,
        "customers": {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "1234567890",
        },
        "order_items": [
            {"product_sku": "SKU1", "product_name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100}
        ],
        "shipments": [
            {
                "carrier": "UPS",
                "tracking_number": "1Z999AA10123456784",
                "delivery_driver_name": "Bob",
                "estimated_delivery_at": "2026-08-05T15:00:00Z",
                "delivered_at": None,
                "delivery_time_slot": "9am-5pm",
            }
        ],
        "order_events": [
            {"event_type": "dispatched", "event_at": "2026-08-02T09:00:00Z", "message": "Left warehouse"}
        ],
    }


@pytest.fixture
def sample_order_no_shipment(sample_order):
    """Same order but with no shipment info yet (tests the 'Not available' branches)."""
    order = dict(sample_order)
    order["shipments"] = []
    return order
