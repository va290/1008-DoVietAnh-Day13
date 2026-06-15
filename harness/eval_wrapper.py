"""Evaluation wrapper: runs the REAL solution/wrapper.py:mitigate() and also dumps
the full per-request result (question, answer, status, meta, trace) to
runs/eval.jsonl so harness/selfscore.py can estimate the composite score offline.

This is for YOUR local validation only -- it does not change the submission.
Run it with the sim instead of solution/wrapper.py:
    ./run.sh --wrapper harness/eval_wrapper.py     (WRAPPER=harness/eval_wrapper.py ./run.sh)
"""
from __future__ import annotations
import os
import sys
import json
import threading

# Make solution/ importable, then reuse the real mitigate (incl. its bootstrap).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from solution.wrapper import mitigate as _real_mitigate  # noqa: E402

_LOCK = threading.Lock()
_PATH = os.path.join(_ROOT, "runs", "eval.jsonl")
os.makedirs(os.path.dirname(_PATH), exist_ok=True)


def mitigate(call_next, question, config, context):
    result = _real_mitigate(call_next, question, config, context)
    try:
        meta = result.get("meta", {}) or {}
        row = {
            "qid": context.get("qid"), "turn_index": context.get("turn_index"),
            "question": question, "answer": result.get("answer"),
            "status": result.get("status"), "steps": result.get("steps"),
            "latency_ms": meta.get("latency_ms"), "usage": meta.get("usage"),
            "model": meta.get("model"), "tools_used": meta.get("tools_used"),
            "trace": result.get("trace"),
        }
        with _LOCK:
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return result
