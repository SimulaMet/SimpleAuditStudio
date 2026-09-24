# SimpleAudit Studio — Product Requirements

Status: Phase 0 draft for independent review  
Date: 2026-09-22  
Owner: Product / Requirements role  
Implementation status: no production implementation approved yet

## 1. Purpose

SimpleAudit Studio should let an organization run, track, compare, and reproduce AI safety audits without requiring users to understand Python scripts, JSON payloads, queue internals, model endpoints, or worker processes.

The platform wraps the existing SimpleAudit engine and visualizer. It does not replace SimpleAudit’s scientific semantics. Its product responsibilities are:

- manage auditable scenario libraries with immutable versions
- register models by human-readable identity while keeping credentials secret
- submit durable audit jobs
- show structured progress and failure state
- preserve every audit’s exact inputs for later reproduction
- compare audits only when scientifically meaningful
- provide administration sufficient for a self-hosted multi-user deployment

## 2. Primary personas

### 2.1 Nontechnical auditor

A researcher, regulator, clinician, policy analyst, or operations staff member who wants to evaluate whether a model behaves acceptably on a set of scenarios.

Needs:

- choose a scenario set and version from a list
- choose target, auditor, and judge models by display name
- start an audit
- see what stage the audit is in and how many scenarios remain
- inspect results in plain language
- export or share a reproducibility manifest
- compare two audits and understand warnings about invalid comparisons

Must never need to know:

- REST endpoint URLs
- JSON request bodies
- queue names or worker IDs
- database schema
- raw API keys
- command-line arguments

### 2.2 Technical operator

An engineer or data scientist who configures models, imports scenario packs, monitors workers, and troubleshoots failures.

Needs:

- create/edit model endpoints
- attach secret references, never raw secrets
- import/export scenario sets
- inspect per-scenario traces and token usage
- retry failed runs with identical frozen inputs
- cancel long-running jobs
- view logs, metrics, and trace links
- diagnose GPU/API/local inference failures

### 2.3 Administrator

A person responsible for a self-hosted deployment.

Needs:

- manage users and roles
- configure organizations/projects if enabled
- rotate secrets
- monitor system health
- back up/restore PostgreSQL and object storage
- upgrade the platform without losing historical audits
- audit administrative actions

## 3. Core user workflows

### 3.1 Create and run an audit

Acceptance criteria:

1. User opens **New Audit**.
2. User selects a scenario set and a specific published version.
3. User selects target, auditor, and judge models from display names.
4. User enters the audit hyperparameters directly.
5. System validates that all selected resources exist and are enabled.
6. System creates an `AuditRun` with status `queued`.
7. System freezes:
   - scenario set version ID and content hash
   - each scenario revision ID and content hash
   - target configuration snapshot
   - auditor configuration snapshot
   - judge configuration snapshot
   - generation parameters
   - SimpleAudit version and git commit
   - runtime metadata
8. Worker executes the audit through the existing SimpleAudit pipeline.
9. Browser receives durable progress events via SSE or equivalent.
10. Completed run shows summary metrics and per-scenario results.

### 3.2 Monitor an audit

Acceptance criteria:

- Statuses include at least:
  - `queued`
  - `preparing`
  - `target_execution`
  - `auditing`
  - `judging`
  - `aggregation`
  - `report_generation`
  - `completed`
  - `failed`
  - `cancelled`
- Progress includes counters:
  - total
  - completed
  - successful
  - failed
  - retried
- Reconnecting the browser does not lose progress.
- Restarting the web server does not lose progress.
- A failed run exposes a human-readable error and a link to detailed diagnostics.

### 3.3 Compare audits

Acceptance criteria:

- Users can select two or more completed audits.
- Comparison displays:
  - target model
  - auditor model
  - judge model/profile
  - scenario set and version
  - SimpleAudit version
  - severity distribution
  - pass/fail rates
  - token usage
  - latency where available
- UI must warn when comparison may be invalid because:
  - scenario versions differ
  - judges differ
  - auditors differ
  - target parameterization differs materially
  - SimpleAudit versions differ
- UI must offer intersection mode:
  - compare only identical scenario revisions
  - show excluded scenarios and reason for exclusion
- System must not silently compare incompatible experiments.

### 3.4 Manage scenario library

Acceptance criteria:

- Users can create, edit, tag, categorize, duplicate, and archive scenarios.
- Edits create new scenario revisions; old revisions remain immutable.
- Scenario sets contain ordered scenario selections.
- Publishing a set creates a new immutable `ScenarioSetVersion`.
- Users can inspect version history and diff revisions.
- Users can fork/duplicate a set.
- Users can import/export scenario sets in a stable file format.
- Launching an audit requires selecting a published version, not a mutable set.

### 3.5 Manage models

Acceptance criteria:

- Model registry separates:
  - model identity/display name
  - provider
  - endpoint URL
  - model identifier/revision
  - default generation parameters
  - capabilities
  - secret reference
- Users never enter raw API keys into audit forms.
- Raw secrets are stored outside the audit manifest.
- Audit snapshots store secret references only.
- Disabled models cannot be selected for new audits.
- Existing audits remain valid after a model endpoint changes.

## 4. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| PR-001 | Submit audit from UI with no JSON editing required | Must have |
| PR-002 | Freeze all audit inputs at submission time | Must have |
| PR-003 | Persist job state outside web process memory | Must have |
| PR-004 | Execute audits on separate worker processes | Must have |
| PR-005 | Support CPU and GPU worker pools | Must have |
| PR-006 | Support durable cancellation that survives web/API restart | Must have |
| PR-007 | Support retry by creating a new run with identical frozen inputs; original run remains terminal | Must have |
| PR-008 | Expose durable structured progress | Must have |
| PR-009 | Store per-scenario results in PostgreSQL | Must have |
| PR-010 | Store large raw artifacts in object storage | Must have |
| PR-011 | Provide reproducibility manifest download | Must have |
| PR-012 | Compare audits with validity warnings | Must have |
| PR-013 | Version scenario sets immutably | Must have |
| PR-014 | Register models by display name | Must have |
| PR-015 | Authenticate users | Must have |
| PR-016 | Enforce role-based authorization | Must have |
| PR-017 | Log administrative actions | Must have |
| PR-018 | OpenTelemetry tracing for audit lifecycle | Should have |
| PR-019 | Langfuse or equivalent LLM observability | Should have |
| PR-020 | Multi-organization isolation | Later |
| PR-021 | SSO/SAML/OIDC | Later |
| PR-022 | Billing/metering | Out of scope initially |

## 5. Nonfunctional requirements

### 5.1 Reproducibility

Every completed audit must be interpretable years later. The manifest must identify enough information to explain exactly what was tested, even if live models, endpoints, or scenario sets later change.

### 5.2 Durability

A crash or restart of any component must not silently lose:

- queued jobs
- running job state
- partial results
- progress counters
- event history
- artifact references

Partial results may be discarded only if explicitly marked incomplete and superseded by a retry.

### 5.3 Security

- Secrets must not appear in manifests, logs, API responses, browser storage, or client-side JavaScript.
- Authorization must be enforced server-side.
- Uploaded/imported scenario files must be validated.
- Model endpoints must be treated as untrusted network targets.
- Audit content may be confidential and must be isolated by project/organization.

### 5.4 Usability

Primary workflows must be completable without reading source code or API docs. Advanced controls may exist but must be secondary.

### 5.5 Observability

Every audit and job must carry correlation identifiers through:

- API request
- workflow execution
- worker process
- target call
- auditor call
- judge call
- artifact write
- result persistence

### 5.6 Portability

Canonical deployment must be possible with Docker Compose on a single host, with optional additional worker hosts joining the same queue/storage.

## 6. MVP scope

MVP includes:

- single organization
- local authentication
- roles: admin, auditor, viewer
- scenario library with immutable versions
- model registry with secret references
- audit submission
- durable queue/workflow execution
- CPU and GPU worker labels
- progress via SSE backed by durable events
- results and comparison
- reproducibility manifest
- Docker Compose deployment
- automated tests for critical invariants

MVP excludes:

- Kubernetes
- Kafka
- Elasticsearch
- ClickHouse
- service mesh
- multi-tenant billing
- public marketplace
- arbitrary plugin execution

## 7. Definition of done

A feature is complete only when:

1. acceptance criteria exist
2. architecture/domain impact is documented
3. implementation is complete
4. migrations exist
5. unit/integration tests pass
6. relevant E2E tests pass
7. security implications reviewed
8. observability exists
9. documentation updated
10. independent reviewer has checked the work
11. no POC substitute remains in the production path

## 8. Success metrics

- New user can deploy with `docker compose up -d` and run a seeded audit.
- An auditor can submit an audit without touching JSON or CLI.
- After web server restart, progress remains visible.
- After worker restart, queued jobs resume or fail visibly.
- Historical audit still references original scenario version after edits.
- Comparison UI flags mismatched judges/scenarios.
- No secret appears in manifest, logs, or API response.
- Critical domain invariant tests pass in CI.
