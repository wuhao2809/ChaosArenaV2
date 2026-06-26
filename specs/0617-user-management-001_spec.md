# user_rbac_management — System Spec (drafted)

## Description

A Spring Boot REST API providing user account management with full RBAC. Users can register at /users/register or be created by an admin at /users. Authentication is performed at /login. Roles are managed under /users/rbac/roles and permissions under /users/rbac/permissions; roles can be assigned to users and permissions can be assigned to roles. A salt-generation utility is exposed at /users/rbac/salt. The UserDTO returned after login includes the user's resolved roles and permissions.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Authentication

Authenticate by sending POST /login with Content-Type: application/json and body {"username": "<username>", "password": "<password>"}. On success the server returns HTTP 200 with a UserDTO body. Because the service is built on Spring Boot / Spring Security, it most likely establishes a server-side session and returns a Set-Cookie: JSESSIONID=<token> response header; include that cookie on all subsequent requests as Cookie: JSESSIONID=<token>. If the implementation uses stateless JWT instead, probe for an Authorization or X-Auth-Token response header and send it as Authorization: Bearer <token> on subsequent requests. Obtain an admin-level session by logging in with a seeded admin account before executing any RBAC-management or cross-user test cases.

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent registration with identical username

- **Given**: No user with username 'raceuser' exists
- **When**: Two clients simultaneously POST /users/register with identical username 'raceuser' and identical or different emails within 10ms of each other
- **Then**: Exactly one request returns 200 or 201 with a UserDTO; the other returns 409 Conflict; a subsequent GET /users (admin) shows exactly one record with that username, not two
- **Priority**: HIGH
- **Estimated turns**: 3

### R2. Concurrent role assignment and global role deletion

- **Given**: Role R exists; User U exists with no roles assigned
- **When**: Client A POSTs /users/{U.id}/roles/{R.id} while Client B DELETEs /users/rbac/roles/{R.id} in parallel
- **Then**: Either the assignment returns 404 (role already gone) or the deletion returns 409/200 with cleanup; GET /users/{U.id} must not list a dangling role name that no longer exists in /users/rbac/roles
- **Priority**: HIGH
- **Estimated turns**: 3

### R3. Concurrent permission deletion and permission-to-role assignment

- **Given**: Permission P exists; Role R exists without permission P
- **When**: Client A POSTs /users/rbac/roles/{R.id}/permissions/{P.key} while Client B DELETEs /users/rbac/permissions/{P.key} in parallel
- **Then**: Either the assignment returns 404 or the deletion returns 409/200 with cascade; GET /users/rbac/roles/{R.id} must not list a permission key that no longer exists in /users/rbac/permissions
- **Priority**: HIGH
- **Estimated turns**: 3

### R4. Concurrent duplicate user creation via admin endpoint

- **Given**: No user with username 'adminrace' exists
- **When**: Two admin clients simultaneously POST /users with identical username 'adminrace' within 10ms
- **Then**: Exactly one returns 200 or 201; the other returns 409 Conflict; only one user record is persisted
- **Priority**: HIGH
- **Estimated turns**: 3

<!-- Category: async_invariants — Async / temporal invariants -->

*Category async_invariants marked N/A by drafter: The API is described as fully synchronous CRUD with no queues, background workers, schedulers, or webhooks mentioned; all mutations are expected to be immediately consistent on the same request.*

<!-- Category: auth_boundaries — Authorization boundaries -->

### R5. IDOR: regular user reads another user's full profile

- **Given**: User A and User B are both registered; User A is authenticated as a non-admin
- **When**: User A sends GET /users/{B.id}
- **Then**: Response is 403 Forbidden or 404 Not Found; User B's name, email, roles, permissions, and contact details are not present in the response body
- **Priority**: HIGH
- **Estimated turns**: 2

### R6. IDOR: regular user modifies or deletes another user's account

- **Given**: User A and User B are both registered; User A is authenticated as a non-admin
- **When**: User A sends PUT /users/{B.id} with a modified email field, then separately DELETE /users/{B.id}
- **Then**: Both requests return 403 or 404; User B's record is unchanged and still retrievable by an admin
- **Priority**: HIGH
- **Estimated turns**: 2

### R7. Privilege escalation: regular user assigns an admin role to themselves

- **Given**: An admin role with a known roleId exists; User A is authenticated as a regular (non-admin) user
- **When**: User A POSTs /users/{A.id}/roles/{adminRoleId}
- **Then**: Response is 403 Forbidden; GET /users/{A.id} (by an admin) shows User A's roles list is unchanged
- **Priority**: HIGH
- **Estimated turns**: 2

### R8. Regular user accesses RBAC management endpoints

- **Given**: User A is authenticated as a regular (non-admin) user
- **When**: User A sends GET /users/rbac/roles, POST /users/rbac/roles, GET /users/rbac/permissions, POST /users/rbac/permissions, and PUT /users/rbac/permissions in sequence
- **Then**: Every request returns 403 Forbidden; no role or permission is created or modified
- **Priority**: HIGH
- **Estimated turns**: 2

### R9. Anonymous access to protected endpoints

- **Given**: No authentication credentials or session cookie are provided
- **When**: Anonymous client sends GET /users, GET /users/{id}, GET /users/rbac/roles, GET /users/rbac/permissions, and DELETE /users/{id}
- **Then**: Every request returns 401 Unauthorized; no user or RBAC data is returned in any response body
- **Priority**: HIGH
- **Estimated turns**: 1

### R10. Salt generation endpoint accessible without authentication

- **Given**: No authentication credentials are provided
- **When**: Anonymous client sends GET /users/rbac/salt
- **Then**: Response is 401 Unauthorized; the salt string is not returned, preventing unauthenticated callers from probing the hashing scheme
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R11. Login succeeds for a disabled user account

- **Given**: User U exists with enabled=false (set by an admin via PUT /users/{U.id})
- **When**: POST /login with U's valid credentials
- **Then**: Response is 401 or 403; no session cookie or token is issued; UserDTO is not returned
- **Priority**: MEDIUM
- **Estimated turns**: 2

### R12. Regular user lists all users

- **Given**: User A is authenticated as a regular (non-admin) user
- **When**: User A sends GET /users
- **Then**: Response is 403 Forbidden; the full user list including other users' emails, roles, and contact details is not returned
- **Priority**: HIGH
- **Estimated turns**: 2

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R13. Duplicate username or email on registration

- **Given**: A user with username 'alice' and email 'alice@example.com' already exists
- **When**: POST /users/register with the same username 'alice' (different email), then separately with the same email 'alice@example.com' (different username)
- **Then**: Each returns 409 Conflict or 400 Bad Request; not 500; no duplicate user record is created
- **Priority**: HIGH
- **Estimated turns**: 1

### R14. Oversize string fields on registration and update

- **Given**: No client-side size constraints are enforced
- **When**: POST /users/register with username of 10,000 characters; separately with password of 10,000 characters; separately PUT /users/{id} with note of 100,000 characters
- **Then**: Each returns 400 Bad Request or 413 Payload Too Large; not 500 and not silent truncation that stores a partial value
- **Priority**: HIGH
- **Estimated turns**: 1

### R15. SQL injection payload in login credentials

- **Given**: Login endpoint parses username and password from JSON body
- **When**: POST /login with username="' OR '1'='1'--" and password="anything"
- **Then**: Response is 401 Unauthorized; not 200 (no authentication bypass) and not 500 (no unhandled query error); no user data is leaked in the response body
- **Priority**: HIGH
- **Estimated turns**: 1

### R16. permissionKey with path-traversal or URL-encoded special characters

- **Given**: RBAC endpoints accept permissionKey as a URL path segment
- **When**: GET /users/rbac/permissions/{key} where key is '../admin', 'key%2F..%2Fadmin', and 'key with spaces'
- **Then**: Each returns 400 Bad Request or 404 Not Found; not 500; no unintended resource path is resolved
- **Priority**: HIGH
- **Estimated turns**: 1

### R17. Delete role currently assigned to one or more users

- **Given**: Role R is assigned to User U
- **When**: Admin sends DELETE /users/rbac/roles/{R.id}
- **Then**: Either 409 Conflict (role still in use) or 200/204 with cascade removal; GET /users/{U.id} must not list the deleted role name; not 500 and not a silent orphan reference
- **Priority**: HIGH
- **Estimated turns**: 2

### R18. Delete permission currently assigned to one or more roles

- **Given**: Permission P is assigned to Role R
- **When**: Admin sends DELETE /users/rbac/permissions/{P.key}
- **Then**: Either 409 Conflict (permission still in use) or 200/204 with cascade removal; GET /users/rbac/roles/{R.id} must not list the deleted permission key; not 500 and not a silent orphan reference
- **Priority**: HIGH
- **Estimated turns**: 2

### R19. Invalid or impossible birthDate values

- **Given**: birthDate field is typed as ISO date string in CreateOrUpdateUserDTO
- **When**: PUT /users/{id} with birthDate='not-a-date', then with birthDate='99999-99-99', then with birthDate='2024-02-30'
- **Then**: Each returns 400 Bad Request; not 500; no partial update is persisted
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R20. Missing required fields on registration

- **Given**: RegisterUserAccountDTO requires username, password, email, name, and surname
- **When**: POST /users/register omitting username; then omitting password; then omitting email
- **Then**: Each returns 400 Bad Request with a descriptive error; not 500; no partial user record is created
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R21. Negative or zero numeric path parameters

- **Given**: Path parameters id and roleId are declared as int64
- **When**: GET /users/-1, GET /users/0, GET /users/rbac/roles/-1, and GET /users/rbac/roles/0
- **Then**: Each returns 400 Bad Request or 404 Not Found; not 500
- **Priority**: LOW
- **Estimated turns**: 1

## Open Exploration

Probe whether the `secured` boolean flag on users controls plaintext vs hashed password storage — setting `secured=false` via PUT /users/{id} and then re-authenticating may reveal a plaintext-storage vulnerability. Verify that sequential integer user IDs cannot be enumerated by a regular user to harvest the full user list one record at a time (IDOR via ID walking). Test whether a user can set their own `enabled=true` via PUT /users/{id} to re-activate a disabled account. Check whether the `permissions` array in UserDTO is computed server-side from the user's roles or can be directly injected by a crafty PUT body. Confirm that the `loginDt`, `creationDt`, and `updatedDt` timestamps are set exclusively server-side and cannot be spoofed by including them in a PUT request body. Finally, probe whether a non-admin user can call POST /users/rbac/roles/{roleId}/permissions/{permissionKey} or DELETE equivalents by guessing valid IDs, bypassing the RBAC guard through direct object reference.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*