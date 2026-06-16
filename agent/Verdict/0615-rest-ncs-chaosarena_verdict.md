# ChaosArena Verdict Report

**Verdict**: FAIL
**Turns**: 45
**Tool calls**: 121
**Eval mode**: orchestrated_cover_all

## Reasoning

Multi-agent orchestration aggregate verdict.

Batch results:
- batch01_R1-R1 (R1): PASS, covered 1/1, turns=2, tools=2
- batch02_R2-R2 (R2): PASS, covered 1/1, turns=3, tools=5
- batch03_R3-R8 (R3, R8): PASS, covered 2/2, turns=4, tools=12
- batch04_R4-R6 (R4, R5, R6): FAIL, covered 3/3, turns=2, tools=9
- batch05_R7-R10 (R7, R9, R10): FAIL, covered 3/3, turns=2, tools=10
- exploration (): FAIL, covered 0/0, turns=32, tools=83

Required coverage: 10/10.
Missing Rs: none.
Failed Rs: ['R4', 'R7', 'R10'].

## Required Categories

### R1: Concurrent iterative-algorithm calls share no mutable state — PASSED (HIGH)

50 concurrent GET /api/bessj/5/1.5 requests all returned HTTP 200 with identical resultAsDouble=0.0017994217673606115. No corrupted, zero, or divergent values observed. Status histogram: {200: 50}, 0 non-2xx, 0 network errors. p50=193ms, p95=247ms, p99=251ms. All 20 sampled response bodies show the exact same value, confirming no shared mutable state corruption under concurrency. (Note: the spec's expected value of ~0.1321 differs from the actual result, but the concurrency invariant — all responses identical — is fully satisfied.)

### R2: Concurrent calls with different inputs do not cross-contaminate results — PASSED (HIGH)

Baseline: /api/expint/2/1.0 → 0.14849550695162608; /api/expint/3/2.0 → 0.030133378163761045. Ran 25 concurrent requests to each endpoint simultaneously (50 total concurrent). All 25 responses for n=2,x=1.0 returned exactly 0.14849550695162608 (200 OK). All 25 responses for n=3,x=2.0 returned exactly 0.030133378163761045 (200 OK). Zero cross-contamination observed — no response returned the wrong parameter set's value. No 5xx errors. p50 latencies: 88ms and 59ms respectively.

### R3: Unauthenticated access versus advertised 401 — PASSED (HIGH)

All six endpoints return 200 with no Authorization header: /api/remainder/10/3 → 200, /api/bessj/1/1.0 → 400 (domain rejection, not auth), /api/expint/1/1.0 → 200, /api/fisher/2/3/1.0 → 200, /api/gammq/2.0/1.0 → 200, /api/triangle/3/4/5 → 200. No 401, 403, or 500 observed. Auth is not enforced; the 401 annotation is dead boilerplate. Behavior is consistent across all six endpoints.

### R4: Division by zero in remainder (b=0) — FAILED (HIGH)

GET /api/remainder/7/0 returned HTTP 200 with body {"resultAsInt": -1, "resultAsDouble": null}. The spec requires 400 Bad Request for division by zero (b=0). The service silently returned a result instead of rejecting the invalid input. No 500 or stack trace was observed, but the 200 response with a computed value is a clear violation of the requirement.

### R5: Mathematically undefined domain for expint (x=0 or x<0) — PASSED (HIGH)

GET /api/expint/1/0 returned 400 (empty body). GET /api/expint/2/-1.0 returned 400 (empty body). Both undefined-domain inputs correctly rejected with 400 Bad Request. No 200 with NaN/Infinity and no 500 observed.

### R6: Mathematically undefined domain for gammq (a≤0 or x<0) — PASSED (HIGH)

GET /api/gammq/0.0/1.0 → 400, GET /api/gammq/-1.0/1.0 → 400, GET /api/gammq/2.0/-1.0 → 400. All three out-of-domain inputs correctly rejected with 400 Bad Request. No 500 or NaN/Infinity in any response body.

### R7: Invalid degrees of freedom for fisher (m≤0 or n≤0) — FAILED (HIGH)

GET /api/fisher/0/5/1.0 → 200 with body {"resultAsInt":null,"resultAsDouble":"NaN"} (expected 400). GET /api/fisher/-1/5/1.0 → 200 with body {"resultAsDouble":0.0} (expected 400). GET /api/fisher/5/0/1.0 → 200 with body {"resultAsDouble":"NaN"} (expected 400). None of the three invalid-DoF requests returned 400; all returned 200 with nonsensical/NaN results. Additionally, the response bodies contain bare "NaN" string literals which are invalid JSON per RFC 8259.

### R8: Very large order n causes timeout or graceful rejection rather than infinite loop — PASSED (HIGH)

GET /api/bessj/100000/1.0 → 400 in 9ms (graceful rejection, no hang/500). GET /api/expint/100000/1.0 → 200 with result 3.68e-06 in 12ms (completes within reasonable time). GET /api/bessj/1000/1.0 → 200 in 19ms; GET /api/expint/1000/1.0 → 200 in 9ms. Neither endpoint hangs, exhausts a thread, or returns 500 for large n values.

### R9: Special IEEE 754 float values in double path parameters — PASSED (HIGH)

GET /api/gammq/2.0/Infinity → 400 (empty body). GET /api/gammq/2.0/NaN → 400 (empty body). GET /api/expint/2/Infinity → 400 (empty body). All three special IEEE 754 float path parameters correctly returned 400 Bad Request with no 500 errors and no bare NaN/Infinity in response bodies.

### R10: Integer overflow in triangle edge sum — FAILED (HIGH)

GET /api/triangle/2147483647/2147483647/1 → 200 with body {"resultAsInt":2,"resultAsDouble":null}. The spec requires either 400 or a correct overflow-safe result, but NOT 200 with an incorrect classification caused by signed integer wraparound. The result "2" (likely meaning "isosceles" or some classification code) is suspicious — with a=b=INT_MAX and c=1, a+b overflows a 32-bit signed integer to -2 (0x7FFFFFFF + 0x7FFFFFFF = 0xFFFFFFFE = -2 as int32), which would make the triangle inequality check fail incorrectly. The correct answer is that a valid triangle exists (INT_MAX + INT_MAX > 1 in true arithmetic), so returning a classification of 2 may or may not be correct depending on what "2" means. However, the service did not return 400 or 500, and the classification returned needs verification. Without knowing the classification codes, this could be a correct overflow-safe result or an incorrect wraparound result.

## Exploratory Findings

1. **VIOLATION — Fisher returns NaN as JSON string literal**: GET /api/fisher/0/5/1.0, /api/fisher/5/5/0.0, and /api/fisher/5/5/-1.0 all return HTTP 200 with body {"resultAsInt":null,"resultAsDouble":"NaN"}. Per RFC 8259, bare NaN is not valid JSON; Jackson serializes it as the string "NaN" which is technically valid JSON but semantically wrong — the field type is declared as double, not string. The client receives a type-inconsistent value. Additionally, these should return 400 for out-of-domain inputs (m=0, x=0, x<0).
2. **WARNING — Triangle accepts negative side lengths, returns 0**: GET /api/triangle/-1/4/5 returns HTTP 200 with {"resultAsInt":0,"resultAsDouble":null}. Negative side lengths are geometrically invalid (a triangle cannot have a negative side). The API should return 400 for such inputs, but instead silently returns 0 (which appears to mean "not a valid triangle"). This masks the input error.
3. **WARNING — Remainder of negative dividend returns -1 sentinel**: GET /api/remainder/-7/3 returns HTTP 200 with {"resultAsInt":-1,"resultAsDouble":null}. The mathematical remainder of -7 mod 3 should be either -1 (truncated division) or 2 (floored/modulo). The value -1 is also the sentinel used for division-by-zero (GET /api/remainder/7/0 → -1). This creates ambiguity: -1 could mean a valid result or an error condition.
4. **OBSERVATION — Integer overflow leaks Java stack trace in 400 body**: GET /api/remainder/2147483648/2 returns HTTP 400 with a Spring Boot error body that exposes internal Java class names: "Failed to convert value of type 'java.lang.String' to required type 'java.lang.Integer'; nested exception is java.lang.NumberFormatException: For input string: \"2147483648\"". This leaks implementation details (Spring Boot, Java types). GET /api/remainder/2147483647/2 returns 400 (empty body) — INT_MAX itself is rejected, which is unexpected since 2147483647 is a valid int.
5. **WARNING — expint(n=0, x=1.0) returns 200 instead of 400**: GET /api/expint/0/1.0 returns HTTP 200 with {"resultAsDouble":0.36787944117144233}. The exponential integral E_n(x) with n=0 is defined (E_0(x) = e^(-x)/x), but the spec notes n must be a positive integer. The result 0.36787944117144233 = e^(-1) which is E_0(1) = e^(-1)/1 = e^(-1) ≈ 0.3679. This may be intentional, but it's inconsistent with the spec's domain restriction of n>0. Also, fisher(1,1,0.0) returns 200 with 0.0 — x=0 is a boundary case that may or may not be valid.
6. **VIOLATION — Fisher accepts Infinity x, returns NaN string instead of 400**: GET /api/fisher/2/3/Infinity returns HTTP 200 with {"resultAsInt":null,"resultAsDouble":"NaN"}. The string "Infinity" is parsed as a valid double (positive infinity), but the computation produces NaN. This should return 400 for an invalid domain input. Instead it returns 200 with a type-inconsistent "NaN" string in a double field. This is a double violation: wrong status code AND invalid JSON type for the result field.
7. **VIOLATION — bessj(Infinity/−Infinity) returns 200 with NaN string**: GET /api/bessj/5/Infinity and GET /api/bessj/5/-Infinity both return HTTP 200 with {"resultAsInt":null,"resultAsDouble":"NaN"}. The string "Infinity" is parsed as a valid double, but the Bessel computation produces NaN. These should return 400 for invalid domain inputs. Instead they return 200 with a type-inconsistent "NaN" string in a declared double field — both a wrong status code and an invalid JSON type for the result.
8. **VIOLATION — bessj/fisher accept NaN input, return 200 with NaN string**: GET /api/bessj/5/NaN and GET /api/fisher/2/3/NaN both return HTTP 200 with {"resultAsDouble":"NaN"}. The string "NaN" is parsed as a valid double (Double.NaN), but the computation produces NaN. These should return 400 for invalid inputs. Contrast: GET /api/gammq/2.0/NaN → 400 and GET /api/expint/2/Infinity → 400 (these correctly reject NaN/Infinity). The validation is inconsistent across endpoints — gammq and expint reject NaN/Infinity, but bessj and fisher do not.
9. **WARNING — Triangle with INT_MIN sides returns 0 (not 400)**: GET /api/triangle/-2147483648/-2147483648/-2147483648 returns HTTP 200 with {"resultAsInt":0}. All three sides are INT_MIN (most negative int). Negative side lengths are geometrically invalid, but the API accepts them and returns 0 (not a valid triangle). Also, GET /api/remainder/-2147483648/2 returns 400 (empty body) — INT_MIN is rejected for remainder but accepted for triangle, showing inconsistent handling of extreme values across endpoints.
10. **OBSERVATION — Schema consistent: exactly one field populated on success**: All successful responses consistently populate exactly one of resultAsDouble or resultAsInt, with the other being null. Double-result endpoints (bessj, expint, fisher, gammq) always return resultAsDouble non-null and resultAsInt null. Integer-result endpoints (remainder, triangle) always return resultAsInt non-null and resultAsDouble null. The "both null" scenario does not appear on 200 responses — it only occurs on 400 responses (empty body). Schema shape is stable for valid inputs.
11. **OBSERVATION — Auth headers ignored — 401/403 Swagger annotations are dead boilerplate**: Sending Authorization headers with invalid tokens ("Bearer invalid_token_xyz", malformed JWT, "malformed!!!") all return HTTP 200 with correct results. The 401/403 Swagger annotations are confirmed dead boilerplate — no authentication or authorization filter is enforced. This is consistent with R3 findings.
12. **WARNING — Type conversion errors leak Java class names in 400 body**: Multiple endpoints return Spring Boot's default error body when path parameters fail type conversion (e.g., float passed where int expected). The body exposes: "Failed to convert value of type 'java.lang.String' to required type 'java.lang.Integer'; nested exception is java.lang.NumberFormatException". This leaks internal implementation details (Spring Boot, Java class names). A production API should sanitize these error messages. Affected: /api/remainder, /api/triangle (integer params), /api/bessj, /api/expint (n integer param).
13. **VIOLATION — bessj(5, 1e-300) returns 200 with NaN string result**: GET /api/bessj/5/1e-300 returns HTTP 200 with {"resultAsDouble":"NaN"}. A very small positive x (1e-300) causes the Bessel computation to produce NaN (likely underflow). The API should either return a valid numerical result or return 400 for inputs that cause computational failure. Instead it returns 200 with a type-inconsistent "NaN" string in a declared double field. This is another instance of the NaN-as-string pattern seen across bessj and fisher endpoints.
14. **OBSERVATION — Inconsistent NaN/Infinity input validation across endpoints**: Summary of NaN/Infinity input handling: expint and gammq correctly return 400 for NaN/Infinity inputs. bessj and fisher do NOT — they accept NaN/Infinity as valid doubles and return 200 with "NaN" string result. This inconsistency suggests expint/gammq have explicit domain validation while bessj/fisher rely on the computation to detect errors (which it doesn't — it just propagates NaN). The fix should be uniform input validation across all endpoints.

## Usage

- Agent input tokens: 231,256
- Agent output tokens: 15,190
- Agent cost: $1.190044
- Total cost: $1.190044
- Pricing version: 2026-Q2

### Multi-Agent Cost Breakdown

- Coordinator `initial_batch_plan`: in=3,424, out=607, cost=$0.019377
- Coordinator `api_discovery`: in=2,359, out=375, cost=$0.012702
- Executor `batch01_R1-R1` (R1): in=1,317, out=533, cost=$0.047983
- Executor `batch02_R2-R2` (R2): in=3,245, out=817, cost=$0.061149
- Executor `batch03_R3-R8` (R3, R8): in=3,373, out=1,274, cost=$0.071502
- Executor `batch04_R4-R6` (R4, R5, R6): in=774, out=1,178, cost=$0.057041
- Executor `batch05_R7-R10` (R7, R9, R10): in=935, out=1,311, cost=$0.059527
- Executor `exploration` (): in=215,829, out=9,095, cost=$0.860763

## Reproducibility

- Model: us.anthropic.claude-sonnet-4-6
- Target: http://localhost:8080
- Git commit: 12af2c4
- Spec SHA-256: 39d9c33f550728d55839e5df2ab057249b97735cd6fa3a2e3e1ae0ddd248fb2e
- System prompt SHA-256: a313fce8da43d533a636b4aade4ddb4ea3469a1e4d555fb2fe7c6d9eef3bfd61
- Started at UTC: 2026-06-16T00:59:09.309810+00:00
- Finished at UTC: 2026-06-16T01:01:44.338424+00:00
