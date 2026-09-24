# SimpleAudit Studio — Production Engineering Mission

The current `~/simpleaudit-studio` implementation is a **prototype/POC only**.

DO NOT continue treating the existing implementation as the desired architecture.

We are building a **mature, production-quality, self-hostable SimpleAudit Studio** that other organizations and researchers can clone/deploy and use for real AI audits.

The final system must be:

* production-oriented
* reproducible
* self-hostable
* easy to spin up
* maintainable by multiple developers
* observable
* secure by default
* well tested
* documented
* upgradeable
* capable of running long-lived and GPU-backed audit jobs
* usable by nontechnical users

Do not replace architectural components with toy substitutes merely because they are easier to run on this development machine.

In particular:

**Do NOT use**

* SQLite as the production database
* an in-process Python queue as the production queue
* process-local state for jobs
* fake/mock infrastructure in the primary deployment
* ad-hoc scenario versioning
* polling hacks when proper event/progress infrastructure exists

The development environment may have lightweight modes, but the canonical architecture and deployment must use the real production components.

---

# FIRST: STOP CODING AND RUN AN SDLC DESIGN PHASE

Before implementing further, inspect:

1. the existing SimpleAudit repository/core
2. the existing SimpleAudit Visualizer
3. `~/simpleaudit-studio`
4. `~/simpleaudit-experiment`
5. existing data formats/results
6. existing ModelAuditor interfaces
7. current deployment assumptions
8. existing tests and documentation

Do not rewrite working SimpleAudit functionality unnecessarily.

The platform should wrap and orchestrate the existing SimpleAudit engine and visualizer.

---

# USE SPECIALIZED SUBAGENTS

Act as the principal engineer / technical lead.

Spawn specialized subagents and give each an explicit responsibility.

At minimum create these roles:

## 1. Product / Requirements Agent

Own:

* user personas
* nontechnical audit workflow
* functional requirements
* acceptance criteria
* audit lifecycle
* scenario-library workflow
* comparison workflow
* administration workflow
* MVP vs later capabilities

Produce a requirements/specification document.

Do not implement code.

---

## 2. Architecture Agent

Own:

* system architecture
* domain model
* service boundaries
* deployment topology
* queue architecture
* worker architecture
* persistence
* artifact storage
* event/progress architecture
* API boundaries
* failure recovery
* idempotency
* scalability

Investigate current SOTA/open-source components before recommending custom implementation.

Produce ADRs for major decisions.

Prefer boring, mature infrastructure.

---

## 3. SimpleAudit Domain Agent

Study the existing SimpleAudit implementation deeply.

Own:

* Target → Auditor → Judge semantics
* existing result formats
* compatibility with ModelAuditor
* audit configuration
* reproducibility requirements
* metrics
* result aggregation
* visualizer integration

Its job is to prevent the platform team from accidentally changing SimpleAudit's scientific semantics.

---

## 4. Data / Versioning Agent

Own the data model for:

Scenario
ScenarioRevision
ScenarioSet
ScenarioSetVersion
ScenarioSetVersionItem
Model
ModelConfiguration
AuditRun
AuditRunScenario
AuditMetric
AuditArtifact

Critical invariant:

**Every AuditRun must reference immutable inputs.**

An audit must permanently capture:

* exact ScenarioSetVersion
* scenario revisions
* target configuration
* auditor configuration
* judge configuration
* prompts
* generation parameters
* model identifiers/revisions where available
* SimpleAudit version
* git commit
* relevant runtime metadata

Editing a scenario after it has been used MUST NOT modify historical audits.

Design migrations and database constraints that enforce this rather than relying only on application convention.

---

## 5. Queue / Distributed Systems Agent

Research and own the execution layer.

Evaluate Hatchet and serious alternatives based on actual SimpleAudit requirements.

Required capabilities:

* persistent jobs
* workers separate from web process
* retries
* cancellation
* timeout
* progress
* structured events
* concurrency limits
* worker pools
* GPU queues
* recovery after worker/server restart
* idempotency
* job history
* retry policies
* graceful shutdown
* backpressure

Do NOT implement an in-process queue as the production system.

A desired topology is approximately:

Web/API
↓
Workflow/Queue
↓
Workers
├── CPU
├── GPU
├── H200/GH200
└── External API

The web server must not need access to GPUs.

---

## 6. Backend/API Agent

Own the production backend.

Prefer Django + PostgreSQL unless architectural investigation establishes a strong reason otherwise.

Responsibilities:

* domain services
* REST/API contracts
* authentication
* authorization
* organizations/projects
* audit submission
* scenario management
* model registry
* comparison API
* queue integration
* SSE/event endpoint
* migrations
* validation
* OpenAPI

Keep business logic out of HTTP handlers.

---

## 7. Frontend / UX Agent

Design for **nontechnical users**.

Primary surfaces:

Dashboard
New Audit
Audit Queue
Audit Detail
Results
Compare
Scenario Library
Scenario Editor
Scenario Version History
Models
Audit Profiles
Administration

The normal user should never need to understand:

* JSON
* CLI arguments
* endpoint URLs
* queue internals
* worker IDs

Provide an Advanced section where appropriate.

Reuse the existing SimpleAudit Visualizer instead of rewriting it.

---

## 8. Observability / Evaluation Agent

Own:

* OpenTelemetry
* traces
* structured logs
* metrics
* correlation IDs
* audit/job IDs
* per-scenario execution traces
* model latency
* tokens
* failures
* retry visibility

Evaluate Langfuse integration rather than recreating LLM observability.

Conceptually trace:

AuditRun
→ Scenario execution
→ Target
→ Auditor
→ Judge

Make failures diagnosable.

---

## 9. Security Agent

Threat-model the platform.

Review:

* authentication
* authorization
* multi-user isolation
* secrets
* model API credentials
* prompt/scenario confidentiality
* uploads
* SSRF
* injection
* XSS
* CSRF
* dependency/supply-chain security
* container security
* audit logs
* API security

Secrets MUST NOT be persisted inside frozen audit configuration.

Store secret references, not credentials.

---

## 10. QA / Verification Agent

Develop the test strategy independently from implementation agents.

Require:

* unit tests
* domain invariant tests
* integration tests
* database tests
* queue/worker tests
* API tests
* migration tests
* browser E2E tests
* restart/recovery tests
* cancellation tests
* concurrency tests
* scenario-version reproducibility tests

Important test:

Create audit using ScenarioSet v1 → modify scenarios → create v2 → verify the historical audit still executes/displays exactly v1.

Also test worker death and restart.

---

## 11. DevOps / Release Agent

Own:

Dockerfiles
Docker Compose
configuration
health checks
migrations
CI
release workflow
versioning
backup/restore
upgrade documentation
deployment documentation
developer environment

A new user should eventually be able to run something approximately as simple as:

git clone ...
cp .env.example .env
docker compose up -d

and receive a functional SimpleAudit installation.

Do not require undocumented manual setup.

---

## 12. Independent Reviewer Agent

This agent MUST NOT implement features.

Periodically inspect the work from a fresh context.

Look for:

* POC shortcuts
* duplicated functionality
* architectural drift
* missing tests
* weak abstractions
* hidden coupling
* security issues
* unreproducible audits
* unnecessary complexity
* reinvented infrastructure

Challenge claims such as "production ready".

Require evidence.

---

# REFERENCE ARCHITECTURE TO INVESTIGATE

Do not blindly implement this, but use it as the baseline:

Browser
↓
SimpleAudit Web UI
↓
Django/API
↓
PostgreSQL
│
├── Scenario Library
├── Model Registry
├── Audit Runs
└── Metrics
↓
Hatchet/workflow system
↓
SimpleAudit workers
↓
Target → Auditor → Judge

Additional systems:

S3/MinIO
→ large artifacts/raw outputs

Langfuse/OpenTelemetry
→ traces/LLM observability

Existing SimpleAudit Visualizer
→ result exploration

The SimpleAudit database remains the source of truth for SimpleAudit domain objects.

Do not make Hatchet or Langfuse the authoritative SimpleAudit database.

---

# CORE DOMAIN RULE

Treat an audit like a scientific experiment.

Once submitted, its effective inputs are immutable.

AuditRun should capture a frozen reproducibility manifest.

Example conceptual manifest:

simpleaudit_version
git_commit

scenario_set:
id
version
content_hash

target:
provider
model
model_revision
parameters

auditor:
...

judge:
...

timestamps
runtime metadata

Historical results must remain interpretable years later.

---

# COMPARISON ENGINE

Design comparisons across:

* AuditRun
* target model
* auditor model
* judge model
* ScenarioSet
* ScenarioSetVersion
* scenario
* category
* tags
* model revision
* quantization
* generation parameters
* SimpleAudit version

The UI MUST identify potentially invalid comparisons.

Example:

"Scenario versions differ: v4 vs v7."

Provide intersection-based comparison where appropriate:

"Compare only identical scenario revisions."

Do not silently compare incompatible experiments.

---

# PROGRESS

Progress must be durable.

A browser reconnect or server restart must not lose it.

Represent structured state, not merely a percentage:

queued
preparing
target_execution
auditing
judging
aggregation
report_generation
completed
failed
cancelled

and counters such as:

total
completed
successful
failed
retried

Expose progress to the browser through an appropriate mechanism such as SSE.

---

# SCENARIO LIBRARY

Users must be able to:

create scenarios
edit scenarios
tag/categorize scenarios
create scenario sets
create new set versions
inspect history
duplicate/fork sets
import/export scenarios
launch an audit from a set

Scenario edits create revisions.

Published/used versions are immutable.

Never mutate historical experiment inputs.

---

# MODEL REGISTRY

Nontechnical users choose:

"Qwen 3.8 27B"

not:

"http://server42:8765/v1"

Model registry should separate:

Model identity
Model endpoint
Credentials
Default generation parameters
Capabilities

AuditRun receives a frozen safe snapshot.

Credentials remain secret references.

---

# DO NOT OVERENGINEER

Production quality does NOT mean microservices.

Prefer:

one main application
PostgreSQL
one workflow system
worker processes
object storage when needed
observability

Do not introduce:

* Kubernetes
* Kafka
* ClickHouse
* lakeFS
* Elasticsearch
* service mesh

unless measured requirements establish the need.

A good Docker Compose deployment is preferable to unnecessary distributed infrastructure.

---

# SKILLS

Before implementation, install/load appropriate Agent Skills covering:

* spec-driven development
* planning/task breakdown
* software architecture
* database/schema design
* API/interface design
* frontend UI engineering
* accessibility
* test-driven development
* Playwright/browser E2E testing
* QA methodology
* secure software engineering
* security review
* observability/instrumentation
* Docker/containerization
* CI/CD
* platform engineering
* release engineering
* technical documentation
* systematic debugging
* performance/load testing
* dependency/supply-chain security

Use skills as procedures, not as decoration.

---

# SDLC PROCESS

Use this order:

DISCOVER
↓
REQUIREMENTS
↓
ARCHITECTURE
↓
ADRs
↓
DATA MODEL
↓
THREAT MODEL
↓
TEST STRATEGY
↓
IMPLEMENTATION PLAN
↓
IMPLEMENT VERTICAL SLICES
↓
INDEPENDENT REVIEW
↓
INTEGRATION TESTING
↓
E2E TESTING
↓
LOAD/FAILURE TESTING
↓
DOCUMENTATION
↓
RELEASE CANDIDATE

Do not skip directly to IMPLEMENTATION.

---

# PHASE 0 DELIVERABLE

Do NOT start the major rewrite immediately.

First create:

docs/product-requirements.md
docs/architecture.md
docs/domain-model.md
docs/threat-model.md
docs/test-strategy.md
docs/deployment.md

docs/adr/
001-web-framework.md
002-database.md
003-job-system.md
004-scenario-versioning.md
005-artifact-storage.md
006-observability.md

ROADMAP.md

Then have the independent reviewer subagent review these documents.

Resolve important findings.

Only after that should implementation begin.

---

# DEFINITION OF DONE

"Works in my browser" is NOT the definition of done.

A feature is complete only when:

1. requirements/acceptance criteria exist
2. architecture/domain implications are considered
3. implementation is complete
4. migrations exist
5. automated tests exist
6. integration tests pass
7. relevant E2E tests pass
8. security implications were considered
9. observability exists
10. documentation exists
11. another agent independently reviewed it
12. no POC substitute remains in the production path

Keep the current prototype only as useful exploratory work.

Now begin with Phase 0 and coordinate the subagents.
Do not begin another "build everything quickly" pass.
