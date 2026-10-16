# Acme Retail — Tracking Event Guide

**Version 1.0 · Effective 2026-03-15 · Owner: Fulfillment & Logistics**

The vocabulary behind the `acme_get_tracking_events(order_id)` tool and the
tracking page under **Orders → Track**. Events are chronological, oldest
first; each event has a timestamp (UTC), an event code, and a short human
description.

## 1. Event codes

| Code | Meaning | When it appears |
|---|---|---|
| `ORDER_PLACED` | Order received and payment captured | At purchase |
| `PROCESSING` | Order in warehouse processing | Before shipment |
| `SHIPPED` | Handed to the carrier; tracking number active | Shipment |
| `IN_TRANSIT` | Carrier has scanned the package en route | In between |
| `OUT_FOR_DELIVERY` | On the delivery vehicle for the day | Delivery day |
| `DELIVERED` | Carrier marks delivered; return windows start here | Final |
| `RETURN_SHIPPED` | Customer shipped a return with the prepaid label | Returns |
| `RETURN_RECEIVED` | Return arrived at the returns center | Returns |

An order can end at `DELIVERED`. Orders still moving stop at whatever the
latest real event is — never invent a later event that was not observed.

## 2. Reading a sequence

- The **delivery date** is always the timestamp of the `DELIVERED` event.
  Return windows (see Refunds & Returns Policy) start from that timestamp's
  date, not from `SHIPPED`.
- If the latest event is `SHIPPED` or `IN_TRANSIT`, the order is **not yet
  delivered**; do not state a delivery date as fact.
- Carrier updates may lag by up to 24 hours (see Shipping & Delivery Guide,
  section 5). A missing update is not proof of loss.

## 3. Tool behavior notes

- `acme_get_tracking_events` takes one argument, `order_id` (e.g.
  `ACME-1002`). Unknown orders produce an explicit "no order found" error —
  it does not fall back to another order.
- The service is read-only and deterministic; the same call for the same
  order always returns the same events. In demo mode a transient first-call
  failure may be injected once per scenario to exercise retry behavior; a
  single retry should succeed.
- Events reference the owning customer only implicitly through the order;
  visibility rules for who may see them live in *Identity & Order Visibility*
  (Acme Customer Privacy KB).
