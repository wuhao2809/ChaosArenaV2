
# Black-Box QA Test Report — Project Tracking System REST API
**Target:** `http://localhost:8080/app`  
**Date:** 2026-06-18  
**Verdict: FAIL**

---

## API Surface Discovered

Seven resource groups via Swagger (`/app/v2/api-docs`):
- `POST /api/authenticate` — JWT auth
- `/api/assignments` — employee–project commit records
- `/api/credentials` — user accounts & roles
- `/api/departments` — org departments
- `/api/employees` — employee records
- `/api/locations` — office locations
- `/api/projects` — project records

---

## BUGS FOUND

### BUG-01 — CRITICAL: All API Endpoints Publicly Accessible Without Authentication

**Description:** Every endpoint—including credential management and employee PII—responds to unauthenticated requests. The service has a `/api/authenticate` endpoint and claims Spring Security headers, but none of the REST API routes enforce authentication.

**Request:**
```
GET http://localhost:8080/app/api/credentials
(no Authorization header)
```
**Response:** `200 OK` with full credential list including all usernames, bcrypt hashes, and roles.

**Why it violates spec:** A task-tracking system with distinct roles (ROLE_ADMIN, ROLE_MGR, ROLE_EMP) must enforce authentication before any data access. The authentication endpoint is vestigial; it is never required.

---

### BUG-02 — CRITICAL: Credentials (Password Hashes + Roles) Exposed Without Auth

**Description:** `GET /api/credentials` returns all user accounts, their bcrypt password hashes, and roles to any anonymous caller.

**Request:**
```
GET http://localhost:8080/app/api/credentials
```
**Response (excerpt):**
```json
{
  "collection": [
    { "credentialId": 4, "username": "admin", "password": "$2a$10$6pNV34gb...", "enabled": true, "role": "ROLE_ADMIN" },
    ...15 total credentials...
  ]
}
```
**Why it violates spec:** Password hashes must never be returned in API responses. Even hashed, they facilitate offline brute-force attacks.

---

### BUG-03 — CRITICAL: Unauthenticated Write/Delete Access to All Resources

**Description:** POST, PUT, and DELETE requests all succeed without authentication. Any anonymous actor can create, modify, or destroy any data.

**Demonstrated:**
```
# Disable the admin account
PUT http://localhost:8080/app/api/credentials
Body: {"credentialId":4,"username":"admin","password":"$2a$10$...","enabled":false,"role":"ROLE_ADMIN"}
Response: 200 OK — admin account disabled
```
```
# Create a new admin account
POST http://localhost:8080/app/api/credentials
Body: {"username":"newadmin","password":"password123","enabled":true,"role":"ROLE_ADMIN"}
Response: 200 OK — new admin created
```

---

### BUG-04 — CRITICAL: POST Credential Does Not Hash Password (PUT Does)

**Description:** `POST /api/credentials` stores the password field verbatim in plain text. `PUT /api/credentials` correctly bcrypt-hashes the password. The two operations are inconsistent.

**Request:**
```
POST http://localhost:8080/app/api/credentials
Body: {"username":"hashtest","password":"plainpassword","enabled":true,"role":"ROLE_EMP"}
```
**Response:**
```json
{ "credentialId": 17, "username": "hashtest", "password": "plainpassword", ... }
```
**Why it violates spec:** Passwords must be hashed before storage. Plain-text storage is a critical security failure.

---

### BUG-05 — CRITICAL: Cascade Delete Destroys Employees When Department Is Deleted

**Description:** Deleting a department silently deletes all employees in that department (along with all their assignment records) with no warning, no confirmation, and no error. 21 assignments were also lost.

**Request:**
```
DELETE http://localhost:8080/app/api/departments/6
```
**Response:** `true` (200 OK)

**Actual effect:** Department 6 (Billing, 5 employees) was deleted. Employees 1, 4, 6, 7, 8 were permanently deleted. 21 of 59 assignments were destroyed. Attempts to restore the department via POST assigned it a new ID (17), leaving orphaned references.

---

### BUG-06 — HIGH: Missing Resource Returns 400 Bad Request Instead of 404 Not Found

**Description:** Requesting a non-existent resource ID returns `400 BAD_REQUEST` instead of `404 NOT_FOUND`.

**Request:**
```
GET http://localhost:8080/app/api/departments/999
```
**Response:**
```json
{ "msg": "###### NO Department object FOUND! ######", "status": "BAD_REQUEST" }
HTTP Status: 400
```
Same behavior for `/api/employees/999`, `/api/projects/999`, `/api/locations/999`.

**Why it violates spec:** HTTP 404 is the semantically correct code for a missing resource. 400 implies a malformed request, which is incorrect.

---

### BUG-07 — HIGH: Authentication Failure Returns 400 Instead of 401

**Description:** Failed login returns `400 BAD_REQUEST` instead of `401 UNAUTHORIZED`.

**Request:**
```
POST http://localhost:8080/app/api/authenticate
Body: {"username":"admin","password":"wrongpassword"}
```
**Response:**
```json
{ "msg": "Bad credentials", "status": "BAD_REQUEST" }
HTTP Status: 400
```

---

### BUG-08 — HIGH: GET /api/assignments/{employeeId}/{projectId} Always Fails

**Description:** The endpoint for looking up a specific assignment by employee and project ID is completely broken. It returns an internal error for any valid or invalid input.

**Request:**
```
GET http://localhost:8080/app/api/assignments/1/1
```
**Response:**
```json
{ "msg": "HttpStatus must not be null", "status": "BAD_REQUEST" }
HTTP Status: 400
```
Tested with all combinations of existing IDs (1/1, 1/2, 2/1, 2/2)—all fail.

**Why it violates spec:** This endpoint is published in the Swagger spec but is entirely non-functional. The `{employeeId}/{projectId}/{commitDate}` variant works correctly.

---

### BUG-09 — HIGH: GET /api/employees/username/{username} Returns 200 Empty Body for Existing Employee

**Description:** After a credential update operation, `GET /api/employees/username/imentouk` returns `200 OK` with `Content-Length: 0` (empty body), even though the employee exists and is retrievable by ID.

**Request:**
```
GET http://localhost:8080/app/api/employees/username/imentouk
```
**Response:**
```
HTTP/1.1 200
Content-Length: 0
```
**Why it violates spec:** A 200 response must include the resource body. The endpoint should return the employee object or a 404 error.

---

### BUG-10 — MEDIUM: PUT With Non-Existent ID Creates New Record Instead of 404

**Description:** `PUT /api/departments` with a non-existent `departmentId` creates a new record with a fresh auto-incremented ID rather than returning 404.

**Request:**
```
PUT http://localhost:8080/app/api/departments
Body: {"departmentId":9999,"departmentName":"NonExistent","location":{"locationId":1}}
```
**Response:**
```json
{ "departmentId": 16, "departmentName": "NonExistent", ... }
HTTP Status: 200
```
The returned ID is 16 (auto-incremented), not 9999.

---

### BUG-11 — MEDIUM: POST With Existing ID Silently Updates Instead of Creating

**Description:** `POST /api/departments` with an existing `departmentId` updates the existing record rather than rejecting the request or creating a new one.

**Request:**
```
POST http://localhost:8080/app/api/departments
Body: {"departmentId":4,"departmentName":"DWH_OVERWRITE","location":{"locationId":1}}
```
**Response:** `200 OK` — Department 4 was updated with the new name.

---

### BUG-12 — MEDIUM: Project Accepts Invalid Status Values

**Description:** The project status field accepts arbitrary strings; no enum validation is enforced.

**Request:**
```
POST http://localhost:8080/app/api/projects
Body: {"title":"Test","startDate":"01-01-2024","endDate":"31-12-2024","status":"INVALID_STATUS"}
```
**Response:** `200 OK` — Project created with `"status": "INVALID_STATUS"`.

Valid statuses observed: `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`.

---

### BUG-13 — MEDIUM: No Date Range Validation (End Date Before Start Date Accepted)

**Request:**
```
POST http://localhost:8080/app/api/projects
Body: {"title":"Test","startDate":"31-12-2024","endDate":"01-01-2024","status":"NOT_STARTED"}
```
**Response:** `200 OK` — Project created with `endDate < startDate`.

---

### BUG-14 — MEDIUM: POST Returns 200 OK Instead of 201 Created

All create operations (`POST /api/departments`, `/api/projects`, `/api/employees`, etc.) return `200 OK` instead of `201 Created`. This prevents clients from reliably detecting creation vs. update.

---

### BUG-15 — MEDIUM: DELETE Returns `true` With 200 Instead of 204 No Content

**Request:**
```
DELETE http://localhost:8080/app/api/departments/4
```
**Response:**
```
HTTP/1.1 200 OK
Body: true
```
DELETE should return `204 No Content` on success (no body).

---

### BUG-16 — MEDIUM: Server-Side Database Error Returns 400 With Internal Details

**Description:** Submitting an oversized string triggers a Hibernate SQL error, which is returned as a `400 BAD_REQUEST` with internal implementation details.

**Request:**
```
POST http://localhost:8080/app/api/departments
Body: {"departmentName":"<10000 chars>","location":{"locationId":1}}
```
**Response:**
```json
{
  "msg": "could not execute statement; SQL [n/a]; nested exception is org.hibernate.exception.DataException: could not execute statement",
  "status": "BAD_REQUEST"
}
HTTP Status: 400
```
**Why it violates spec:** This is a server-side error (5xx). Returning 400 misclassifies it. The Hibernate class name leaks implementation details.

---

### BUG-17 — MEDIUM: Unsupported Media Type Returns 405 Instead of 415

**Request:**
```
POST http://localhost:8080/app/api/departments
Content-Type: text/plain
Body: test
```
**Response:** `405 Method Not Allowed`

**Why it violates spec:** `415 Unsupported Media Type` is correct for a wrong `Content-Type`. `405` implies the HTTP method itself is not allowed, misleading clients.

---

### BUG-18 — MEDIUM: POST Response for Department Has Null Location Fields

**Description:** The `POST /api/departments` response returns the created resource with null location sub-fields, but an immediate `GET` for the same resource returns the full location data. The create response is inconsistent with the stored state.

**POST response:**
```json
{ "departmentId": 27, "departmentName": "NullFieldTest",
  "location": { "locationId": 1, "adr": null, "postalCode": null, "city": null } }
```
**GET response for same ID:**
```json
{ "departmentId": 27, "departmentName": "NullFieldTest",
  "location": { "locationId": 1, "adr": "RUE DE LA BOURSE", "postalCode": "2016", "city": "LAC2" } }
```

---

### BUG-19 — LOW: Inconsistent Date Formats Across Endpoints

`GET /api/assignments` returns dates as `"26-11-202010:50:09"` (no separator between date and time).  
`GET /api/assignments/data/project-commit/{id}` returns dates as `"2020-12-12T17:25:48"` (ISO 8601).

Same underlying data is serialized in two different formats depending on which endpoint is called.

---

### BUG-20 — LOW: Error Responses Leak Internal Class Names

Validation errors expose full Java class paths:
```
"Validation failed for classes [com.pfa.app.model.entity.Department] during persist time...
ConstraintViolationImpl{interpolatedMessage='*Must not blank**', propertyPath=departmentName,
rootBeanClass=class com.pfa.app.model.entity.Department, messageTemplate='*Must not blank**'}"
```
Internal package structure and ORM implementation details should not be surfaced in API responses.

---

### BUG-21 — LOW: DELETE /api/employees/username/{username} Leaks NullPointerException

Attempting to delete a credential-only user (no associated employee record) crashes with:
```json
{ "msg": "Entity must not be null!; nested exception is java.lang.IllegalArgumentException: Entity must not be null!" }
```
This should return `404 Not Found` with a user-facing message.

---

### BUG-22 — INFO: Duplicate Endpoints for Same Operations

The API exposes redundant routes (`/api/departments/save` = `POST /api/departments`, `/api/departments/update` = `PUT /api/departments`, `/api/departments/delete/{id}` = `DELETE /api/departments/{id}`). These are operational surface area with no additional behavior but double the attack surface.

---

## CORRECT BEHAVIORS VERIFIED

| Test | Result |
|------|--------|
| `GET /api/departments`, `/api/projects`, `/api/locations` return correct data | Pass |
| `GET /api/departments/{validId}` returns the correct object | Pass |
| `GET /api/departments/{nonExistentId}` returns an error (wrong status, but not a crash) | Pass (partial) |
| Validation correctly rejects blank `departmentName` | Pass |
| Validation correctly rejects null `location` on department | Pass |
| `PUT /api/credentials` correctly bcrypt-hashes passwords | Pass |
| Long strings trigger a controlled error (not a silent truncation or panic) | Pass (partial — wrong status code) |
| Invalid JSON body correctly rejected with 400 | Pass |
| XSS payloads in string fields do not cause server errors (stored as literals) | Pass (storage is safe at DB level) |
| SQL injection strings stored as literals; no table destruction occurred | Pass |
| OPTIONS preflight from external origins returns 403 (CORS restricted) | Pass |
| `GET /api/assignments/{employeeId}/{projectId}/{commitDate}` returns correct assignment | Pass |
| Swagger spec available at `/app/v2/api-docs` | Pass |

---

## Overall Verdict: **FAIL**

The service fails on the most fundamental security requirement: **it has no working authentication enforcement on any API endpoint**. Every resource—including the credential store with password hashes—is readable and writable by anonymous HTTP clients. Beyond authentication, there are multiple HTTP semantic violations (400 for 404, 200 for 201, 400 for 401, 400 for 500, 405 for 415), a completely broken assignment lookup endpoint (`GET /api/assignments/{empId}/{projId}`), silent cascade deletion that destroyed 5 employees and 21 assignments during testing, plain-text password storage on credential creation, and inconsistent data serialization across endpoints. The service is not safe to expose to any network in its current state.