# Acme Retail — Return Exception Matrix

**Version 1.0 · Effective 2026-03-01 · Owner: Returns Operations**

This matrix defines how Acme Retail handles returns that fall outside the
standard 30-day window in the Refunds & Returns Policy (v3.2). When a rule
here conflicts with the general policy, this matrix applies **only** to the
row it names; all other cases follow the standard policy. Frozen evaluation
clock for demo data: **as_of_date 2026-10-14**.

## 1. Damaged on arrival

| Condition | Window | Outcome |
|---|---|---|
| Item arrived damaged or a box was missing items | Report within **2 days of delivery** with photos of packaging and item | Replacement shipped, or refund issued for the affected items, without shipping the item back (unless high value) |

Report from **Orders → Select order → Report issue**. No restocking fee ever
applies to damage-on-arrival claims.

## 2. Defective items

A defect is a manufacturing fault or an item that does not match its listing
that the customer did not cause.

| Condition | Window | Outcome |
|---|---|---|
| Defective non-personalized item | Within the item's standard return window (30 days, or 15 days for electronics) | Full refund or replacement; no restocking fee; no return shipping charge |
| Defective item reported after the standard window | Up to **90 days from delivery**, verified by Acme support | Refund or replacement at Acme's discretion; no restocking fee |
| **Defective personalized or monogrammed item** | Within **30 days of delivery** | **Exception:** returned for a full refund or replacement despite the "personalized = non-returnable" rule, provided photos/video of the defect are submitted with the request |

The defective-personalized-item exception is the single case where a
personalized or monogrammed item is returnable. A preference change ("I
changed my mind") on a personalized item is **not** a defect and remains
non-returnable.

## 3. Personalized and monogrammed items

Non-defective personalized items are final sale. The only path back is the
defect exception in section 2. Exchange for a differently personalized item
is handled as: refund of the defective original + a new purchase at current
price.

## 4. Electronics and devices

Electronics keep the **15-day** refund window from the Refunds & Returns
Policy (never the blanket 30 days), must include all original accessories,
cables, and packaging, and may carry a restocking fee of up to 20% if opened
or missing parts. A defective electronic device is covered by section 2
instead, including after day 15 (up to 90 days).

## 5. Wrong item / not as described (Acme error)

If Acme shipped the wrong item or one that does not match its listing: the
customer keeps or ships back the item at their choice, gets a full refund to
the original method, pays no return shipping, and no restocking fee applies.
No fixed window — report as soon as practicable.

## 6. Resolution and escalation

Every row above resolves through the same channel: **Orders → Return or
exchange**, reason selected as shown. Escalation paths, timelines, and who
reviews what are defined in *Refund Timeline & Escalation* (same knowledge
base, v1.0).
