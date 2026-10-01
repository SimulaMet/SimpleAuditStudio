---
name: parallel-orchestrate
description: Parallel-first orchestration convention for substantial tasks
---

Use parallel subagents aggressively for independent work. Optimize for
wall-clock time, not token count. Treat model calls as cheap. Keep yourself
as the coordinator and integrator. Verify claims with repository evidence
and executable checks before concluding.

For every substantial task:
1. Launch 2-3 Researcher subagents in parallel for independent angles
   (simplest implementation, architecture-compatible implementation,
   hidden risks).
2. Integrate the best findings and implement.
3. Launch Verifier, Test Reviewer, and Regression Reviewer in parallel.
4. Fix confirmed findings and re-verify until there are no actionable
   failures.

Keep the delegation tree wide, not deep: orchestrator -> workers/reviewers.
Do not nest subagents more than one level.
