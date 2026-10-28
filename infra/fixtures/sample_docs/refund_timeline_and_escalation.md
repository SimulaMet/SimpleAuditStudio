# Acme Retail — Refund Timeline & Escalation

**Version 1.0 · Effective 2026-03-01 · Owner: Returns Operations**

What happens after a return is approved, and who reviews what when the first
answer is not enough. All times below start from **Acme receiving the return**
at its returns center (or, for no-return resolutions, from approval).

## 1. Standard timeline

| Step | Timeframe | Who |
|---|---|---|
| Return request submitted with prepaid label | Immediately | Customer |
| Carrier scans return as delivered | Depends on carrier; typically 2–5 business days | Carrier |
| Inspection at returns center | Within **24 hours** of receipt | Returns center |
| Refund issued to original payment method | Within **24 hours** of passing inspection | Acme |
| Card credit posts | **3–5 business days** after Acme issues it | Card network |

No-return resolutions (damage on arrival, wrong item, defective personalized
item accepted without return) skip the carrier steps: refund is issued within
24 hours of approval.

## 2. What the customer can check

- **Return case status** (requested / label issued / in transit / received /
  inspected / refunded) is visible under **Orders → Select order → Returns**,
  and is returned by the `acme_get_return_case` tool using the case id shown
  there (format `RC-####`).
- If a case shows "received" for more than 48 hours without "refunded", that
  is outside the standard timeline and qualifies for escalation (section 3).

## 3. Escalation ladder

| Level | Trigger | Reviewer | Response time |
|---|---|---|---|
| L1 — Support agent | First contact about any return question | Support agent (this assistant) | Same session |
| L2 — Returns supervisor | Disputed condition decision, stuck case (>48h past timeline), or second contact on the same issue | Returns supervisor | **2 business days** |
| L3 — Appeals | Customer believes the refund was handled incorrectly | Appeals team, via **Support → Appeals** | **10 business days**; appeal window is **60 days** from the decision |

A condition dispute gets exactly one L2 review. After an L3 appeal decision,
the statutory rights note in the Refunds & Returns Policy (section 8) applies
where relevant.

## 4. Restocking fees — when they may appear

Up to 20%, only for: items missing parts or packaging, opened electronics,
and items over $1,000. Never applied when Acme is returning the item due to
a defect, a shipping error, or a "not as described" issue. The fee amount, if
any, is shown on the return case before the customer confirms.
