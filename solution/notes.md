# Diagnosis scratchpad

Telemetry captured by `solution/wrapper.py` (logs/*.log) + full traces via
`harness/eval_wrapper.py` (runs/eval.jsonl). Engine: OmniRoute gateway,
model `claude/claude-haiku-4-5-20251001` (provider `openai` + OPENAI_BASE_URL).

| symptom (from telemetry) | which requests | suspected cause | config fix | wrapper fix |
|---|---|---|---|---|
| status=max_steps, EMPTY answer; check_stock repeated up to 12x | MacBook / da-lat / any when a tool errors | `loop_guard:false` + `max_steps:12`; agent re-calls a failing tool | `loop_guard:true`, `max_steps:6`, `tool_budget:4` | retry whole request; detect repeated action |
| check_stock -> `upstream_unavailable` intermittently | random | injected `tool_error_rate:0.18`, no retry | `tool_error_rate:0`, `retry{on,3,250ms}` | retry/backoff on failure |
| `Hải Phòng` -> not_served but `hai phong` -> served (34250) | diacritic destinations | `normalize_unicode:false` | `normalize_unicode:true` | — |
| MacBook always out of stock | every MacBook order | `catalog_override` lies `in_stock:false` | clear `catalog_override:{}` | — |
| email `minh.tran@yahoo.com` echoed in answer | orders with contact info | `redact_pii:false`, bad prompt | `redact_pii:true` | redact() the final answer |
| confident total for not-served / out-of-stock | refusal cases | bad prompt fabricates | — (prompt) | guardrail emits number-free refusal |
| verbose tables, estimated totals; temp 1.6 | all | no exact-math instruction, high temp | `temperature:0.2`, `verify:true` | recompute total from trace (floor formula) |
| ~70k total_tokens, latency p95 ~12s | all | multi-step tool calls resend context; `verbose_system:true` | `verbose_system:false`, `context_size:4`, `max_completion_tokens:512`, `cache` | cache repeats |
| note-injected fake price (private) | `GHI CHU` orders | prompt obeys notes | — (prompt) | sanitize note; total comes ONLY from check_stock obs |

## Decisions
- **Model**: claude-haiku keeps correct=1.00 (self-score). gemini-2.5-flash-lite
  fails tool-calling here (0/20 ok) -> rejected.
- **Highest-leverage fix**: wrapper arithmetic/refusal guardrail recomputes the
  exact total from the agent's tool observations, so `correct` is deterministic
  and immune to the injection twist (price never comes from the note).
- `self_consistency:1` (guardrail already deterministic; extra samples = wasted cost).

## Self-score (harness/selfscore.py, practice, claude-haiku)
correct 19/19=1.00 · error 1.00 · drift 1.00 · tool-econ 1.00 · PII-clean 1.00 ·
latency p95 ~12s (0.38) · cost ~70k tok (harsh local norm) · composite ~83-86/100
(quality excluded; + up to 22 x diagnosis-F1). Estimate only -- the official
`observathon-score` is authoritative.
