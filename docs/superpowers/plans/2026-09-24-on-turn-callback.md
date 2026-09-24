# On-Turn Callback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class `on_turn` callback support to SimpleAudit's `ModelAuditor.run_scenario()` and `AuditExperiment.run_scenario_reps()`, then remove the monkey-patch from SimpleAudit Studio.

**Architecture:** The `on_turn` callback fires at each phase boundary (auditor probe generation, target response, judge evaluation) with `(turn_index, max_turns, role)` where role is `"auditor"`, `"target"`, or `"judge"`. This is threaded through `ModelAuditor.run_scenario()` → `AuditExperiment._run_single_rep()` → `AuditExperiment.run_scenario_reps()`. Studio's `engine.py` will use the native `run_scenario_reps()` path instead of building a manual rep loop with patched instances.

**Tech Stack:** Python 3.11+, asyncio, pytest

**Spec:** N/A (this is a refactoring/cleanup task, not a new feature)

## Global Constraints

- No breaking changes to existing public APIs
- All existing tests must pass
- The `on_turn` callback is optional (defaults to `None`)
- Callbacks are called synchronously from within the asyncio event loop — callers must not perform blocking I/O inside them
- Studio's frozen audit manifest semantics are unchanged

---

### Task 1: Add `on_turn` parameter to `ModelAuditor.run_scenario()`

**Files:**
- Modify: `~/simpleaudit/simpleaudit/model_auditor.py:793-982` (the `run_scenario` method)
- Test: `~/simpleaudit/tests/test_model_auditor.py` (add new test class)

**Interfaces:**
- Consumes: Existing `ModelAuditor.run_scenario()` signature
- Produces: `on_turn: Optional[Callable[[int, int, str], None]] = None` parameter; callback invoked at each phase boundary with `(turn_index, max_turns, role)`

- [ ] **Step 1: Write the failing test**

```python
# Add to ~/simpleaudit/tests/test_model_auditor.py

class TestOnTurnCallback:
    """Tests for the on_turn callback in run_scenario."""

    def test_on_turn_called_at_each_phase(self):
        """on_turn should be called once per turn for auditor, target, and judge."""
        calls = []

        def on_turn(turn_index, max_turns, role):
            calls.append((turn_index, max_turns, role))

        # Mock the async methods to avoid real LLM calls
        with patch.object(ModelAuditor, "_generate_probe_async", new_callable=AsyncMock) as mock_probe, \
             patch.object(ModelAuditor, "_call_async", new_callable=AsyncMock) as mock_call, \
             patch.object(ModelAuditor, "_judge_conversation_async", new_callable=AsyncMock) as mock_judge:
            
            mock_probe.return_value = ("probe text", 10, 20)
            mock_call.return_value = ("response text", 30, 40)
            mock_judge.return_value = ({"severity": "pass"}, 50, 60)

            auditor = ModelAuditor(
                model="test-model",
                provider="openai",
                judge_model="judge-model",
                judge_provider="openai",
                api_key="test-key",
                show_progress=False,
                verbose=False,
            )

            result = asyncio.run(auditor.run_scenario(
                name="test-scenario",
                description="test description",
                expected_behavior=["should do X"],
                test_prompt="initial prompt",
                max_turns=2,
                language="English",
                on_turn=on_turn,
            ))

        # Should be called 3 times per turn (auditor, target, judge) × 2 turns + 1 judge call
        # Turn 0: auditor (if no test_prompt), target, judge
        # Turn 1: auditor, target, judge
        # But test_prompt skips auditor on turn 0
        assert len(calls) >= 3  # At minimum: target, judge for turn 0
        roles = [c[2] for c in calls]
        assert "target" in roles
        assert "judge" in roles
        # If test_prompt is provided, auditor is skipped on turn 0
        if "test_prompt" in locals():
            assert calls[0][2] == "target"  # First call is target, not auditor

    def test_on_turn_not_called_when_none(self):
        """When on_turn is None, no callback should fire (no error)."""
        with patch.object(ModelAuditor, "_generate_probe_async", new_callable=AsyncMock) as mock_probe, \
             patch.object(ModelAuditor, "_call_async", new_callable=AsyncMock) as mock_call, \
             patch.object(ModelAuditor, "_judge_conversation_async", new_callable=AsyncMock) as mock_judge:
            
            mock_probe.return_value = ("probe", 10, 20)
            mock_call.return_value = ("response", 30, 40)
            mock_judge.return_value = ({"severity": "pass"}, 50, 60)

            auditor = ModelAuditor(
                model="test-model",
                provider="openai",
                judge_model="judge-model",
                judge_provider="openai",
                api_key="test-key",
                show_progress=False,
                verbose=False,
            )

            # Should not raise
            result = asyncio.run(auditor.run_scenario(
                name="test",
                description="desc",
                max_turns=1,
                language="English",
                on_turn=None,
            ))

    def test_on_turn_receives_correct_turn_index(self):
        """turn_index should increment correctly across turns."""
        calls = []

        def on_turn(turn_index, max_turns, role):
            calls.append((turn_index, role))

        with patch.object(ModelAuditor, "_generate_probe_async", new_callable=AsyncMock) as mock_probe, \
             patch.object(ModelAuditor, "_call_async", new_callable=AsyncMock) as mock_call, \
             patch.object(ModelAuditor, "_judge_conversation_async", new_callable=AsyncMock) as mock_judge:
            
            mock_probe.return_value = ("probe", 10, 20)
            mock_call.return_value = ("response", 30, 40)
            mock_judge.return_value = ({"severity": "pass"}, 50, 60)

            auditor = ModelAuditor(
                model="test-model",
                provider="openai",
                judge_model="judge-model",
                judge_provider="openai",
                api_key="test-key",
                show_progress=False,
                verbose=False,
            )

            asyncio.run(auditor.run_scenario(
                name="test",
                description="desc",
                max_turns=3,
                language="English",
                on_turn=on_turn,
            ))

        # Extract turn indices for target calls
        target_calls = [(idx, role) for idx, role in calls if role == "target"]
        turn_indices = [idx for idx, _ in target_calls]
        assert turn_indices == [0, 1, 2]  # One target call per turn
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/simpleaudit && python -m pytest tests/test_model_auditor.py::TestOnTurnCallback -v`
Expected: FAIL with "unexpected keyword argument 'on_turn'"

- [ ] **Step 3: Write minimal implementation**

Modify `~/simpleaudit/simpleaudit/model_auditor.py`:

1. Add `on_turn` parameter to `run_scenario()` signature (line ~793):
```python
async def run_scenario(
    self,
    name: str,
    description: str,
    expected_behavior: Optional[List[str]] = None,
    test_prompt: Optional[str] = None,
    file_uri: Optional[Union[str, List[str]]] = None,
    documents: Optional[List[Union[str, Dict[str, Any]]]] = None,
    judge_notes: Optional[List[str]] = None,
    max_turns: Optional[int] = None,
    language: str = "English",
    pbar_audit: Optional[tqdm] = None,
    pbar_judge: Optional[tqdm] = None,
    max_workers: Optional[int] = None,
    scenario_meta: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    target_params: Optional[Dict[str, Any]] = None,
    judge_params: Optional[Dict[str, Any]] = None,
    auditor_params: Optional[Dict[str, Any]] = None,
    on_turn: Optional[Callable[[int, int, str], None]] = None,  # NEW
) -> AuditResult:
```

2. Add helper method to invoke the callback safely:
```python
def _fire_on_turn(self, turn_index: int, max_turns: int, role: str) -> None:
    """Invoke the on_turn callback if set. Never raises."""
    if self.on_turn is not None:
        try:
            self.on_turn(turn_index, max_turns, role)
        except Exception:
            # Log but don't crash the audit
            self._log(f"on_turn callback failed: {traceback.format_exc()}")
```

3. Store `on_turn` in `__init__` (add after line ~310):
```python
self.on_turn = on_turn
```

4. Add `on_turn` parameter to `__init__` signature (after `auditor_params`):
```python
def __init__(
    self,
    model: str,
    provider: str,
    judge_model: str,
    judge_provider: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    system_prompt: Optional[str] = None,
    judge_api_key: Optional[str] = None,
    judge_base_url: Optional[str] = None,
    auditor_model: Optional[str] = None,
    auditor_provider: Optional[str] = None,
    auditor_api_key: Optional[str] = None,
    auditor_base_url: Optional[str] = None,
    judge: Optional[str] = None,
    probe_prompt: Optional[str] = None,
    judge_prompt: Optional[str] = None,
    judge_response_schema: Optional[Dict[str, Any]] = None,
    judge_fields: Optional[List[str]] = None,
    json_format: bool = True,
    max_turns: int = 5,
    verbose: bool = False,
    show_progress: bool = True,
    max_retries: int = 2,
    retry_backoff: float = 0.5,
    kwargs: Optional[Dict[str, Any]] = None,
    judge_kwargs: Optional[Dict[str, Any]] = None,
    target_kwargs: Optional[Dict[str, Any]] = None,
    auditor_kwargs: Optional[Dict[str, Any]] = None,
    judge_postprocess: Optional[Callable[..., Dict[str, Any]]] = None,
    params: Optional[Dict[str, Any]] = None,
    target_params: Optional[Dict[str, Any]] = None,
    judge_params: Optional[Dict[str, Any]] = None,
    auditor_params: Optional[Dict[str, Any]] = None,
    on_turn: Optional[Callable[[int, int, str], None]] = None,  # NEW
):
```

5. Fire the callback at each phase boundary in `run_scenario()`:

After probe generation (line ~850):
```python
probe, a_in, a_out = await self._generate_probe_async(...)
auditor_input_tokens += a_in
auditor_output_tokens += a_out
probe = ModelAuditor.strip_thinking(probe)
self._fire_on_turn(turn, turns, "auditor")  # NEW
```

After target response (line ~870):
```python
response, t_in, t_out = await self._call_async(...)
target_input_tokens += t_in
target_output_tokens += t_out
response = ModelAuditor.strip_thinking(response)
self._fire_on_turn(turn, turns, "target")  # NEW
```

After judge evaluation (line ~930):
```python
judgment, j_in, j_out = await self._judge_conversation_async(...)
judge_input_tokens += j_in
judge_output_tokens += j_out
self._fire_on_turn(turns - 1, turns, "judge")  # NEW (judge runs once after all turns)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/simpleaudit && python -m pytest tests/test_model_auditor.py::TestOnTurnCallback -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/simpleaudit
git add simpleaudit/model_auditor.py tests/test_model_auditor.py
git commit -m "feat: add on_turn callback to ModelAuditor.run_scenario

Adds first-class progress hook that fires at each phase boundary
(auditor probe generation, target response, judge evaluation) with
(turn_index, max_turns, role). Callbacks are optional and never
crash the audit if they raise."
```

---

### Task 2: Forward `on_turn` through `AuditExperiment.run_scenario_reps()`

**Files:**
- Modify: `~/simpleaudit/simpleaudit/experiment.py:332-425` (the `run_scenario_reps` method)
- Modify: `~/simpleaudit/simpleaudit/experiment.py:301-330` (the `_run_single_rep` method)
- Test: `~/simpleaudit/tests/test_experiment_streaming.py` (add new test class)

**Interfaces:**
- Consumes: `ModelAuditor.run_scenario(on_turn=...)` from Task 1
- Produces: `on_turn: Optional[Callable[[int, int, str], None]] = None` parameter on `run_scenario_reps()`; forwarded to each rep's `ModelAuditor`

- [ ] **Step 1: Write the failing test**

```python
# Add to ~/simpleaudit/tests/test_experiment_streaming.py

class TestOnTurnForwarding:
    """Tests that on_turn is forwarded through run_scenario_reps."""

    def test_on_turn_forwarded_to_each_rep(self):
        """on_turn should be called for each rep's execution."""
        calls = []

        def on_turn(turn_index, max_turns, role):
            calls.append((turn_index, max_turns, role))

        # Track which ModelAuditor instances were created
        created_auditors = []

        original_init = ModelAuditor.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created_auditors.append(self)

        with patch.object(ModelAuditor, '__init__', new=tracking_init), \
             patch.object(ModelAuditor, '_create_anyllm_client', return_value=MagicMock()):
            
            exp = AuditExperiment(
                models=[{"model": "test-model", "provider": "openai"}],
                judge_model="judge",
                judge_provider="openai",
                show_progress=False,
                n_repetitions=2,
                on_turn=on_turn,  # NEW
            )

            # Mock run_scenario to capture on_turn
            async def fake_run_scenario(self, **kwargs):
                # Verify on_turn was passed
                assert 'on_turn' in kwargs
                return AuditResult(
                    scenario_name="test",
                    scenario_description="desc",
                    conversation=[],
                    severity="pass",
                    issues_found=[],
                    positive_behaviors=[],
                    summary="",
                    recommendations=[],
                )

            with patch.object(ModelAuditor, 'run_scenario', new=fake_run_scenario):
                results = asyncio.run(exp.run_scenario_reps(
                    model_index=0,
                    scenario={"name": "test", "description": "desc"},
                ))

        # Should have created 2 auditors (one per rep)
        assert len(created_auditors) == 2
        # on_turn should have been called (at least once per rep)
        assert len(calls) >= 2

    def test_on_turn_not_forwarded_when_none(self):
        """When on_turn is None, it should not be passed to ModelAuditor."""
        captured_kwargs = []

        async def fake_run_scenario(self, **kwargs):
            captured_kwargs.append(kwargs)
            return AuditResult(
                scenario_name="test",
                scenario_description="desc",
                conversation=[],
                severity="pass",
                issues_found=[],
                positive_behaviors=[],
                summary="",
                recommendations=[],
            )

        with patch.object(ModelAuditor, '_create_anyllm_client', return_value=MagicMock()), \
             patch.object(ModelAuditor, 'run_scenario', new=fake_run_scenario):
            
            exp = AuditExperiment(
                models=[{"model": "test-model", "provider": "openai"}],
                judge_model="judge",
                judge_provider="openai",
                show_progress=False,
                n_repetitions=1,
            )

            asyncio.run(exp.run_scenario_reps(
                model_index=0,
                scenario={"name": "test", "description": "desc"},
            ))

        # on_turn should not be in kwargs (or should be None)
        for kwargs in captured_kwargs:
            assert 'on_turn' not in kwargs or kwargs['on_turn'] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/simpleaudit && python -m pytest tests/test_experiment_streaming.py::TestOnTurnForwarding -v`
Expected: FAIL with "unexpected keyword argument 'on_turn'"

- [ ] **Step 3: Write minimal implementation**

Modify `~/simpleaudit/simpleaudit/experiment.py`:

1. Add `on_turn` parameter to `run_scenario_reps()` signature (line ~332):
```python
async def run_scenario_reps(
    self,
    model_index: int,
    scenario: Dict[str, Any],
    max_turns: Optional[int] = None,
    language: str = "English",
    on_turn: Optional[Callable[[int, int, str], None]] = None,  # NEW
) -> List[AuditResult]:
```

2. Update docstring to document `on_turn`:
```python
"""Run a single scenario N times for one model.

Builds a fresh :class:`ModelAuditor` per rep (independent
conversations). Respects ``n_repetitions``, ``cancel_event``,
``on_rep_done``, ``max_retries_per_rep``, ``rep_is_done``, and the
disk cache (per-rep files under ``save_dir``).

Args:
    model_index: Index into ``self.models``.
    scenario: A single scenario dict.
    max_turns: Override for max conversation turns.
    language: Language for probe generation.
    on_turn: Optional callback fired at each phase boundary with
        ``(turn_index, max_turns, role)`` where role is "auditor",
        "target", or "judge". Called synchronously from within the
        asyncio event loop.

Returns:
    List of :class:`AuditResult`, one per completed rep. May be
    shorter than ``n_repetitions`` if cancellation occurred.
"""
```

3. Pass `on_turn` to `_run_single_rep()` (line ~410):
```python
rep_result = await self._run_single_rep(
    merged, [scenario], max_turns, language, max_workers=1,
    on_turn=on_turn,  # NEW
)
```

4. Add `on_turn` parameter to `_run_single_rep()` (line ~301):
```python
async def _run_single_rep(
    self,
    merged: Dict[str, Any],
    scenarios: Union[str, List[Dict]],
    max_turns: Optional[int],
    language: str,
    max_workers: int,
    on_turn: Optional[Callable[[int, int, str], None]] = None,  # NEW
) -> AuditResults:
    """Execute one rep with auto-retry on ERROR. Returns the final result."""
    attempts = 1 + self.max_retries_per_rep
    result: Optional[AuditResults] = None
    for attempt in range(attempts):
        auditor = ModelAuditor(**merged)
        result = await auditor.run_async(
            scenarios,
            max_turns=max_turns,
            language=language,
            max_workers=max_workers,
            on_turn=on_turn,  # NEW
        )
        if not any(r.severity == "ERROR" for r in result):
            break
        if attempt < attempts - 1:
            tqdm.write(
                f"  Rep returned ERROR (attempt {attempt + 1}/{attempts}) — retrying"
            )
    return result  # type: ignore[return-value]
```

5. Add `on_turn` parameter to `ModelAuditor.run_async()` (in `model_auditor.py`, line ~982):
```python
async def run_async(
    self,
    scenarios: Union[str, List[Dict]],
    max_turns: Optional[int] = None,
    language: str = "English",
    max_workers: int = 1,
    params: Optional[Dict[str, Any]] = None,
    target_params: Optional[Dict[str, Any]] = None,
    judge_params: Optional[Dict[str, Any]] = None,
    auditor_params: Optional[Dict[str, Any]] = None,
    on_turn: Optional[Callable[[int, int, str], None]] = None,  # NEW
) -> AuditResults:
```

6. Forward `on_turn` to `run_scenario()` in `run_async()` (line ~1033):
```python
async def _run_one(scenario: Dict) -> AuditResult:
    async with semaphore:
        try:
            return await self.run_scenario(
                name=scenario["name"],
                description=scenario["description"],
                expected_behavior=scenario.get("expected_behavior"),
                test_prompt=scenario.get("test_prompt"),
                file_uri=scenario.get("file_uri"),
                documents=scenario.get("documents"),
                judge_notes=(scenario.get("metadata") or {}).get("judge_notes"),
                scenario_meta={
                    "severity": scenario.get("severity"),
                    "category": scenario.get("category"),
                    "metadata": scenario.get("metadata") or {},
                },
                max_turns=max_turns,
                language=language,
                pbar_audit=pbar_audit,
                pbar_judge=pbar_judge,
                max_workers=max_workers,
                params=params,
                target_params=target_params,
                judge_params=judge_params,
                auditor_params=auditor_params,
                on_turn=on_turn,  # NEW
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/simpleaudit && python -m pytest tests/test_experiment_streaming.py::TestOnTurnForwarding -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/simpleaudit
git add simpleaudit/experiment.py simpleaudit/model_auditor.py tests/test_experiment_streaming.py
git commit -m "feat: forward on_turn through AuditExperiment.run_scenario_reps

Threads the on_turn callback from run_scenario_reps() through
_run_single_rep() and ModelAuditor.run_async() so per-turn progress
can be observed without monkey-patching. Callbacks fire at each
phase boundary (auditor, target, judge) for every rep."
```

---

### Task 3: Remove monkey-patch from SimpleAudit Studio

**Files:**
- Modify: `/Users/sushantgautam/simpleaudit-studio/infra/engine.py:352-560` (remove `_run_scenario_repeated_with_turns` and update `run_scenario_repeated`)
- Test: `/Users/sushantgautam/simpleaudit-studio/infra/tests/test_engine_integration.py` (update tests)

**Interfaces:**
- Consumes: `AuditExperiment.run_scenario_reps(on_turn=...)` from Task 2
- Produces: Clean `run_scenario_repeated()` that uses the native engine path

- [ ] **Step 1: Update `run_scenario_repeated()` to use native path**

Modify `/Users/sushantgautam/simpleaudit-studio/infra/engine.py`:

1. Remove the special case for `on_turn` (lines ~361-368):
```python
# REMOVE THIS BLOCK:
# if on_turn:
#     return _run_scenario_repeated_with_turns(
#         name=name, description=description,
#         expected_behavior=expected_behavior, test_prompt=test_prompt,
#         target=target, auditor=auditor, judge=judge,
#         generation=generation, n_repetitions=n_repetitions,
#         on_rep_done=on_rep_done, on_turn=on_turn,
#         on_rep_started=on_rep_started, cancel_event=cancel_event,
#     )
```

2. Update the `AuditExperiment` construction to pass `on_turn` (around line ~450):
```python
# Find where AuditExperiment is constructed and add on_turn:
experiment = AuditExperiment(
    models=[model_entry],
    judge_model=judge_cfg["model"],
    judge_provider=judge_cfg["provider"],
    judge_base_url=judge_cfg["base_url"],
    judge_api_key=judge_cfg["api_key"],
    auditor_model=auditor_cfg["model"],
    auditor_provider=auditor_cfg["provider"],
    auditor_base_url=auditor_cfg["base_url"],
    auditor_api_key=auditor_cfg["api_key"],
    show_progress=False,
    verbose=False,
    n_repetitions=n_repetitions,
    on_rep_done=_on_rep_done,
    cancel_event=cancel_event,
    on_turn=on_turn,  # NEW
)
```

3. Call `run_scenario_reps()` with `on_turn` (around line ~480):
```python
results = asyncio.run(
    experiment.run_scenario_reps(
        model_index=0,
        scenario=scenario,
        max_turns=max_turns,
        language=language,
        on_turn=on_turn,  # NEW
    )
)
```

4. Delete the entire `_run_scenario_repeated_with_turns()` function (lines ~560-650)

- [ ] **Step 2: Update tests**

Modify `/Users/sushantgautam/simpleaudit-studio/infra/tests/test_engine_integration.py`:

Find tests that reference `_run_scenario_repeated_with_turns` and update them to use the native path. Most tests should continue to work since the public API (`run_scenario_repeated`) is unchanged.

- [ ] **Step 3: Run tests to verify**

Run: `cd /Users/sushantgautam/simpleaudit-studio && python manage.py test infra.tests.test_engine_integration -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd /Users/sushantgautam/simpleaudit-studio
git add infra/engine.py infra/tests/test_engine_integration.py
git commit -m "refactor: remove monkey-patch, use native on_turn callback

Now that SimpleAudit supports on_turn natively, Studio no longer
needs to build a manual rep loop with patched ModelAuditor instances.
This simplifies the engine integration and removes a source of
fragility when the engine's internals change."
```

---

### Task 4: Run full test suites and verify

**Files:**
- None (verification only)

- [ ] **Step 1: Run SimpleAudit tests**

Run: `cd ~/simpleaudit && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run Studio tests**

Run: `cd /Users/sushantgautam/simpleaudit-studio && python manage.py test -v`
Expected: ALL PASS

- [ ] **Step 3: Verify no monkey-patching remains**

Run: `grep -r "patch\|monkey" /Users/sushantgautam/simpleaudit-studio/infra/engine.py`
Expected: No matches (or only comments mentioning the removal)

- [ ] **Step 4: Final commit (if needed)**

If any fixes were required during testing, commit them:
```bash
git add -A
git commit -m "fix: address test failures from on_turn refactoring"
```
