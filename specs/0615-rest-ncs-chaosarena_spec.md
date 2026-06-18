# ncs_numerical_algorithms — System Spec (drafted)

## Description

A stateless REST API (Numerical Case Study) exposing six numerical algorithm endpoints: Bessel function of the first kind (bessj), exponential integral (expint), Fisher F-distribution (fisher), incomplete gamma function (gammq), integer remainder, and triangle-type classification. All endpoints are GET-only with path parameters; the Swagger spec lists 401 and 403 as possible responses on every endpoint despite describing no authentication mechanism. The Dto response carries either a double or an integer result field.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent iterative-algorithm calls share no mutable state

- **Given**: The server is running and /api/bessj/5/1.5 has a known correct result (approximately 0.1321)
- **When**: 50 concurrent GET /api/bessj/5/1.5 requests are issued simultaneously
- **Then**: All 50 responses return HTTP 200 with resultAsDouble equal to the same correct value (within floating-point tolerance); no response returns a corrupted, zero, or wildly different value that would indicate a shared mutable accumulator or static variable being overwritten mid-computation
- **Priority**: HIGH
- **Estimated turns**: 3

### R2. Concurrent calls with different inputs do not cross-contaminate results

- **Given**: The server is running; /api/expint/2/1.0 and /api/expint/3/2.0 have distinct known correct results
- **When**: 25 requests for /api/expint/2/1.0 and 25 requests for /api/expint/3/2.0 are interleaved concurrently
- **Then**: Every response for n=2,x=1.0 returns the correct value for those parameters and every response for n=3,x=2.0 returns the correct value for those parameters; no response returns the result belonging to the other parameter set, which would indicate a singleton bean with shared iteration state
- **Priority**: HIGH
- **Estimated turns**: 3

<!-- Category: async_invariants — Async / temporal invariants -->

*Category async_invariants marked N/A by drafter: All six endpoints are synchronous GET computations with no queues, background workers, retries, webhooks, or schedulers described or implied.*

<!-- Category: auth_boundaries — Authorization boundaries -->

### R3. Unauthenticated access versus advertised 401

- **Given**: Every endpoint in the Swagger spec lists 401 Unauthorized as a possible response, yet no security definition or authentication scheme is declared in the spec
- **When**: An unauthenticated client (no Authorization header, no session cookie) calls GET /api/remainder/10/3
- **Then**: The server returns either 200 (confirming auth is not enforced and the 401 annotation is dead boilerplate) or 401 (confirming auth IS enforced); it must not return 403 without first returning 401, and must not return 500; the actual behavior must be consistent across all six endpoints
- **Priority**: HIGH
- **Estimated turns**: 2

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R4. Division by zero in remainder (b=0)

- **Given**: GET /api/remainder/{a}/{b} computes a mod b
- **When**: GET /api/remainder/7/0
- **Then**: Response is 400 Bad Request with an explanatory message; not 500 Internal Server Error and not a response body containing Infinity, NaN, or a Java ArithmeticException stack trace
- **Priority**: HIGH
- **Estimated turns**: 1

### R5. Mathematically undefined domain for expint (x=0 or x<0)

- **Given**: The exponential integral E_n(x) diverges at x=0 for n≤1 and is undefined for x<0
- **When**: GET /api/expint/1/0 and GET /api/expint/2/-1.0
- **Then**: Both return 400 Bad Request; neither returns 200 with NaN or Infinity in resultAsDouble (which would be invalid JSON), and neither returns 500
- **Priority**: HIGH
- **Estimated turns**: 1

### R6. Mathematically undefined domain for gammq (a≤0 or x<0)

- **Given**: The incomplete gamma function Q(a,x) requires a>0 and x≥0
- **When**: GET /api/gammq/0.0/1.0, GET /api/gammq/-1.0/1.0, and GET /api/gammq/2.0/-1.0
- **Then**: All three return 400 Bad Request; none returns 500 or a body containing NaN/Infinity
- **Priority**: HIGH
- **Estimated turns**: 1

### R7. Invalid degrees of freedom for fisher (m≤0 or n≤0)

- **Given**: Fisher's F-distribution requires both degree-of-freedom parameters m and n to be positive integers
- **When**: GET /api/fisher/0/5/1.0, GET /api/fisher/-1/5/1.0, and GET /api/fisher/5/0/1.0
- **Then**: All return 400 Bad Request; none returns 500 or a nonsensical numeric result
- **Priority**: HIGH
- **Estimated turns**: 1

### R8. Very large order n causes timeout or graceful rejection rather than infinite loop

- **Given**: Iterative Bessel and exponential-integral algorithms may loop proportionally to n
- **When**: GET /api/bessj/100000/1.0 and GET /api/expint/100000/1.0
- **Then**: Each request either completes within a reasonable timeout (e.g., 5 seconds) with a valid numeric result, or returns 400/503/429; the server must not hang indefinitely, exhaust a thread, or return 500 after a timeout
- **Priority**: HIGH
- **Estimated turns**: 2

### R9. Special IEEE 754 float values in double path parameters

- **Given**: Path parameters for x, a are typed as double; HTTP paths may carry URL-encoded special strings
- **When**: GET /api/gammq/2.0/Infinity, GET /api/gammq/2.0/NaN, and GET /api/expint/2/Infinity
- **Then**: Each returns 400 Bad Request; none returns 500 or a response body that itself contains a bare NaN or Infinity literal (which is not valid JSON)
- **Priority**: HIGH
- **Estimated turns**: 1

### R10. Integer overflow in triangle edge sum

- **Given**: Triangle inequality check computes a+b; if a and b are near INT_MAX this overflows a 32-bit integer
- **When**: GET /api/triangle/2147483647/2147483647/1
- **Then**: Response is 400 Bad Request or a correct overflow-safe result; not 200 with an incorrect triangle classification caused by signed integer wraparound, and not 500
- **Priority**: HIGH
- **Estimated turns**: 1

## Open Exploration

Probe whether any endpoint ever returns a JSON body containing a bare NaN or Infinity literal (technically invalid JSON per RFC 8259, but produced by default Java/Jackson serialization of Double.NaN). Investigate whether the 401/403 Swagger annotations are enforced by a real security filter or are dead boilerplate copied from a template — if enforced, check whether the filter itself throws 500 on malformed tokens. Explore whether repeated expensive calls (large n bessj/expint) trigger any rate-limiting or circuit-breaker behavior, or whether a single slow request can starve the thread pool. Also probe whether the Dto response always populates exactly one of resultAsDouble or resultAsInt, or whether both fields can be null simultaneously on an error path that incorrectly returns 200.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*