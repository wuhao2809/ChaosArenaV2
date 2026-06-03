# ChaosArena MVP

LLM-agent-driven evaluation pipeline. An agent reads a markdown spec describing acceptance criteria, probes a deployed target service via a small tool API, and emits a PASS/FAIL verdict.

Status: **MVP** — minimal closed-loop demo. Tool API has 3 primitives; spec is single-tier; no AWS yet.

## Layout

```
mvp/
├── agent/              Python agent runner (Bedrock-backed Claude)
├── target/             Python stub server, Flask (with optional BUG_MODE for demo)
└── specs/              Markdown specs the agent reads
```

## Run locally

```bash
# Terminal A — start the stub server (good version)
cd target
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python stub_server.py
# → listening on :8080

# Terminal A' — or with a deliberate bug for the FAIL demo
BUG_MODE=race .venv/bin/python stub_server.py

# Terminal B — run the agent (Bedrock backend)
cd agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export AWS_PROFILE=008209411721_myisb_IsbUsersPS  # NEU Sandbox SSO profile
export AWS_REGION=us-west-2
.venv/bin/python main.py --spec ../specs/tasktracker.md --target http://localhost:8080
```

Output streams to the terminal: each agent turn, each tool call, each response, and the final verdict.

## Component status

- [x] Directory scaffold
- [x] Python stub server (Flask) — with `BUG_MODE=race`
- [x] Fake spec (`tasktracker.md`)
- [x] System prompt
- [x] Python agent runner (`http_call` only) — Bedrock verified
- [x] Local smoke test passes — good version → PASS verdict in 10 turns
- [ ] `parallel_n`, `record_event` tools (Tue: needed to detect race-mode bug)
- [ ] Dockerfile for agent (Tue)
- [ ] Terraform for ECR / ECS / Fargate / ALB / IAM / CloudWatch (Tue)
- [ ] AWS Bedrock-backed agent task on Fargate (Tue)
- [ ] AWS demo via `aws ecs run-task` + `aws logs tail` (Tue)

## Related docs

See [`../Doc/mvp_scope_5_13.md`](../Doc/mvp_scope_5_13.md) for the full development plan.
