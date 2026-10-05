"""
name: Acme Order Lookup
description: Look up a fictional Acme Retail order by its order id.
category: Sales
"""

# A small table of fictional orders so the tool needs no external credentials
# and is safe to ship as a seed fixture.
_ORDERS = {
    "ACME-1001": {
        "status": "Delivered",
        "delivered": "2026-01-02",
        "items": [
            {"sku": "HT-MUG-500", "name": "Stoneware Mug, 500ml", "qty": 2},
            {"sku": "TK-BLANKET", "name": "Waffle Blanket", "qty": 1},
        ],
        "shipping_speed": "Standard",
        "refund_window_days": 30,
    },
    "ACME-1002": {
        "status": "In Transit",
        "delivered": None,
        "items": [
            {"sku": "EL-CHGR-USBC", "name": "USB-C Charger, 30W", "qty": 1},
        ],
        "shipping_speed": "Expedited",
        "refund_window_days": 30,
    },
    "ACME-1003": {
        "status": "Delivered",
        "delivered": "2025-12-18",
        "items": [
            {"sku": "EL-HEADPHONES", "name": "Wireless Headphones", "qty": 1},
        ],
        "shipping_speed": "Standard",
        "refund_window_days": 15,  # electronics: 15-day window
    },
    "ACME-1004": {
        "status": "Delivered",
        "delivered": "2026-01-10",
        "items": [
            {"sku": "FR-SWEATER-01", "name": "Cotton Crew Sweater (Personalized)", "qty": 1},
        ],
        "shipping_speed": "Standard",
        "refund_window_days": 30,
        "personalized": True,  # returnable only if defective
    },
}


class Tools:
    """Fictional Acme Retail order-lookup tools for the support agent.

    Open WebUI introspects the public methods of this class (type hints +
    docstrings) to build the tool spec, so each method needs a typed signature
    and a docstring.
    """

    def acme_lookup_order(self, order_id: str):
        """Look up a fictional Acme Retail order by its order id.

        Args:
            order_id (str): The order id, e.g. 'ACME-1001'.

        Returns the order status, delivery date, items, shipping speed and the
        refund window in days that applies. Raises a ValueError with a helpful
        message for unknown order ids.
        """
        key = (order_id or "").strip().upper()
        if key not in _ORDERS:
            raise ValueError(
                f"No order found for '{order_id}'. "
                f"Try one of: {', '.join(sorted(_ORDERS))}."
            )
        return _ORDERS[key]
