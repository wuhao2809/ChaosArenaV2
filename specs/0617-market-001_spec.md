# ecommerce_cart_orders — System Spec (drafted)

## Description

A Spring-based e-commerce API for a spirits retailer. Customers register via POST /register and can manage a personal shopping cart (add items, toggle delivery, clear), pay by credit card to create an order, update contact details, and browse a product catalog. Orders carry payed and executed status flags. All customer-scoped endpoints accept an optional ?name query parameter that appears to identify the acting customer, creating a significant authorization surface.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Authentication

No explicit login endpoint is documented. Registration is performed via POST /register with a JSON body containing name, email, password, phone, and address fields (UserDTOReq). Authentication on subsequent requests is almost certainly HTTP Basic Auth (standard Spring Security default): encode credentials as Base64(name:password) and send the header 'Authorization: Basic <encoded>' on every protected request. The optional ?name query parameter present on all customer-scoped endpoints (GET /customer, GET/PUT/DELETE /customer/cart, PUT /customer/cart/delivery, POST /customer/cart/pay, GET/PUT /customer/contacts, GET /customer/orders, GET /customer/orders/{orderId}) appears to identify the target customer; whether the server validates this parameter against the authenticated principal is a primary security concern and must be probed in every auth-boundary test.

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Double payment from same cart

- **Given**: Customer A is authenticated and has a non-empty cart
- **When**: Two concurrent POST /customer/cart/pay requests are issued with a valid CreditCardDTO within 10ms of each other
- **Then**: Exactly one request returns 201 with an OrderDTO; the second returns 409 Conflict or 400 Bad Request (empty cart); only one order is created in GET /customer/orders; the cart is empty after the first success
- **Priority**: HIGH
- **Estimated turns**: 4

### R2. Concurrent addItem for the same product

- **Given**: Customer A has an empty cart and a valid productId P exists
- **When**: Two concurrent PUT /customer/cart requests each add productId P with quantity=1 within 10ms
- **Then**: GET /customer/cart shows totalItems == 2 (or the product appears once with quantity 2, depending on design); no lost write; productsCost and totalCost are consistent with the actual quantity stored
- **Priority**: HIGH
- **Estimated turns**: 3

### R3. AddItem racing with clearCart

- **Given**: Customer A has items in the cart
- **When**: PUT /customer/cart (addItem) and DELETE /customer/cart (clearCart) are issued concurrently within 10ms
- **Then**: Cart ends in a consistent state: either fully cleared (empty==true, totalItems==0) or contains exactly the newly added item; no partial state where totalItems is non-zero but cartItems is empty, and no 5xx errors
- **Priority**: HIGH
- **Estimated turns**: 3

### R4. Concurrent duplicate customer registration

- **Given**: No customer with username 'testuser' exists
- **When**: Two concurrent POST /register requests with identical name='testuser' and password are issued within 10ms
- **Then**: Exactly one returns 201; the other returns 409 Conflict or 400 Bad Request; only one customer record exists (GET /customer?name=testuser returns a single result)
- **Priority**: HIGH
- **Estimated turns**: 3

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. Cart emptied immediately after successful payment

- **Given**: Customer A has a non-empty cart and POST /customer/cart/pay returns 201 with an OrderDTO
- **When**: GET /customer/cart is called in the turn immediately following the 201 response
- **Then**: GET /customer/cart immediately after (within the same logical session, no delay) returns a cart with empty==true and totalItems==0; the paid items are not still present in the cart
- **Priority**: HIGH
- **Estimated turns**: 2

### R6. Order payed flag is true immediately after payment response

- **Given**: POST /customer/cart/pay returns 201 with an OrderDTO containing an id
- **When**: GET /customer/orders/{orderId} is called immediately after
- **Then**: The returned OrderDTO has payed==true; the order is visible in GET /customer/orders; totalCost and deliveryCost match the cart state at payment time
- **Priority**: MEDIUM
- **Estimated turns**: 2

### R7. Delivery flag change reflected in subsequent pay total

- **Given**: Customer A has items in the cart; PUT /customer/cart/delivery?included=true returns 200 with deliveryCost > 0
- **When**: PUT /customer/cart/delivery?included=false is called, then POST /customer/cart/pay is called immediately after
- **Then**: The resulting OrderDTO has deliveryIncluded==false and deliveryCost==0; the totalCost does not include the delivery surcharge; no stale delivery state is baked into the order
- **Priority**: HIGH
- **Estimated turns**: 2

<!-- Category: auth_boundaries — Authorization boundaries -->

### R8. IDOR via name param — cart and contacts read

- **Given**: Customer A and Customer B are both registered; Customer B has items in the cart and contact details set
- **When**: Customer A authenticates (Basic Auth as A) and calls GET /customer/cart?name=B and GET /customer/contacts?name=B
- **Then**: Both requests return 403 Forbidden (or 404); Customer B's cart contents and contact details are not returned to Customer A
- **Priority**: HIGH
- **Estimated turns**: 2

### R9. IDOR via name param — cross-customer payment

- **Given**: Customer B has a non-empty cart
- **When**: Customer A authenticates and calls POST /customer/cart/pay?name=B with a valid CreditCardDTO
- **Then**: Response is 403 Forbidden; no order is created on Customer B's account; Customer B's cart is unchanged
- **Priority**: HIGH
- **Estimated turns**: 2

### R10. IDOR on order by sequential ID

- **Given**: Customer B has placed an order with orderId O
- **When**: Customer A authenticates and calls GET /customer/orders/O (without name param, relying solely on the path ID)
- **Then**: Response is 403 Forbidden or 404 Not Found; no order details belonging to Customer B are returned
- **Priority**: HIGH
- **Estimated turns**: 2

### R11. IDOR via name param — addItem to another customer's cart

- **Given**: Customer B has an empty cart
- **When**: Customer A authenticates and calls PUT /customer/cart?name=B with a valid CartItemDTOReq
- **Then**: Response is 403 Forbidden; Customer B's cart remains empty; no item is added on behalf of Customer B
- **Priority**: HIGH
- **Estimated turns**: 2

### R12. Anonymous access to protected customer endpoints

- **Given**: No Authorization header is sent
- **When**: Unauthenticated requests are made to GET /customer/cart, POST /customer/cart/pay, GET /customer/orders, and PUT /customer/contacts
- **Then**: All return 401 Unauthorized; no customer data or order data is returned
- **Priority**: MEDIUM
- **Estimated turns**: 1

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R13. Zero or negative quantity in cart addItem

- **Given**: Customer A is authenticated and a valid productId exists
- **When**: PUT /customer/cart is called with quantity=0 and separately with quantity=-1
- **Then**: Both return 400 Bad Request; neither modifies the cart; response is not 500 or a silent no-op that corrupts totalItems
- **Priority**: HIGH
- **Estimated turns**: 1

### R14. Non-existent productId in cart addItem

- **Given**: Customer A is authenticated
- **When**: PUT /customer/cart is called with productId=999999999 (does not exist)
- **Then**: Response is 404 Not Found or 400 Bad Request; not 500; cart is unchanged
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R15. Payment with empty cart

- **Given**: Customer A is authenticated and the cart is empty (or has just been cleared)
- **When**: POST /customer/cart/pay is called with a valid CreditCardDTO
- **Then**: Response is 400 Bad Request or 409 Conflict; not 201 with a zero-cost order and not 500
- **Priority**: HIGH
- **Estimated turns**: 1

### R16. Invalid credit card number — too short or too long

- **Given**: Customer A has a non-empty cart
- **When**: POST /customer/cart/pay is called with ccNumber='123456789012' (12 digits, below the 13-digit minimum) and separately with ccNumber='12345678901234567' (17 digits, above the 16-digit maximum)
- **Then**: Both return 400 Bad Request; no order is created; not 500
- **Priority**: HIGH
- **Estimated turns**: 1

### R17. Duplicate username registration

- **Given**: A customer with name='existinguser' is already registered
- **When**: POST /register is called again with name='existinguser' and any valid password
- **Then**: Response is 409 Conflict or 400 Bad Request; not 201 and not 500; only one customer record exists
- **Priority**: HIGH
- **Estimated turns**: 1

### R18. Registration field constraint violations

- **Given**: POST /register endpoint is available
- **When**: Separate requests are made with: (a) password='pass!@#' (special chars violating ^[a-zA-Z0-9]+$), (b) email='notanemail' (invalid format), (c) address containing '#' (forbidden by pattern), (d) name of 51 characters (exceeds maxLength 50)
- **Then**: All return 400 Bad Request with a descriptive error; none return 500 or silently create a customer record with invalid data
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R19. Negative or zero path parameters

- **Given**: Customer A is authenticated
- **When**: GET /customer/orders/-1 and GET /products/-1 and GET /products/0 are called
- **Then**: All return 400 Bad Request or 404 Not Found; none return 500 or expose a stack trace
- **Priority**: LOW
- **Estimated turns**: 1

### R20. Oversize address field in contacts update

- **Given**: Customer A is authenticated
- **When**: PUT /customer/contacts is called with an address string of 101 characters (exceeds maxLength 100)
- **Then**: Response is 400 Bad Request or 413; not 500 and not silent truncation that stores a corrupted address
- **Priority**: LOW
- **Estimated turns**: 1

## Open Exploration

Probe whether the ?name query parameter is entirely ignored by the server (meaning auth context alone determines the customer) or whether it overrides the auth context — the latter is a critical IDOR. Investigate whether orderId and productId values are sequential integers that enable enumeration across customers. Check whether GET /customer/orders returns orders from all customers when called by an admin-level credential vs. a regular customer. Explore whether a customer can add a product marked available==false to the cart, and whether paying for such a cart produces a 500 or a clean 400. Test whether the ccNumber pattern can be bypassed by sending a non-string type (e.g., integer) in the JSON body. Finally, verify that error responses never include stack traces, internal class names, or SQL fragments that would aid further exploitation.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*