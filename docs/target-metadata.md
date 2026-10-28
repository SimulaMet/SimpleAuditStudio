# Optional target metadata in model discovery

SimpleAuditStudio supports an **optional, nonstandard OpenAI-compatible extension**
for targets that want to expose auditable configuration before an audit begins.
The official OpenAI Model object does **not** define `metadata` on `/v1/models`.
Do not apply the unrelated 16 string-pair/512-character constraint of OpenAI's
shared Metadata type to this extension.

Return a JSON dictionary under `metadata` on `GET /v1/models/{model_id}` or
inside the matching model entry of `GET /v1/models`. Open WebUI's `info.meta`
shape is also recognized. The target can include other model fields as usual.

```json
{
  "id": "my-rag-agent",
  "object": "model",
  "metadata": {
    "version": "2.3.1",
    "framework": "langgraph",
    "prompts": {
      "system": {
        "content": "You are a helpful assistant.",
        "version": "3.2"
      },
      "retrieval": {
        "content": "Retrieve relevant documents for {query}",
        "version": "1.4"
      }
    },
    "retrieval": {"strategy": "hybrid", "top_k": 5},
    "tools": ["search", "calculator"],
    "guardrails": {"input": ["prompt_injection"]}
  }
}
```

The schema inside `metadata` is open: keys and nested JSON values are preserved,
including unfamiliar keys. Fields such as `prompts`, `framework`, `version`,
`retrieval`, `tools`, and `guardrails` are conventions, not required fields.

On each new run, Studio probes the selected target's model-detail endpoint,
falling back to the model list. It freezes metadata in
`AuditRun.target_config_snapshot.remote_metadata`, with source, capture time,
and discovery status in `metadata_discovery`. A missing or unavailable response
does not prevent the audit. A resumed run never refreshes the frozen metadata.
The run's Frozen Manifest and the Compare Runs input table flatten nested
objects to dot-notation rows (arrays stay JSON in one row).

The snapshot is a **target-declared claim, not independent proof** of which
prompts, guardrails, retrieval steps, or tools were actually used. Metadata may
contain private prompt text. Known secret-valued keys are redacted, but prompt
bodies can contain secrets too, so target owners should curate what they expose.
The response is limited to 256 KiB of metadata and a maximum nesting depth.
Metadata is visible to users with permission to view the run, just like other
frozen configuration details.
