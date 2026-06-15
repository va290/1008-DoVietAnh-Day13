"""Push a finished run's telemetry to Langfuse Cloud.

Why a separate step: the agent runs inside a PyInstaller-frozen interpreter with a
trimmed stdlib, where Langfuse's OpenTelemetry/requests stack will not import. So
the wrapper records telemetry to the *file* backend in-process (logs/*.log), and
this script — run in a normal Python where `langfuse` works — reads those events
and recreates them as Langfuse generations. Observability stays off the agent's
critical path (best practice).

Usage (inside the same python:3.12-slim container, or any host with langfuse):
    pip install langfuse
    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... LANGFUSE_HOST=https://cloud.langfuse.com \
        python tools/export_to_langfuse.py --logs logs --session practice

Reads every AGENT_CALL event the wrapper logged and emits one Langfuse trace per
request with a nested generation (model, token usage, cost, latency, tool count).
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import sys


def _load_events(logs_dir: str):
    events = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "*.log"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") == "AGENT_CALL":
                    events.append(rec)
    # The sim calls mitigate twice per request; keep the last event per qid.
    by_qid = {}
    for rec in events:
        qid = (rec.get("data") or {}).get("qid")
        by_qid[qid if qid is not None else id(rec)] = rec
    return list(by_qid.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="logs", help="dir with telemetry *.log files")
    ap.add_argument("--session", default="run", help="session/tag for the traces")
    args = ap.parse_args()

    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        print("[export] LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set", file=sys.stderr)
        return 2
    try:
        from langfuse import get_client
    except ImportError:
        print("[export] pip install langfuse first", file=sys.stderr)
        return 2

    events = _load_events(args.logs)
    if not events:
        print(f"[export] no AGENT_CALL events found under {args.logs}/")
        return 0

    client = get_client()
    if not client.auth_check():
        print("[export] Langfuse auth failed (check keys/host)", file=sys.stderr)
        return 2

    n = 0
    for rec in events:
        d = rec.get("data", {})
        usage = d.get("usage", {}) or {}
        qid = d.get("qid") or "req"
        with client.start_as_current_observation(
            name=f"request:{qid}", as_type="span",
            input={"qid": qid, "turn_index": d.get("turn_index")},
            output=d.get("answer_preview"),
            metadata={
                "status": d.get("status"), "looped": d.get("looped"),
                "overridden": d.get("overridden"), "tools_used": d.get("tools_used"),
                "n_tool_calls": d.get("n_tool_calls"), "attempts": d.get("attempts"),
                "pii_in_answer": d.get("pii_in_answer"),
            },
        ) as span:
            try:
                span.update_trace(name=f"{args.session}:{qid}", session_id=args.session,
                                  tags=[args.session, str(d.get("status"))])
            except Exception:
                pass
            with client.start_as_current_observation(
                name="agent-generation", as_type="generation",
                model=d.get("model") or "unknown",
                output=d.get("answer_preview"),
                usage_details={
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                },
                cost_details={"total": float(d.get("cost_usd") or 0.0)},
                metadata={"latency_ms": d.get("latency_ms"), "wall_ms": d.get("wall_ms"),
                          "status": d.get("status")},
            ):
                pass
        n += 1

    client.flush()
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    print(f"[export] pushed {n} traces to Langfuse ({host}); session='{args.session}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
