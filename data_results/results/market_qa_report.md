# Market API — Black-Box QA Report

**Target:** `http://localhost:8080/`  
**Spec:** `market.json` (Swagger 2.0)  
**Date:** 2026-06-18  
**Method:** Black-box HTTP testing only (curl + Python requests)

---

## 1. Bugs Found

### BUG-01 — `totalCost` is wrong when `deliveryIncluded=false`

**Description:** After setting delivery to `false`, `totalCost` still includes the delivery cost instead of equalling `productsCost` alone.

**Exact request:**
```
PUT http://localhost:8080/customer/cart/delivery?name=Delivery%20Bug&included=false
Authorization: Basic ZGVsYnVnQHRlc3QuY29tOnBhc3MxMjM=
```

**Exact response (HTTP 200):**
```json
{
  "productsCost": 7020.0,
  "deliveryCost": 400,
  "deliveryIncluded": false,
  "totalCost": 7420.0
}
```

**Spec violation:** `deliveryIncluded=false` yet `totalCost (7420) = productsCost (7020) + deliveryCost (400)`. When delivery is not included, `totalCost` should equal `productsCost` (7020.0). The delivery cost of 400 is being added regardless of the flag.

---

### BUG-02 — `totalItems` counts distinct product lines, not total quantity

**Description:** `totalItems` in `CartDTO` equals the number of distinct product line entries, not the sum of all item quantities.

**Exact request:**
```
GET http://localhost:8080/customer/cart?name=Items%20Counter
Authorization: Basic aXRlbXNjQHRlc3QuY29tOnBhc3MxMjM=
```
(After adding productId=2 qty=5 and productId=3 qty=3)

**Exact response (HTTP 200, excerpt):**
```json
{
  "cartItems": [{"productId": 2, "quantity": 5}, {"productId": 3, "quantity": 3}],
  "totalItems": 2
}
```

**Spec violation:** `totalItems` is 2 (line count) instead of 8 (5+3, sum of quantities). The field name `totalItems` strongly implies total count of individual items across all lines.

---

### BUG-03 — Adding an unavailable product returns 200 OK but silently does nothing

**Description:** Sending `PUT /customer/cart` with a product whose `available=false` returns HTTP 200 with the unchanged cart. The item is not added and no error is reported.

**Exact request:**
```
PUT http://localhost:8080/customer/cart?name=Avail%20Tester
Authorization: Basic YXZhaWxAdGVzdC5jb206cGFzczEyMw==
Content-Type: application/json

{"productId": 1, "quantity": 1}
```
(Product 1 has `available: false`)

**Exact response (HTTP 200):**
```json
{
  "totalItems": 0,
  "productsCost": 0.0,
  "cartItems": [],
  "empty": true
}
```

**Spec violation:** The client receives a success response (200 OK) but the action (adding an item) did not occur. This is a silent failure. The service should return a 400/422 error indicating the product is unavailable for purchase.

---

### BUG-04 — Any authenticated user can access any order by ID (Authorization breach)

**Description:** `GET /customer/orders/{orderId}` returns the order regardless of whether it belongs to the authenticated user. Bob (auth: `bob@example.com`) can retrieve Alice's order simply by knowing its numeric ID.

**Exact request:**
```
GET http://localhost:8080/customer/orders/2?name=Bob%20Jones
Authorization: Basic Ym9iQGV4YW1wbGUuY29tOnNlY3JldDk5
```
(Order ID 2 belongs to `alice@example.com`)

**Exact response (HTTP 200):**
```json
{
  "userAccount": "alice@example.com",
  "id": 2,
  "billNumber": 112528405,
  "totalCost": 12823.0,
  "payed": true
}
```

**Spec violation:** The service returns 200 and Alice's full order data to Bob, an unauthorized user. The expected behavior is 403 Forbidden or 404 Not Found when a user attempts to access another user's order.

---

### BUG-05 — `dateCreated` time-of-day is truncated to midnight in order retrieval

**Description:** The payment confirmation (`POST /customer/cart/pay`) returns an accurate timestamp for `dateCreated`, but both `GET /customer/orders` and `GET /customer/orders/{id}` return the same order with the time portion zeroed out (midnight).

**Exact requests:**
```
POST http://localhost:8080/customer/cart/pay?name=Date%20Tester
→ Response dateCreated: "2026-06-18T22:54:15.156+00:00"

GET http://localhost:8080/customer/orders?name=Date%20Tester
→ Response dateCreated: "2026-06-18T00:00:00.000+00:00"

GET http://localhost:8080/customer/orders/{id}?name=Date%20Tester
→ Response dateCreated: "2026-06-18T00:00:00.000+00:00"
```

**Spec violation:** `OrderDTO.dateCreated` is typed as `string / format: date-time`. The payment endpoint preserves the full timestamp; both order-retrieval endpoints drop the time portion. This is data loss — the order time cannot be recovered from the GET endpoints.

---

### BUG-06 — HTTP 500 returned for malformed/missing client inputs (should be 4xx)

**Description:** Multiple endpoints return `500 Internal Server Error` for inputs that represent client errors and should produce a `400 Bad Request`.

| Request | Actual | Expected |
|---------|--------|----------|
| `POST /register` with no body | 500 | 400 |
| `POST /register` with malformed JSON (`not-json`) | 500 | 400 |
| `PUT /customer/cart/delivery` without required `included` param | 500 | 400 |
| `PUT /customer/cart` with `"quantity": "abc"` (string for int) | 500 | 400 |
| `GET /products/abc` (non-numeric path param) | 500 | 400 |

**Example — missing body:**
```
POST http://localhost:8080/register
Content-Type: application/json
(no body)

HTTP/1.1 500
{"message":"Required request body is missing: ...","description":"uri=/register"}
```

**Spec violation:** 500 indicates an unhandled server error. These are all client-side mistakes (missing/malformed input) that should be caught and returned as `400 Bad Request` without leaking internal stack/class details.

---

### BUG-07 — Input validation errors use HTTP 406 instead of 400/422

**Description:** All field-level validation failures (invalid patterns, length violations, business rules) return `406 Not Acceptable` rather than `400 Bad Request` or `422 Unprocessable Entity`.

**Example request:**
```
POST http://localhost:8080/register
Content-Type: application/json

{"name": "Test", "email": "not-an-email", "password": "p"}
```

**Exact response (HTTP 406):**
```json
{
  "message": "Argument validation error",
  "description": "uri=/register",
  "entityName": "userDTO",
  "fieldErrors": [
    {"field": "email", "message": "The value shall be a valid email address"},
    {"field": "password", "message": "Length shall be between 6 and 50 characters"}
  ]
}
```

**Spec violation:** RFC 7231 defines `406 Not Acceptable` exclusively for content-negotiation failures (when the server cannot produce a response matching the client's `Accept` headers). Using it for input validation is semantically incorrect. `400 Bad Request` (general client error) or `422 Unprocessable Entity` (semantically invalid request body) are the correct codes.

---

### BUG-08 — Duplicate registration returns 406 instead of 409 Conflict

**Description:** Attempting to register with an already-taken email address returns `406 Not Acceptable` instead of `409 Conflict`.

**Exact request:**
```
POST http://localhost:8080/register
Content-Type: application/json

{"name":"Alice Smith","email":"alice@example.com","password":"password123",...}
```
(alice@example.com already registered)

**Exact response (HTTP 406):**
```json
{
  "message": "Argument validation error",
  "entityName": "UserAccount",
  "fieldErrors": [{"field": "email", "message": "Account with this email already exists"}]
}
```

**Spec violation:** A duplicate resource conflict should return `409 Conflict` (RFC 7231 §6.5.8). `406` is semantically wrong for this scenario.

---

## 2. Correct Behaviors Verified

| # | What was tested | Observed result |
|---|----------------|-----------------|
| 1 | `GET /products` — no auth required | 200 OK, array of 11 products |
| 2 | `GET /products/{id}` — valid ID | 200 OK with product data |
| 3 | `GET /products/{id}` — nonexistent ID (99999) | 404 Not Found |
| 4 | `POST /register` — valid payload | 201 Created with user data; password masked as `"hidden"` |
| 5 | `POST /register` — invalid email pattern | 406 (validation rejected) |
| 6 | `POST /register` — invalid phone pattern | 406 (validation rejected) |
| 7 | `POST /register` — password < 6 chars | 406 (validation rejected) |
| 8 | `POST /register` — address with `#` char | 406 (validation rejected) |
| 9 | `POST /register` — name with `#` char | 406 (validation rejected) |
| 10 | `GET /customer` — with valid auth | 200 OK, password field masked as `"hidden"` |
| 11 | `GET /customer` — without auth | 401 Unauthorized |
| 12 | `GET /customer/cart` — authenticated | 200 OK |
| 13 | `PUT /customer/cart` — add available product | 200 OK, item appears in cart |
| 14 | `PUT /customer/cart` — nonexistent productId | 404 Not Found |
| 15 | `PUT /customer/cart` — quantity = 0 | 406 validation rejected |
| 16 | `PUT /customer/cart` — quantity = -5 | 406 validation rejected |
| 17 | `PUT /customer/cart/delivery?included=true` | 200 OK, `deliveryIncluded=true` |
| 18 | `PUT /customer/cart/delivery?included=false` | 200 OK, `deliveryIncluded=false` |
| 19 | `productsCost` calculation | Correctly equals sum of `price × quantity` for each item |
| 20 | `totalCost` when `deliveryIncluded=true` | Correctly equals `productsCost + deliveryCost` |
| 21 | `GET /customer/contacts` | 200 OK |
| 22 | `PUT /customer/contacts` — valid data | 200 OK, data updated |
| 23 | `PUT /customer/contacts` — invalid phone | 406 rejected |
| 24 | `PUT /customer/contacts` — address with `#` | 406 rejected |
| 25 | `POST /customer/cart/pay` — valid 16-digit CC | 201 Created with `OrderDTO`, `payed=true` |
| 26 | `POST /customer/cart/pay` — empty cart | 406 rejected ("cart is empty") |
| 27 | `POST /customer/cart/pay` — 4-digit CC | 406 rejected ("Not a valid credit card number") |
| 28 | `POST /customer/cart/pay` — CC with letters | 406 rejected |
| 29 | Cart cleared after successful payment | `empty=true`, `cartItems=[]` after pay |
| 30 | `GET /customer/orders` — authenticated | 200 OK, array of orders |
| 31 | `GET /customer/orders/{id}` — valid ID | 200 OK |
| 32 | `GET /customer/orders/99999` — nonexistent | 404 Not Found |
| 33 | `DELETE /customer/cart` | 200 OK, cart is empty afterwards |
| 34 | `POST /products` | 405 Method Not Allowed |
| 35 | `DELETE /products` | 405 Method Not Allowed |
| 36 | `GET /register` | 405 Method Not Allowed |
| 37 | `PUT /customer/orders/{id}` | 405 Method Not Allowed |
| 38 | `GET /customer/orders` without auth | 401 Unauthorized |

---

## 3. Overall Verdict: **FAIL**

The service correctly implements the core happy-path flows (registration, product browsing, cart management, payment, order retrieval) and enforces most input validation rules. However, it has **8 confirmed bugs**, two of which are serious:

**Critical:** BUG-04 is a broken authorization control — any authenticated user can read any other user's order by guessing or knowing the numeric order ID. BUG-01 causes an incorrect financial total to be displayed (and presumably charged) whenever a user turns off delivery, because the delivery cost is still added to `totalCost`. BUG-05 (timestamp truncation) means all order retrieval times are silently wrong (midnight instead of actual time). BUG-06 (HTTP 500 on client errors) leaks internal server details and signals unhandled exceptions for ordinary bad input.

The remaining bugs (BUG-02 `totalItems` semantics, BUG-03 silent unavailable-product ignore, BUG-07/08 wrong HTTP status codes) are lower severity but indicate systemic issues with response correctness and HTTP semantics throughout the service.
