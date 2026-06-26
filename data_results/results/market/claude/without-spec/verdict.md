## Black-Box QA Report — Summary

**Verdict: FAIL** — 8 bugs confirmed across 38+ verified behaviors.

---

### Bugs Found (8)

| #      | Bug                                                                                                              | Severity     |
| ------ | ---------------------------------------------------------------------------------------------------------------- | ------------ |
| BUG-01 | `totalCost` doesn't update when `deliveryIncluded=false` — delivery always added to total                        | **High**     |
| BUG-02 | `totalItems` counts distinct product lines (2), not total quantity (e.g., 8 for qty=5+3)                         | Medium       |
| BUG-03 | Adding `available=false` product returns `200 OK` with unchanged cart (silent failure)                           | Medium       |
| BUG-04 | **Authorization breach**: any authenticated user can read any order by ID (Bob reads Alice's order)              | **Critical** |
| BUG-05 | `dateCreated` time truncated to midnight in `GET /customer/orders` and `GET /customer/orders/{id}`               | High         |
| BUG-06 | HTTP **500** returned for client-input errors (missing body, malformed JSON, wrong type, missing required param) | High         |
| BUG-07 | Input validation errors use HTTP **406 Not Acceptable** instead of 400/422 (semantically wrong per RFC 7231)     | Medium       |
| BUG-08 | Duplicate registration returns **406** instead of **409 Conflict**                                               | Low          |

---

### Key Correct Behaviors Verified (38 tests)

- Product listing/retrieval, auth enforcement (401), method-not-allowed (405)
- Registration field validation (email pattern, phone pattern, password length/chars, address chars)
- Cart operations: add/clear/delivery flag, qty validation, nonexistent product → 404
- `productsCost` calculation is correct; `totalCost` correct when delivery is ON
- Payment: CC validation enforced, empty-cart rejected, `payed=true` after payment, cart cleared
- Order listing/retrieval and 404 for nonexistent orders

The full report is saved to `results/market_qa_report.md`.
[usage] in=17 out=38,840 cost=$0.0000 wall=552.9s
