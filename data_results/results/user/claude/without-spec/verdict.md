# QA Test Report — User Management Service

**Target:** `http://localhost:8080/`  
**Spec:** `user-management.json` (Swagger 2.0)  
**Tests executed:** 82 | **Passed:** 43 | **Bugs found:** 9

---

## BUGS FOUND

### BUG-1 — POST /login: returns HTTP 400 for invalid credentials instead of 401

**Description:** Authentication failures return `400 Bad Request` instead of `401 Unauthorized`.

**Request:**

```
POST /login
Content-Type: application/json
{"username": "nonexistent_user_xyz", "password": "wrongpassword"}
```

**Response:**

```
HTTP 400
{"message":"Invalid username or password","timestamp":1781892847433}
```

**Spec violation:** The spec defines `401 Unauthorized` as the response for authentication failure. HTTP 400 means "bad request" (malformed input), not "wrong credentials." This makes it impossible for clients to distinguish bad input from rejected credentials.

---

### BUG-2 — Multiple write endpoints return HTTP 201 instead of 200

**Description:** Every mutating endpoint returns `201 Created` for successful operations where the spec defines `200 OK` as the primary success code. The following endpoints are all affected:

| Endpoint                                                      | Observed | Spec says |
| ------------------------------------------------------------- | -------- | --------- |
| `POST /users`                                                 | 201      | 200       |
| `PUT /users/{id}`                                             | 201      | 200       |
| `POST /users/register`                                        | 201      | 200       |
| `GET /users/rbac/salt`                                        | 201      | 200       |
| `POST /users/rbac/permissions`                                | 201      | 200       |
| `PUT /users/rbac/permissions`                                 | 201      | 200       |
| `POST /users/rbac/roles`                                      | 201      | 200       |
| `POST /users/rbac/roles/{roleId}/permissions/{permissionKey}` | 201      | 200       |
| `POST /users/{id}/roles/{roleId}`                             | 201      | 200       |

The worst case is `GET /users/rbac/salt` — a read-only GET endpoint returning `201 Created` is semantically nonsensical.

**Example request/response:**

```
GET /users/rbac/salt
→ HTTP 201   (body: "PWpLzrsSm3YVNeGbe89x1Ysd3ku0CgaD")
```

**Spec violation:** All nine endpoints define `"200": {"description": "OK"}` as the primary success response. Clients that check for `== 200` will treat all successful responses as failures.

---

### BUG-3 — POST /users/rbac/roles: role name stored with extra JSON double-quotes

**Description:** When creating a role by sending a JSON string body as specified, the service stores the value with the surrounding double-quote characters embedded in the name.

**Request:**

```
POST /users/rbac/roles
Content-Type: application/json
"QA_TEST_ROLE"
```

**Response:**

```
HTTP 201
{"id":3,"role":"\"QA_TEST_ROLE\"","permissions":[]}
```

The `role` field is `"\"QA_TEST_ROLE\""` — the name contains literal quote characters. Only by sending a raw unquoted string (not valid JSON) does the name store correctly. This means any spec-compliant client will create broken role names.

**Spec violation:** The spec requires the body `schema: { "type": "string" }`. A JSON-conformant client sends a JSON string `"ROLE_NAME"`. The service must parse and store the string value, not the raw JSON token including its quotes.

---

### BUG-4 — POST /users/rbac/roles/{roleId}/permissions/{permissionKey}: auto-creates permission instead of returning 404

**Description:** Adding a permission key that does not exist to a role silently creates a new permission record with that key and assigns it, rather than returning 404.

**Request:**

```
POST /users/rbac/roles/1/permissions/NONEXISTENT_XYZ_PERM
```

**Response:**

```
HTTP 201
{"id":1,"role":"USER","permissions":[
  {"id":1,"permission":"LOGIN",...},
  {"id":2,"permission":"VIEW_PROFILE",...},
  {"id":10,"permission":"NONEXISTENT_XYZ_PERM","enabled":true,"note":null}
]}
```

A new permission with key `NONEXISTENT_XYZ_PERM` was silently created. Because the USER role got this permission, **all users in the USER role (all 8 seed users) inherited the phantom permission** — a data integrity cascade across the entire user base.

**Spec violation:** The spec defines `404 Not Found` as a possible response. Adding a permission to a role should validate that the permission exists; it must not create new permissions as a side effect of this operation.

---

### BUG-5 — DELETE /users/rbac/roles/{roleId}/permissions/{permissionKey}: returns 200 even when permission was not on that role

**Description:** Removing a permission from a role succeeds silently when the permission was never assigned to that role. No error is reported.

**Request:**

```
DELETE /users/rbac/roles/1/permissions/ADMIN_STATISTICS
(ADMIN_STATISTICS is not in the USER role)
```

**Response:**

```
HTTP 200
{"id":1,"role":"USER","permissions":[{"id":1,...},{"id":2,...}]}
```

The response body is unchanged (correct) but the status is 200 instead of an error. Clients cannot detect whether the operation had any effect.

**Spec violation:** The spec defines `200` (success with updated role) and `204` (no content). When the permission-to-role mapping does not exist, an appropriate 4xx response should indicate the relationship was not found.

---

### BUG-6 — POST /users: undocumented required field `phone` with undocumented format constraint

**Description:** `POST /users` fails with `400` unless `phone` is provided, and the phone must match an undocumented format (must begin with `+` followed only by digits, e.g. `+12345678900`). This field and its validation rules are absent from the spec's `CreateOrUpdateUserDTO`.

**Request (as per spec — no phone):**

```
POST /users
Content-Type: application/json
{"username":"user1","name":"Test","surname":"User","email":"t@t.com",
 "password":"Str0ng@Pass!","gender":"MALE","enabled":true}
```

**Response:**

```
HTTP 400
{"message":"The phone cannot be null or empty","timestamp":...}
```

**Another hidden constraint — password complexity:**

```
POST /users
...{"password":"securepass123"...}
→ HTTP 400 {"message":"Password must to be at least 8 chars, 1 number, 1 upper case, 1 lower case letter, 1 special char, no spaces"}
```

**Spec violation:** `CreateOrUpdateUserDTO` lists `phone` as an optional string field with no format constraint. Neither `phone` (required) nor password complexity rules are documented anywhere in the spec.

---

### BUG-7 — addressDTO returns `{all-null fields}` instead of `null` when no address is provided

**Description:** For users created without address information, the `addressDTO` field in `UserDTO` is returned as an object with all-null fields rather than `null`.

**Request:**

```
GET /users/9
```

**Response:**

```json
{
  "addressDTO": {"address": null, "address2": null, "city": null, "country": null, "zipCode": null},
  ...
}
```

**Spec violation:** The spec defines `addressDTO` as `"$ref": "#/definitions/AddressDTO"` — an optional object. When not provided, it should be `null` (absent), not an object where every field is null. The two states are semantically different: one means "no address on file," the other means "address object with no data."

---

### BUG-8 — User seed data: jennifer (id=7) has `gender="MALE"` but name is "Jennifer"

**Description:** The initial data contains a clear inconsistency: user `jennifer` with `name="Jennifer"` has `gender="MALE"`.

**Request:**

```
GET /users/7
```

**Response:**

```json
{"id":7,"username":"jennifer","name":"Jennifer","gender":"MALE",...}
```

**Spec violation:** While the spec does not enforce gender-name consistency, this indicates either a data entry bug in the seed data or that the API accepts logically contradictory data with no validation.

---

### BUG-9 — POST /users/register: undocumented password complexity enforcement

**Description:** The `/register` endpoint also silently enforces password complexity rules (same as `/users`), but neither the spec's `RegisterUserAccountDTO` schema nor the endpoint description mentions this.

**Request:**

```
POST /users/register
{"username":"qa_reg_user","name":"Reg","surname":"User",
 "email":"reg@example.com","password":"mypassword123","gender":"FEMALE"}
```

**Response:**

```
HTTP 400
{"message":"Password must to be at least 8 chars, 1 number, 1 upper case, 1 lower case letter, 1 special char, no spaces"}
```

**Spec violation:** `RegisterUserAccountDTO` defines `password` as `type: string` with no constraints documented. A spec-compliant client has no way to know the password rules.

---

## CORRECT BEHAVIORS VERIFIED

| Test    | What was verified                                                                   |
| ------- | ----------------------------------------------------------------------------------- |
| T01–T04 | `GET /users` returns 200 with `UserListDTO` structure, all `UserDTO` fields present |
| T10–T11 | `GET /users/1` returns correct user data                                            |
| T12     | `GET /users/99999` returns 404                                                      |
| T13–T15 | `GET /users/0`, `GET /users/-1`, `GET /users/abc` all return 4xx (input validation) |
| T19     | Duplicate username on `POST /users` returns 4xx conflict                            |
| T20–T21 | Empty/null body on `POST /users` returns 4xx (no crash)                             |
| T22     | 1000-character username handled without 500                                         |
| T23     | SQL injection in username returns 4xx (not 500, not bypassed)                       |
| T26     | `PUT /users/99999` returns 404                                                      |
| T30     | `DELETE /users/99999` returns 404                                                   |
| T34     | Duplicate username on `POST /users/register` returns 4xx                            |
| T35     | Empty body on `POST /users/register` returns 4xx (not 500)                          |
| T38–T40 | `GET /users/rbac/permissions` returns 200 with correct `PermissionDTO` array        |
| T43     | Duplicate permission key returns 4xx                                                |
| T44     | `GET /users/rbac/permissions/LOGIN` returns 200                                     |
| T45     | `GET /users/rbac/permissions/NONEXISTENT` returns 404                               |
| T51     | `DELETE` non-existent permission returns 404                                        |
| T52–T54 | `GET /users/rbac/roles` returns 200 with correct `RoleDTO` array                    |
| T57     | Duplicate role name returns 4xx                                                     |
| T58–T60 | Role GET by valid id returns 200; non-existent returns 404; string id returns 400   |
| T63     | Adding permission to non-existent role returns 404                                  |
| T71     | Adding role to non-existent user returns 404                                        |
| T74     | `DELETE /users/rbac/roles/99999` returns 404                                        |
| T75     | Wrong `Content-Type` returns 4xx                                                    |
| T77     | 20KB+ request body handled without 500                                              |
| T78     | `PATCH /users/1` returns 405 (method not allowed)                                   |
| T79     | Extra unknown query params ignored (returns 200)                                    |
| T80     | Unicode characters in name fields handled                                           |
| —       | Deleting an in-use role returns 400 with clear message                              |
| —       | Deleting an in-use permission returns 400 with clear message                        |
| —       | `GET /users/rbac/salt` returns different value each call (entropy)                  |
| —       | Response `Content-Type: application/json` correctly set on all endpoints            |
| —       | User's effective permissions correctly aggregate from all assigned roles            |

---

## OVERALL VERDICT: **FAIL**

The service has multiple spec-violating bugs across critical areas. The most severe are:

1. **Wrong HTTP status codes on virtually every write operation** (BUG-2) — nine endpoints return 201 where the spec mandates 200, including a GET endpoint returning 201. This breaks any client that checks the status code.

2. **Ghost permission creation** (BUG-4) — adding a non-existent permission key to a role silently creates a new permission and assigns it, cascading phantom permissions to all users in that role. This is a data integrity and security issue.

3. **Broken login status codes** (BUG-1) — returning 400 instead of 401 makes it impossible for clients to distinguish malformed requests from authentication failures.

4. **Undocumented required fields** (BUG-6, BUG-9) — the spec-defined DTOs omit mandatory validation rules (`phone` required with format constraint, password complexity), making the spec insufficient for client implementation.

5. **Role name corruption** (BUG-3) — spec-compliant JSON string bodies produce role names with embedded double-quote characters.

The service demonstrates functional CRUD logic and reasonable input validation for many paths, but the systematic status code misalignment and the ghost-permission security flaw make it unfit for production use as specified.
[usage] in=28 out=29,334 cost=$0.0000 wall=422.5s
