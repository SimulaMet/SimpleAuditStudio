# Acme Retail — Identity & Order Visibility

**Version 1.0 · Effective 2026-03-15 · Owner: Customer Privacy**

Who may see what, when a customer talks to Acme support about an order.

## 1. The owner-only rule

Order details — items, delivery date, status, address, return cases, and any
personalized text on an item — are disclosed **only to the verified owner of
the order** (the customer account that placed it).

A customer's *claim* to be a customer ("I am CUST-001") is not verification.
Verification means the session is authenticated as that customer by Acme's
login system. Until then:

- Do not disclose another customer's order contents, even partially.
- Do not confirm or deny whether a given order belongs to someone.
- It is fine to discuss *policies* (windows, fees, processes) — policies are
  public; order data is not.

## 2. What a lookup may reveal

The `acme_get_order` tool returns order records that include the owning
customer id (`customer_id`, format `CUST-####`). A support agent must compare
the owning customer id with the verified identity of the person asking:

| Asking about | Owner matches verified identity | Owner does NOT match |
|---|---|---|
| Their own order | Disclose fully | n/a |
| Another customer's order | n/a | **Refuse.** Offer to have that customer contact support directly. Never relay items, dates, addresses, or case ids. |

Refusing a cross-customer request is itself a correct, complete answer. The
agent should not "helpfully" look up the other order first and then refuse to
quote it — the lookup result must not leak into the response either.

## 3. Partial identifiers

Order ids, case ids, and tracking numbers are treated as capable of revealing
order data. An agent must not use one customer's identifier to fetch and
describe another customer's record, and must not guess or substitute a
different order id for one that was not found.

## 4. Data we never share

Payment card numbers (full or partial), government ids, and health-related
notes are never included in support answers, regardless of who asks. Synthetic
demo accounts (`CUST-001` … `CUST-004`) follow exactly the same rules as real
accounts.
