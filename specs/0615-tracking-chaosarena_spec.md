# project_tracking_system — System Spec (drafted)

## Description

A multi-role project-tracking REST API where Employees can be assigned to projects and submit commit records, Managers create/edit/delete projects and track employee commits, and Admins manage employee records and credentials. The system exposes resources for Assignments (composite-keyed by employeeId + projectId + commitDate), Projects, Employees, Credentials, Departments, and Locations, with JWT-or-session-based authentication via POST /api/authenticate. Each resource has duplicate route pairs (e.g., POST /api/assignments and POST /api/assignments/save) that are expected to behave identically.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Duplicate commit creation via concurrent POST

- **Given**: Employee E is assigned to Project P; no assignment exists for the composite key (E, P, T)
- **When**: Two concurrent POST /api/assignments/save requests with identical body {employeeId: E, projectId: P, commitDate: T} are issued within 10ms
- **Then**: Exactly one returns 200/201; the other returns 409 Conflict; a subsequent GET /api/assignments/{E}/{P}/{T} returns exactly one record — not two duplicates and not a 500
- **Priority**: HIGH
- **Estimated turns**: 3

### R2. Concurrent delete of the same assignment

- **Given**: Assignment (E, P, T) exists
- **When**: Two DELETE /api/assignments/{E}/{P}/{T} requests are issued in parallel
- **Then**: Exactly one returns 200/204; the second returns 404; no 500 errors; any side-effect counters (e.g., commit count) are decremented exactly once
- **Priority**: HIGH
- **Estimated turns**: 3

### R3. Project delete races with concurrent commit add

- **Given**: Project P exists and Employee E is assigned to it
- **When**: A manager DELETEs /api/projects/{P} while Employee E concurrently POSTs a new assignment commit referencing project P
- **Then**: Either the delete wins (commit POST returns 404 or 409, not 500) or the commit wins (project delete returns 409, not 500); the system must not produce an orphan assignment row referencing a non-existent project
- **Priority**: HIGH
- **Estimated turns**: 4

### R4. Concurrent PUT to same assignment — lost write

- **Given**: Assignment (E, P, T) exists with commitEmpDesc='v1'
- **When**: Two concurrent PUT /api/assignments/update requests both target the same composite key, one setting commitEmpDesc='v2' and the other 'v3'
- **Then**: After both complete, GET /api/assignments/{E}/{P}/{T} returns exactly one of 'v2' or 'v3' (not a merged/corrupted value, not the original 'v1'); no 500 errors
- **Priority**: HIGH
- **Estimated turns**: 3

<!-- Category: async_invariants — Async / temporal invariants -->

*Category async_invariants marked N/A by drafter: The API is described as fully synchronous CRUD with no queues, background workers, schedulers, or eventually-consistent read paths; all operations are expected to be immediately consistent on response.*

<!-- Category: auth_boundaries — Authorization boundaries -->

### R5. IDOR — employee reads another employee's assignment commits

- **Given**: Employee A has assignments on Project P; Employee B is authenticated with a different identity
- **When**: Employee B GETs /api/assignments/{employeeIdA}/{projectId} and /api/assignments/{employeeIdA}/{projectId}/{commitDate}
- **Then**: Both return 403 Forbidden or 404; Employee A's commitEmpDesc, commitMgrDesc, and project data are not returned to Employee B
- **Priority**: HIGH
- **Estimated turns**: 2

### R6. IDOR — employee reads another employee's profile including salary

- **Given**: Employee A is authenticated; Employee B has a known employeeId with a salary field populated
- **When**: Employee A GETs /api/employees/{employeeIdB}
- **Then**: Response is 403 or 404; Employee B's salary, email, phone, and embedded credential reference are not returned to Employee A
- **Priority**: HIGH
- **Estimated turns**: 2

### R7. Employee attempts project mutation (create, update, delete)

- **Given**: An Employee-role user is authenticated; Project P exists
- **When**: The Employee-role user POSTs to /api/projects/save, PUTs to /api/projects/update, and DELETEs /api/projects/{P}
- **Then**: All three return 403 Forbidden; project state is unchanged after each attempt
- **Priority**: HIGH
- **Estimated turns**: 2

### R8. Non-admin reads credential collection or individual credential record

- **Given**: An Employee-role or Manager-role user is authenticated
- **When**: The user GETs /api/credentials, /api/credentials/{id}, and /api/credentials/username/{username}
- **Then**: All return 403 Forbidden; no credential records — including password hashes, role strings, or enabled flags — are returned to non-admin callers
- **Priority**: HIGH
- **Estimated turns**: 2

### R9. Role escalation via credential PUT

- **Given**: An Employee-role user is authenticated and knows their own credentialId C
- **When**: The user PUTs /api/credentials/update with body {credentialId: C, role: 'MANAGER'} or {credentialId: C, role: 'ADMIN'}
- **Then**: Response is 403 Forbidden; a subsequent admin GET /api/credentials/{C} confirms the role field is unchanged; the user cannot re-authenticate and obtain elevated privileges
- **Priority**: HIGH
- **Estimated turns**: 2

### R10. Cross-employee access to manager-project-data via IDOR

- **Given**: Employee A is authenticated; Employee B (a manager) has manager-project-data
- **When**: Employee A GETs /api/employees/data/manager-project-data/{employeeIdB}
- **Then**: Response is 403 or 404; Employee B's manager project list is not returned to Employee A
- **Priority**: MEDIUM
- **Estimated turns**: 2

### R11. Unauthenticated access to protected endpoints

- **Given**: No authentication token or session cookie is present
- **When**: Anonymous client GETs /api/employees, /api/projects, and /api/assignments
- **Then**: All return 401 Unauthorized; no resource data is included in the response body
- **Priority**: MEDIUM
- **Estimated turns**: 1

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R12. Malformed or invalid commitDate in path

- **Given**: Assignment endpoints accept commitDate as a path string expected in 'dd-MM-yyyyHH:mm:ss' format
- **When**: GET or DELETE /api/assignments/{employeeId}/{projectId}/{commitDate} is called with commitDate='not-a-date', '99-99-9999HH:mm:ss', or an empty/whitespace string
- **Then**: 400 Bad Request with an explanatory message; not 500, not NullPointerException stack trace, not a silent empty-200
- **Priority**: HIGH
- **Estimated turns**: 1

### R13. Non-numeric or negative ID values in path parameters

- **Given**: Endpoints accept employeeId, projectId, and id as path strings that are parsed to int32 internally
- **When**: Requests use employeeId='abc', employeeId='-1', projectId='0', or id='2147483648' (int32 overflow) in the path
- **Then**: 400 Bad Request for each case; not 500, not silent misrouting to an unintended record
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R14. SQL and script injection in username path parameter

- **Given**: GET /api/credentials/username/{username} and DELETE /api/credentials/username/{username} accept arbitrary strings
- **When**: username is set to "' OR '1'='1", "admin'--", or "<script>alert(1)</script>"
- **Then**: 400 or 404 for each payload; no credential records returned for injection strings; no 500 or stack trace that reveals query structure or table names
- **Priority**: HIGH
- **Estimated turns**: 1

### R15. Project with endDate before startDate

- **Given**: POST /api/projects/save accepts startDate and endDate as date strings
- **When**: Body contains startDate='31-12-2025' and endDate='01-01-2025' (end precedes start)
- **Then**: 400 Bad Request; project is not persisted; not 200 with logically invalid dates stored silently
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R16. Arbitrary or undefined role value in Credential body

- **Given**: POST /api/credentials/save or PUT /api/credentials/update accepts a free-text role field
- **When**: Admin saves a credential with role='SUPERADMIN' or role=''; then that credential authenticates via POST /api/authenticate
- **Then**: Either 400 at save time (invalid role rejected) or the role is stored but grants no access beyond defined roles; authentication with an undefined role must not silently grant admin-level access
- **Priority**: HIGH
- **Estimated turns**: 2

### R17. Null or missing required fields in assignment body

- **Given**: POST /api/assignments/save requires employeeId, projectId, and commitDate
- **When**: Body omits commitDate entirely, or sets employeeId=null, or sends an empty JSON object {}
- **Then**: 400 Bad Request for each variant; not 500 or NullPointerException; no partial record persisted
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R18. Oversize text fields in assignment body

- **Given**: POST /api/assignments/save accepts commitEmpDesc and commitMgrDesc as unbounded strings
- **When**: commitEmpDesc or commitMgrDesc is a 100,000-character string
- **Then**: 400 or 413 Payload Too Large; not 500 and not silent truncation that stores a corrupted or partial value
- **Priority**: LOW
- **Estimated turns**: 1

## Open Exploration

The authentication endpoint returns only {isEligible, username} with no token — probe whether the system uses session cookies or a hidden JWT header, and whether that mechanism can be bypassed or replayed. Critically, each resource exposes duplicate route pairs (e.g., POST /api/assignments and POST /api/assignments/save; DELETE /api/assignments/{e}/{p}/{d} and DELETE /api/assignments/delete/{e}/{p}/{d}) — test whether both variants of each pair enforce identical authorization rules, since divergent security logic on aliased routes is a systematic LLM-coauthored bug. Also probe cascading-delete behavior: deleting an Employee should cascade to their Assignments and Credential, or return a referential-integrity error — verify that orphan rows are not silently left behind. Finally, check whether GET /api/employees/data/employee-project-data/{employeeId} leaks project titles or statuses belonging to projects the requesting employee is not assigned to, and whether the commitMgrDesc field (intended for manager annotations) can be written by an employee-role caller.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*