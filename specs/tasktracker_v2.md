# TaskTracker — System Spec (local stub)

## Description

A minimal task-tracking REST API. Clients can check service health, create tasks, fetch tasks by id, and list all tasks.

## Required Test Categories

### R1. Health endpoint responds successfully

- **Given**: The service is running
- **When**: `GET /health` is called
- **Then**: The response is `200` and the JSON body contains `"status": "ok"`

### R2. Read-your-writes for created task

- **Given**: A valid task title
- **When**: `POST /tasks` creates a task and the returned `task_id` is immediately fetched with `GET /tasks/:task_id`
- **Then**: The POST returns `201`; the GET returns `200`; the fetched task has the same `task_id` and `title`

### R3. No 5xx responses under concurrent task creation

- **Given**: A valid task title payload
- **When**: 20 concurrent `POST /tasks` requests are issued
- **Then**: All 20 responses are 2xx; no response is 5xx

### R4. List endpoint includes previously created tasks

- **Given**: At least one task has already been created successfully during the run
- **When**: `GET /tasks` is called
- **Then**: The response is `200`; the body is a JSON array; it includes every previously created `task_id`

### R5. Client errors return correct status codes

- **Given**: Invalid client inputs
- **When**: `POST /tasks` is called with a missing or empty `title`, and `GET /tasks/:task_id` is called with a non-existent id
- **Then**: Missing or empty `title` returns `400`; non-existent task lookup returns `404`; none of these client-error cases return `500`

## Open Exploration

Probe for any additional issues the required Rs do not enumerate, especially inconsistent error payloads, malformed JSON handling, or list endpoint instability after concurrent writes.

## Out of Scope

- Authentication or authorization
- Persistence across service restarts
- Update and delete operations
- Pagination
