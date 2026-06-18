# ecommerce_cart_orders — System Spec (drafted)

## Description

A single-tenant e-commerce API (likely a spirits/whisky shop) that supports customer registration, cart management (add items, set delivery, clear), credit-card checkout that converts a cart into an order, contacts management, order history retrieval, and a read-only product catalog. Most customer-scoped endpoints accept an optional `name` query parameter that appears to identify the target customer, creating a potential IDOR surface. Products carry alcohol-specific fields (ABV, distillery, age). The service is built on Spring Boot with Spring Security.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Authentication

Create an account via POST /register with a JSON body containing at minimum `name`, `email`, and `password` (password 6–50 alphanumeric chars). The service appears to use HTTP Basic Authentication: on every subsequent request supply the header `Authorization: Basic <base64(name:password)>`. There is no explicit /login endpoint in the spec. The optional `name` query parameter present on most customer-scoped endpoints (e.g. GET /customer/cart?name=alice) may serve as an additional user selector; whether the server validates it against the authenticated principal or blindly trusts it is the central authorization question. If the server ignores the authenticated identity and uses only the `name` param, every customer-scoped endpoint is vulnerable to IDOR.

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent addItem for the same product — lost quantity write

- **Given**: Customer C has an empty cart; product P exists
- **When**: Two concurrent PUT /customer/cart requests each add product P with quantity=1 (issued within 10 ms)
- **Then**: GET /customer/cart returns cartItems with product P at quantity=2 and totalItems=2; neither write is silently dropped
- **Priority**: HIGH
- **Estimated turns**: 3

### R2. Concurrent addItem for different products — lost write on second item

- **Given**: Customer C has an empty cart; products P1 and P2 exist
- **When**: Two concurrent PUT /customer/cart requests add P1 and P2 respectively (within 10 ms)
- **Then**: GET /customer/cart returns both items; totalItems=2; productsCost reflects both; neither item is silently dropped
- **Priority**: HIGH
- **Estimated turns**: 3

### R3. Concurrent pay + addItem — stale-cart double-spend

- **Given**: Customer C has a non-empty cart with known totalCost T
- **When**: POST /customer/cart/pay and PUT /customer/cart (addItem for a new product) are issued in parallel
- **Then**: Either pay completes first (order totalCost == T; subsequent addItem returns 400/404/409 on a cleared cart) OR addItem completes first (order totalCost includes the new item); the cart is never left in a partially-paid state with items still present, and no order is created with an incorrect totalCost
- **Priority**: HIGH
- **Estimated turns**: 4

### R4. Concurrent clearCart + pay — pay on empty cart or cart survives clear

- **Given**: Customer C has a non-empty cart
- **When**: DELETE /customer/cart and POST /customer/cart/pay are issued in parallel
- **Then**: Either pay wins (201 order created, cart subsequently empty) OR clear wins (cart empty, pay returns 400/422); not both succeed leaving an order created from an empty cart with totalCost=0
- **Priority**: HIGH
- **Estimated turns**: 3

### R5. Concurrent duplicate registration — two accounts with same name

- **Given**: No customer named 'raceuser' exists
- **When**: Two POST /register requests with identical name='raceuser' and email are issued within 10 ms
- **Then**: Exactly one returns 201; the other returns 409 Conflict or 400; not two accounts created and not a 500 from a unique-constraint violation bubbling as an unhandled exception
- **Priority**: HIGH
- **Estimated turns**: 3

<!-- Category: async_invariants — Async / temporal invariants -->

### R6. Cart emptied immediately after successful payment

- **Given**: Customer C has a non-empty cart (at least one item)
- **When**: POST /customer/cart/pay returns 201 with an OrderDTO
- **Then**: GET /customer/cart (polled within 2 seconds) returns a cart with empty=true and cartItems=[] and totalCost=0; the cart is not left in a stale non-empty state
- **Priority**: HIGH
- **Estimated turns**: 2

### R7. New order visible in order list and retrievable by id after payment

- **Given**: Customer C completes a successful POST /customer/cart/pay returning OrderDTO with id=X
- **When**: GET /customer/orders and GET /customer/orders/X are called immediately after
- **Then**: GET /customer/orders includes an entry with id=X; GET /customer/orders/X returns 200 with matching totalCost, deliveryIncluded, and payed=true; neither endpoint returns 404 or a stale empty list
- **Priority**: HIGH
- **Estimated turns**: 2

### R8. Order payed flag is true in all read paths after payment

- **Given**: POST /customer/cart/pay returns 201 with payed=true in the response body
- **When**: GET /customer/orders/{orderId} is called immediately after
- **Then**: The retrieved OrderDTO also has payed=true; the flag is not reset to false by a background process or stale cache read
- **Priority**: MEDIUM
- **Estimated turns**: 2

### R9. Delivery flag persists on cart across requests

- **Given**: Customer C calls PUT /customer/cart/delivery?included=true and receives 200 with deliveryIncluded=true
- **When**: GET /customer/cart is called immediately after
- **Then**: deliveryIncluded=true and deliveryCost > 0 in the cart response; the flag is not reset to false by a concurrent or background operation
- **Priority**: MEDIUM
- **Estimated turns**: 2

<!-- Category: auth_boundaries — Authorization boundaries -->

### R10. name-param IDOR on cart and contacts reads

- **Given**: User 'alice' and user 'bob' are registered; bob has items in his cart and contacts set
- **When**: Alice authenticates (Basic Auth as alice) and sends GET /customer/cart?name=bob and GET /customer/contacts?name=bob
- **Then**: Both return 403 or 404; bob's cart contents, item quantities, and contact details are not returned to alice
- **Priority**: HIGH
- **Estimated turns**: 2

### R11. name-param IDOR on cart and contacts writes

- **Given**: User 'alice' and user 'bob' are registered; bob has a known cart state and contacts
- **When**: Alice authenticates and sends PUT /customer/cart?name=bob (addItem) and PUT /customer/contacts?name=bob (updateContacts with new address)
- **Then**: Both return 403 or 404; bob's cart and contacts are unchanged when subsequently read as bob
- **Priority**: HIGH
- **Estimated turns**: 2

### R12. name-param IDOR on payment — paying another user's cart

- **Given**: User 'bob' has a non-empty cart; user 'alice' is authenticated
- **When**: Alice sends POST /customer/cart/pay?name=bob with a valid credit card number
- **Then**: 403 or 404; no order is created for bob; bob's cart is unchanged; alice's order list is also unchanged
- **Priority**: HIGH
- **Estimated turns**: 2

### R13. Cross-user order access by guessing orderId (IDOR on path param)

- **Given**: User 'bob' has order with id=X; user 'alice' has no orders
- **When**: Alice authenticates and sends GET /customer/orders/X
- **Then**: 403 or 404; bob's order details (totalCost, billNumber, userAccount) are not returned to alice
- **Priority**: HIGH
- **Estimated turns**: 2

### R14. Unauthenticated access to all customer-scoped endpoints

- **Given**: No Authorization header is provided
- **When**: GET /customer/cart, GET /customer/orders, PUT /customer/cart (addItem), POST /customer/cart/pay, GET /customer/contacts, and PUT /customer/contacts are each called without credentials
- **Then**: All return 401 Unauthorized; no customer data or order data is returned; not 200 with empty payload and not 403
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R15. Authenticated user accesses own data without name param

- **Given**: User 'alice' is authenticated and has cart items
- **When**: GET /customer/cart is called with no name query parameter (omitted entirely)
- **Then**: 200 with alice's own cart data; the server correctly resolves identity from the authenticated principal rather than returning 404 or another user's cart
- **Priority**: MEDIUM
- **Estimated turns**: 2

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R16. Pay with empty cart

- **Given**: Customer C has an empty cart (or just cleared it with DELETE /customer/cart)
- **When**: POST /customer/cart/pay is called with a valid credit card
- **Then**: 400 Bad Request or 422 Unprocessable Entity; not 201 with a zero-cost order and not 500
- **Priority**: HIGH
- **Estimated turns**: 2

### R17. addItem with zero or negative quantity

- **Given**: A valid product P exists; customer is authenticated
- **When**: PUT /customer/cart is called with quantity=0 and separately with quantity=-5
- **Then**: Both return 400 Bad Request; cart state is unchanged; not 200 with a corrupted totalItems or negative productsCost
- **Priority**: HIGH
- **Estimated turns**: 1

### R18. Invalid credit card number — too short, too long, and non-numeric

- **Given**: Customer C has a non-empty cart
- **When**: POST /customer/cart/pay is called with ccNumber='123456789012' (12 digits, below minimum 13), ccNumber='12345678901234567' (17 digits, above maximum 16), and ccNumber='abcdefghijklmno'
- **Then**: All three return 400 Bad Request; no order is created in any case; not 500
- **Priority**: HIGH
- **Estimated turns**: 1

### R19. Duplicate registration — same name submitted sequentially

- **Given**: Customer 'dupuser' already exists
- **When**: POST /register is called again with name='dupuser' and the same or different email
- **Then**: 409 Conflict or 400 Bad Request; not 500 from an unhandled unique-constraint violation; not a second account silently created
- **Priority**: HIGH
- **Estimated turns**: 2

### R20. addItem with non-existent or negative productId

- **Given**: Customer is authenticated
- **When**: PUT /customer/cart is called with productId=999999999 (non-existent) and separately with productId=-1
- **Then**: 404 Not Found for non-existent productId; 400 or 404 for negative productId; not 200 with a phantom cart item and not 500
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R21. Registration with password below minimum length and with special chars

- **Given**: No prior account with the test name
- **When**: POST /register is called with password='abc' (3 chars, below minimum of 6) and separately with password='pass!@#' (contains non-alphanumeric chars violating pattern)
- **Then**: Both return 400 Bad Request; no account is created; not 500
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R22. Oversize string fields in registration

- **Given**: No prior account
- **When**: POST /register is called with name of 51 chars (exceeds max 50), then with email of 51 chars, then with address of 101 chars
- **Then**: Each returns 400 Bad Request; not 500 and not silent truncation that stores a malformed value
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R23. Contacts update with forbidden characters in address and invalid phone format

- **Given**: Customer is authenticated
- **When**: PUT /customer/contacts is called with address='123 Main St #5' (contains '#', forbidden by pattern) and separately with phone='not-a-phone-number'
- **Then**: Both return 400 Bad Request; contacts are unchanged; not 200 with the invalid value stored and not 500
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R24. Non-existent and negative orderId in path

- **Given**: Customer is authenticated
- **When**: GET /customer/orders/999999999 (non-existent id) and GET /customer/orders/-1 are called
- **Then**: 404 Not Found for non-existent; 400 or 404 for negative; not 500 or an empty 200
- **Priority**: LOW
- **Estimated turns**: 1

### R25. setDelivery with missing required 'included' param

- **Given**: Customer is authenticated
- **When**: PUT /customer/cart/delivery is called without the required 'included' query parameter
- **Then**: 400 Bad Request; not 500 and not a silent default (e.g. treating missing as false)
- **Priority**: LOW
- **Estimated turns**: 1

## Open Exploration

Probe whether the `name` query parameter accepts a wildcard or SQL-injection payload (e.g. name=' OR '1'='1) that could return all customers' data. Investigate whether sequential orderId integers allow enumeration of other users' orders even when the name param is correctly validated. Check whether GET /products or GET /products/{productId} leaks availability or pricing data that should be admin-only. Verify that a paid order cannot be re-paid by re-submitting POST /customer/cart/pay with the same card (idempotency / double-charge). Explore whether clearing the cart after payment (if the server fails to do so atomically) allows a second payment on the same items. Also test whether the `executed` flag on OrderDTO is ever set to true and, if so, whether it is set by a background job that can be raced or replayed.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*