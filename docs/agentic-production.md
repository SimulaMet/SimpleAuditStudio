# Agentic Auditing — Production Readiness

Status: stable (T01-T25 complete, Phase 1-3 tested)  
Date: 2026-10-06

## Deployment Checklist

### Before Launch

- [ ] **OTLP Configuration**  
  Set `SIMPLEAUDIT_CHAT_OTLP=true` (embedded) or configure external Tempo (compose).  
  Verify trace export works: check `infra/tracing.py` and Tempo retention.

- [ ] **Evidence Retention**  
  Agentic evaluation requires full span data. Set retention ≥ scenario TTL.  
  (default: 7 days in embedded mode)

- [ ] **Judge Composition**  
  Test `audits/agentic/judge_composition.py::compose_agentic_judge()` with your judge specs.  
  Verify base judge + agentic dimensions produce valid prompts.

- [ ] **Scenario Validation**  
  Scenarios with `metadata.agentic` keys run v2 schema validation (`schema_v2.py`).  
  Test with `audits/agentic/schema_v2.py::validate_agentic_metadata()`.  
  Use `migrate_v1_to_v2()` to safely upgrade existing v1 scenarios.

- [ ] **Worker Pool**  
  Agentic audits are CPU-intensive (normalization, checks, judge extension).  
  Add dedicated worker pool or scale existing: `WORKER_POOL=agentic`.  
  Monitor: `infra/worker.py` task labels.

### Monitoring & Observability

**Key Metrics** (emit to Prometheus)

```python
# infra/tracing.py or custom metrics
audit.agentic.checks.run (gauge)         # checks executed per run
audit.agentic.verdict.status (histogram) # PASS/FAIL/INCONCLUSIVE
audit.agentic.evidence.coverage (gauge)  # trace_ids collected
audit.agentic.matcher.errors (counter)   # matcher validation failures
```

**Dashboards**

- Checks pass rate by category (tools, trajectory, retrieval, etc.)
- Verdict distribution (PASS%, FAIL%, INCONCLUSIVE%)
- Evidence coverage: % runs with trace_ids, selected_spans
- Matcher error rate (malformed arguments in scenario definitions)

**Alerts**

- Agentic evaluation ERROR rate > 2% (indicates bugs in checks or schema)
- Evidence coverage < 80% (OTLP or trace selection issues)
- Verdict INCONCLUSIVE > 50% (incomplete or missing evidence)
- Worker lag > 5 min (insufficient agentic pool)

### Configuration

**Environment**

```bash
# .env for production compose
SIMPLEAUDIT_CHAT_OTLP=true
OTLP_EXPORTER_OTLP_ENDPOINT=http://tempo:4317  # or external
SIMPLEAUDIT_EVIDENCE_RETENTION_DAYS=7
SIMPLEAUDIT_AGENTIC_POOL_SIZE=4  # dedicated worker pool

# Optional: enable result streaming for large audits
SIMPLEAUDIT_AGENTIC_STREAM_RESULTS=true
```

**Worker Configuration** (infra/worker.py)

Ensure Hatchet tasks for agentic runs label correctly:

```python
# Task submission in audits/views.py or infra/ui.py
await hatchet.task.trigger_run(
    "audit.scenario_execute",
    input={"run_id": run_id, ...},
    labels={"WORKER_POOL": "agentic" if scenario.metadata.agentic else "cpu"}
)
```

### Troubleshooting

**"Invalid agentic schema" on scenario execute**

- Check `schema_v2.py::validate_agentic_metadata()` output
- Verify matchers in `tools.allowed` use valid operators (see `matchers.py`)
- Inspect `scenario.metadata.agentic` JSON for syntax errors

**"INCONCLUSIVE" verdict**

- Missing trace_ids: Check OTLP export (Tempo, OTel span processors)
- Missing selected_spans: Check `evidence_resolver` in `infra/engine.py`
  runs after target execution
- Empty trajectory: Verify agent produces tool calls or errors (no trace = no audit)

**Worker timeouts**

- Agentic runs may exceed default 30s: increase `HATCHET_TASK_TIMEOUT=120` (seconds)
- Check `orchestrate_agentic_audit()` perf on large trajectories (1000+ steps)

**Matcher validation errors**

- Use `audits/agentic/matchers.py::match_arguments()` in tests before deploying
- Register custom predicates with `register_predicate()` in initialization code

### Data Cleanup

Agentic runs create large `AuditEvent` and `trace_evidence` rows.

**Retention policy** (PostgreSQL)

```sql
-- Retain 90 days of agentic runs; delete older events
DELETE FROM audits_auditevent
WHERE audit_run_id IN (
  SELECT id FROM audits_auditrun
  WHERE created_at < now() - interval '90 days'
  AND scenario.metadata ->> 'agentic' IS NOT NULL
)
```

**SQLite (development)** — automatic: delete dev.sqlite3 to reset.

### Rollback Plan

If agentic evaluation has bugs:

1. Set `SIMPLEAUDIT_AGENTIC_DISABLED=true` in `.env`
2. Re-run affected audits (they will skip agentic evaluation)
3. Existing results remain under `agentic_evaluation` for review
4. Fix `audits/agentic/` code, re-deploy, unset `SIMPLEAUDIT_AGENTIC_DISABLED`

### Testing

**Local verification** (before production)

```bash
# Full test suite (excluding embedded_hatchet)
uv run pytest -m "not embedded_hatchet" -x

# Agentic-specific tests
uv run pytest audits/agentic/ -v

# E2E flow
uv run pytest audits/test_agentic_e2e.py -v
```

**Load testing**

```bash
# Simulate 100 concurrent agentic audits (requires Hatchet + worker)
pytest tests/performance/agentic_load.py --workers=4 --runs=100
```

### Support & Escalation

**Common issues → resolution**

| Issue | Check | Fix |
|-------|-------|-----|
| Checks always PASS | Tool validation disabled? | Set `tools.enabled: true` in scenario metadata |
| Verdict INCONCLUSIVE | No trace_ids | Verify agent exports OTLP; check `evidence_resolver` |
| Matcher error | Invalid arguments | Test with `match_arguments()` before scenario save |
| Worker lag | Pool too small? | Scale `SIMPLEAUDIT_AGENTIC_POOL_SIZE` |
| Out of memory | Large trajectory | Split long runs; increase worker heap |

## Next: Advanced Scenarios

Once production stable:

- **Custom checks**: Extend `audits/agentic/checks/` with domain-specific validations
- **Custom matchers**: Register predicates in `matchers.py` (see `register_predicate`)
- **Trajectory replay**: Store execution videos for post-audit inspection (`audits/agentic/adapters.py`)
- **Multi-agent workflows**: Extend `schema.py` to track agent-to-agent handoffs
