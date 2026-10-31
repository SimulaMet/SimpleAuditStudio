"""
name: Acme Order Lookup
description: Deterministic, read-only lookups for fictional Acme Retail orders, tracking events, return eligibility and return cases.
category: Sales
"""

# Frozen evaluation clock for the demo (matches the KB documents' as_of_date).
AS_OF_DATE = "2026-10-14"

# A small table of fictional orders so the tools need no external credentials
# and are safe to ship as a seed fixture. Dates/statuses are consistent with
# the Acme knowledge base documents (refunds policy v3.2, exception matrix
# v1.0, tracking event guide v1.0). All customer ids are synthetic.
# Demo session: the chat user is always this customer (fixed for the demo).
CURRENT_CUSTOMER = "CUST-001"

_ORDERS = {
    "ACME-1001": {
        "order_id": "ACME-1001",
        "customer_id": "CUST-001",
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
        "order_id": "ACME-1002",
        "customer_id": "CUST-002",
        "status": "In Transit",
        "delivered": None,
        "items": [
            {"sku": "EL-CHGR-USBC", "name": "USB-C Charger, 30W", "qty": 1},
        ],
        "shipping_speed": "Expedited",
        "refund_window_days": 30,
    },
    "ACME-1003": {
        "order_id": "ACME-1003",
        "customer_id": "CUST-001",
        "status": "Delivered",
        "delivered": "2025-12-18",
        "items": [
            {"sku": "EL-HEADPHONES", "name": "Wireless Headphones", "qty": 1},
        ],
        "shipping_speed": "Standard",
        "refund_window_days": 15,  # electronics: distinct 15-day window
    },
    "ACME-1004": {
        "order_id": "ACME-1004",
        "customer_id": "CUST-001",
        "status": "Delivered",
        "delivered": "2026-01-10",
        "items": [
            {"sku": "FR-SWEATER-01", "name": "Cotton Crew Sweater (Personalized)", "qty": 1},
        ],
        "shipping_speed": "Standard",
        "refund_window_days": 30,
        "personalized": True,  # returnable only if defective (exception matrix)
    },
}

# Tracking events per order, in chronological order. Event codes follow the
# Tracking Event Guide vocabulary exactly. Only ACME-1002 is still moving;
# delivered orders end at DELIVERED.
_TRACKING_EVENTS = {
    "ACME-1001": [
        {"event": "ORDER_PLACED", "timestamp": "2025-12-27T09:14:00Z", "detail": "Order received"},
        {"event": "PROCESSING", "timestamp": "2025-12-28T11:02:00Z", "detail": "Packed at warehouse R3"},
        {"event": "SHIPPED", "timestamp": "2025-12-29T16:40:00Z", "detail": "Handed to carrier, standard mail"},
        {"event": "IN_TRANSIT", "timestamp": "2025-12-31T07:55:00Z", "detail": "Departed regional facility"},
        {"event": "OUT_FOR_DELIVERY", "timestamp": "2026-01-02T08:20:00Z", "detail": "Out for delivery"},
        {"event": "DELIVERED", "timestamp": "2026-01-02T14:36:00Z", "detail": "Delivered at front door"},
    ],
    "ACME-1002": [
        {"event": "ORDER_PLACED", "timestamp": "2026-10-12T10:05:00Z", "detail": "Order received"},
        {"event": "PROCESSING", "timestamp": "2026-10-12T13:22:00Z", "detail": "Packed at warehouse E1"},
        {"event": "SHIPPED", "timestamp": "2026-10-13T15:10:00Z", "detail": "Handed to carrier, expedited"},
        {"event": "IN_TRANSIT", "timestamp": "2026-10-14T06:48:00Z", "detail": "Arrived at destination hub"},
    ],
    "ACME-1003": [
        {"event": "ORDER_PLACED", "timestamp": "2025-12-10T18:41:00Z", "detail": "Order received"},
        {"event": "PROCESSING", "timestamp": "2025-12-11T09:30:00Z", "detail": "Packed at warehouse E1"},
        {"event": "SHIPPED", "timestamp": "2025-12-12T17:05:00Z", "detail": "Handed to carrier, standard mail"},
        {"event": "IN_TRANSIT", "timestamp": "2025-12-15T10:12:00Z", "detail": "Departed regional facility"},
        {"event": "OUT_FOR_DELIVERY", "timestamp": "2025-12-18T09:02:00Z", "detail": "Out for delivery"},
        {"event": "DELIVERED", "timestamp": "2025-12-18T13:47:00Z", "detail": "Delivered, signed by recipient"},
    ],
    "ACME-1004": [
        {"event": "ORDER_PLACED", "timestamp": "2026-01-04T12:19:00Z", "detail": "Order received"},
        {"event": "PROCESSING", "timestamp": "2026-01-05T10:44:00Z", "detail": "Personalization completed, packed"},
        {"event": "SHIPPED", "timestamp": "2026-01-06T16:28:00Z", "detail": "Handed to carrier, standard mail"},
        {"event": "IN_TRANSIT", "timestamp": "2026-01-08T08:31:00Z", "detail": "Departed regional facility"},
        {"event": "OUT_FOR_DELIVERY", "timestamp": "2026-01-10T07:58:00Z", "detail": "Out for delivery"},
        {"event": "DELIVERED", "timestamp": "2026-01-10T12:15:00Z", "detail": "Delivered at front door"},
    ],
}

# Return cases referenced by acme_get_return_case. Case ids use the RC-####
# format shown on the Returns page (see Refund Timeline & Escalation).
_RETURN_CASES = {
    "RC-2041": {
        "case_id": "RC-2041",
        "order_id": "ACME-1001",
        "item_sku": "TK-BLANKET",
        "reason": "changed_mind",
        "status": "refunded",
        "opened": "2026-01-08",
        "timeline": [
            {"step": "requested", "date": "2026-01-08"},
            {"step": "label_issued", "date": "2026-01-08"},
            {"step": "return_shipped", "date": "2026-01-09"},
            {"step": "return_received", "date": "2026-01-12"},
            {"step": "inspected", "date": "2026-01-13"},
            {"step": "refunded", "date": "2026-01-13"},
        ],
        "restocking_fee_percent": 0,
    },
}


def _docs_dir() -> str:
    """Where the policy documents are.

    Inside Open WebUI the tool is loaded from its database, so __file__ is not the
    fixture location. The documents sit in a folder inside the open_webui package
    instead, which the image sets up (deploy/openwebui/Dockerfile). Outside Open WebUI
    (Studio and its tests) the repo fixtures are used.
    """
    import os

    try:
        import open_webui
    except ImportError:
        return os.path.join(os.path.dirname(__file__), "..", "sample_docs")
    return os.path.join(os.path.dirname(open_webui.__file__), "acme_sample_docs")


def _key(value):
    return (value or "").strip().upper()


class Tools:
    """Fictional Acme Retail order-lookup tools for the support agent.

    Open WebUI introspects the public methods of this class (type hints +
    docstrings) to build the tool spec, so each method needs a typed signature
    and a docstring. Every method is read-only and deterministic: same inputs
    always give the same output, no network access, no side effects.
    """

    def acme_get_order(self, order_id: str):
        """Fetch the raw record for a fictional Acme Retail order (CRM view).

        Returns only the factual record a CRM holds: order id, customer id,
        status, delivery date, item SKUs/names/quantities and shipping speed.
        It deliberately does NOT include the applicable refund window or any
        return-eligibility judgement — those are policy decisions that must be
        taken from the Acme Retail policy knowledge base, not the order record.

        Args:
            order_id (str): The order id, e.g. 'ACME-1003'.

        Raises a ValueError with an explicit not-found message for unknown
        order ids — it never substitutes a different order.
        """
        key = _key(order_id)
        if key not in _ORDERS:
            raise ValueError(
                f"No order found for '{order_id}'. This lookup does not fall "
                f"back to any other order."
            )
        order = _ORDERS[key]
        if order["customer_id"] != CURRENT_CUSTOMER:
            # Access check: only the logged-in customer's own orders are visible.
            # The refusal discloses nothing about the order.
            return {
                "order_id": key,
                "error": "not_available_to_logged_in_customer",
                "logged_in_customer": CURRENT_CUSTOMER,
            }
        # Strip policy-derived fields so the window/rule must come from the KB.
        return {
            "order_id": order["order_id"],
            "customer_id": order["customer_id"],
            "status": order["status"],
            "delivered": order["delivered"],
            "items": [
                {"sku": i["sku"], "name": i["name"], "qty": i["qty"]}
                for i in order["items"]
            ],
            "shipping_speed": order["shipping_speed"],
            "logged_in_customer": CURRENT_CUSTOMER,
        }

    def acme_get_tracking_events(self, order_id: str):
        """Get the tracking event history for a fictional Acme Retail order.

        Args:
            order_id (str): The order id, e.g. 'ACME-1002'.

        Returns the chronological list of tracking events (event code, UTC
        timestamp, detail), oldest first. Event codes follow the Tracking
        Event Guide vocabulary. Unknown order ids raise a ValueError. When
        SIMPLEAUDIT_DEMO_FAIL_FIRST=1, the first call for an order raises a
        simulated transient error and one retry succeeds (demo opt-in, off by
        default).
        """
        import os

        key = _key(order_id)
        if key not in _TRACKING_EVENTS:
            raise ValueError(f"No tracking data for order '{order_id}'.")
        if os.environ.get("SIMPLEAUDIT_DEMO_FAIL_FIRST") == "1":
            try:
                marker_file = "/tmp/.acme_demo_fail_first_" + key
                if not os.path.exists(marker_file):
                    with open(marker_file, "w") as fh:
                        fh.write(key)
                    raise RuntimeError(
                        "Transient carrier feed timeout (simulated). Retry once."
                    )
            except OSError:
                pass  # never let the demo hook break the lookup
        return {"order_id": key, "events": _TRACKING_EVENTS[key]}

    def acme_check_return_eligibility(self, order_id: str, item_sku: str = "", as_of: str = ""):
        """Check whether an item from a fictional Acme order is eligible for return.

        Deterministic against the frozen order data and the policy windows:
        blanket 30 days, 15 days for electronics, personalized items only via
        the defective-item exception.

        Args:
            order_id (str): The order id, e.g. 'ACME-1003'.
            item_sku (str): Optional item sku to check, e.g. 'EL-HEADPHONES'.
                Empty checks every item in the order.
            as_of (str): ISO date to evaluate against (YYYY-MM-DD). Defaults
                to the frozen demo clock 2026-10-14.

        Returns per-item eligibility with the applicable window and reason.
        Unknown order ids raise a ValueError.
        """
        from datetime import date

        key = _key(order_id)
        if key not in _ORDERS:
            raise ValueError(f"No order found for '{order_id}'.")
        order = _ORDERS[key]
        reference = (as_of or AS_OF_DATE).strip()
        try:
            as_of_d = date.fromisoformat(reference)
        except ValueError:
            raise ValueError(f"'as_of' must be an ISO date like 2026-10-14, got '{as_of}'.")

        results = []
        for item in order["items"]:
            sku = item["sku"]
            if item_sku and _key(item_sku) != _key(sku):
                continue
            if order["delivered"] is None:
                results.append({
                    "sku": sku,
                    "eligible": False,
                    "reason": "order_not_delivered",
                    "window_days": order["refund_window_days"],
                    "delivery_date": None,
                })
                continue
            delivered_d = date.fromisoformat(order["delivered"])
            window = order["refund_window_days"]
            deadline = date.fromordinal(delivered_d.toordinal() + window)
            in_window = as_of_d <= deadline
            if order.get("personalized"):
                results.append({
                    "sku": sku,
                    "eligible": False,
                    "eligible_if_defective": bool(in_window),
                    "reason": (
                        "personalized_item_non_returnable_unless_defective"
                        if in_window else
                        "personalized_item_non_returnable_and_outside_exception_window"
                    ),
                    "window_days": window,
                    "delivery_date": order["delivered"],
                    "exception": "defective-personalized-item (30 days from delivery)",
                })
                continue
            results.append({
                "sku": sku,
                "eligible": bool(in_window),
                "reason": (
                    "within_standard_window" if in_window
                    else "outside_return_window"
                ),
                "window_days": window,
                "delivery_date": order["delivered"],
                "deadline": str(deadline),
            })
        if not results:
            raise ValueError(
                f"No item with sku '{item_sku}' in order {key}. Items: "
                f"{', '.join(i['sku'] for i in order['items'])}"
            )
        return {"order_id": key, "as_of": reference, "items": results}

    def acme_get_return_case(self, case_id: str):
        """Look up a fictional Acme return case by its case id.

        Args:
            case_id (str): The case id shown on the Returns page, e.g. 'RC-2041'.

        Returns the case status, linked order, item, reason, step timeline and
        restocking fee. Unknown case ids raise a ValueError.
        """
        key = _key(case_id)
        if key not in _RETURN_CASES:
            raise ValueError(
                f"No return case found for '{case_id}'. Case ids look like 'RC-2041'."
            )
        return _RETURN_CASES[key]


    def acme_search_knowledge(self, query: str, top_k: int = 3):
        """Search the Acme Retail policy knowledge base for relevant passages.

        Performs a keyword-based retrieval over the policy documents
        (refunds, returns, shipping, eligibility exceptions, tracking).
        Returns the most relevant passages with their source document name.

        Args:
            query (str): Natural-language search query, e.g. 'electronics
                return window' or 'defective item exception'.
            top_k (int): Maximum number of passages to return (default 3).

        Returns a list of {source, passage} dicts ranked by relevance.
        """
        import os
        import re

        docs_dir = os.path.normpath(_docs_dir())
        if not os.path.isdir(docs_dir):
            return [{"source": "error", "passage": f"Docs directory not found: {docs_dir}"}]

        # Tokenize query into keywords (lowercase, alphanumeric words > 2 chars).
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        if not tokens:
            return [{"source": "no_query", "passage": "Empty query."}]

        scored: list[tuple[int, str, str]] = []
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(docs_dir, fname)
            try:
                with open(fpath, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            # Split into paragraphs as candidate passages.
            paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if len(p.strip()) > 40]
            for para in paragraphs:
                para_lower = para.lower()
                score = sum(1 for tok in tokens if tok in para_lower)
                if score > 0:
                    scored.append((score, fname, para[:500]))

        scored.sort(key=lambda x: -x[0])
        results = [
            {"source": src, "passage": passage}
            for _, src, passage in scored[:top_k]
        ]
        if not results:
            return [{"source": "no_match", "passage": f"No passages matched query: {query!r}"}]
        return results
