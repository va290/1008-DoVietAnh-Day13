"""Mitigation + observability layer for the opaque, buggy e-commerce agent.

The simulator calls mitigate() around the (silent, real-LLM) agent for every
request. This is the ONLY place observability can live. Legal moves used here:
  - observability: structured logs + cost metrics + distributed traces
    (Day-13 telemetry/, backend chosen by OBS_BACKEND; 'langfuse' -> Langfuse Cloud)
  - input sanitize: strip injected "GHI CHU"/note instructions (private twist)
  - prompt routing: inject our rewritten system prompt per request
  - retry/backoff on agent failure (max_steps / empty / tool errors)
  - cache: thread-safe, keyed by sanitized question
  - output redaction: scrub PII (email/phone) from the answer
  - arithmetic/guardrail validation: recompute the exact total from the tool
    observations in result["trace"] and rewrite the final line. Prices come ONLY
    from check_stock -> immune to note-injected fake prices.

Illegal moves avoided: no hardcoded answers / price tables, no importing the
agent internals, no reading instructor files, no question exfiltration.
"""
from __future__ import annotations

# --- runtime bootstrap (local Docker only; harmless elsewhere) ----------------
# The distributed sim binary is a PyInstaller bundle shipped WITHOUT `openai` and
# with a trimmed stdlib (e.g. asyncio). This wrapper is imported INSIDE the frozen
# interpreter, so we repair its import path so the agent's lazy `import openai`
# (and our telemetry deps) resolve. Idempotent and safe on a normal host: the
# extra paths simply do not exist there. We append (never prepend) so the frozen
# bundle's own modules keep priority.
import sys as _sys
for _p in ("/opt/pylibs",
           "/usr/local/lib/python3.12",
           "/usr/local/lib/python3.12/lib-dynload",
           "/usr/local/lib/python3.12/site-packages"):
    if _p not in _sys.path:
        _sys.path.append(_p)
# Observability runs in-process on the *file* backend (reliable inside the frozen
# interpreter). Langfuse Cloud is fed afterwards by tools/export_to_langfuse.py,
# which runs in a normal Python where the OTel/requests stack works. So we never
# attempt the heavy Langfuse import here.
import os as _os
_os.environ.setdefault("OBS_BACKEND", "file")
# -----------------------------------------------------------------------------

import os
import re
import time
import random
import threading

# Concurrency throttle: the run is parallel (--concurrency); cap simultaneous
# in-flight LLM calls to avoid provider rate limits (429) on OmniRoute/OpenAI.
try:
    _MAX_INFLIGHT = int(os.getenv("OBS_MAX_INFLIGHT", "6"))
except Exception:
    _MAX_INFLIGHT = 6
_INFLIGHT = threading.Semaphore(max(1, _MAX_INFLIGHT))
_RATE_RE = re.compile(r"(?i)\b(429|rate.?limit|too many requests|overloaded|quota)\b")

# Day-13 telemetry toolkit (optional; the wrapper still runs if it is absent).
try:
    from telemetry.logger import logger, new_correlation_id, set_correlation_id
    from telemetry.cost import cost_from_usage
    from telemetry.redact import redact
    from telemetry.tracing import Tracer
except Exception:  # pragma: no cover
    logger = None

    def cost_from_usage(model, usage):
        return 0.0

    def redact(s, *a, **k):
        return (s, 0)

    Tracer = None

# One tracer for the whole run; its backend is picked from OBS_BACKEND
# (file [default] | console | sqlite | multi | langfuse). Set OBS_BACKEND=langfuse
# + LANGFUSE_* env to stream traces to Langfuse Cloud.
_TRACER = None
if Tracer is not None:
    try:
        _TRACER = Tracer(service_name="observathon-agent")
    except Exception:
        _TRACER = None

# Load our rewritten system prompt once (for per-request prompt routing).
_PROMPT = None
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "prompt.txt"), encoding="utf-8") as _f:
        _PROMPT = _f.read().strip()
except Exception:
    _PROMPT = None

# Lightweight in-process drift counters (guarded by the shared cache_lock).
_STATE = {"calls": 0, "ok": 0, "looped": 0, "overridden": 0, "refused": 0, "pii": 0}

# --- input sanitization (defuse note-injection) -------------------------------
# Remove an appended "GHI CHU"/note/system clause that may carry a fake price or
# an instruction. We only drop the trailing note, never the order fields.
_NOTE_RE = re.compile(
    r"(?is)\s*[\(\[\-–—;,. ]*\b(ghi\s*ch[uú]|l[uư]u\s*[yý]|ch[uú]\s*[yý]|note|notes|"
    r"system|admin|important|instruction|chỉ\s*thị|cập\s*nhật|update)\b\s*[:\-–—].*$"
)


def _sanitize(question: str) -> str:
    if not isinstance(question, str):
        return question
    return _NOTE_RE.sub("", question).strip()


# --- trace parsing + deterministic total --------------------------------------
_KNOWN_TOOLS = {"check_stock", "get_discount", "calc_shipping"}


def _only_known_tools(trace):
    """True if every tool in the trace is one our floor formula models. If the
    private set introduces a loyalty/coupon (F13) tool, this is False and we do
    NOT override the total -- we trust the injection-defended prompt instead of
    forcing a number our formula would compute wrong."""
    for s in trace or []:
        t = s.get("tool")
        if t and t not in _KNOWN_TOOLS:
            return False
    return True


def _last_obs(trace, tool, need_key=None):
    """Last observation for `tool` that carries real data (skip pure errors)."""
    found = None
    for step in trace or []:
        if step.get("tool") != tool:
            continue
        obs = step.get("observation") or {}
        if need_key is not None and need_key not in obs:
            continue
        found = obs
    return found


# Number words (VN with/without diacritics + English) -> robust to paraphrase.
_NUMWORD = {
    "mot": 1, "một": 1, "hai": 2, "ba": 3, "bon": 4, "bốn": 4, "nam": 5, "năm": 5,
    "sau": 6, "sáu": 6, "bay": 7, "bảy": 7, "tam": 8, "tám": 8, "chin": 9, "chín": 9,
    "muoi": 10, "mười": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}
_BUY = r"(?:mua|lay|lấy|dat|đặt|can|cần|order|buy|get|purchase)"


def _qty(question: str):
    q = question or ""
    # 1) buy-verb + digit:  "mua 3", "order 5"
    m = re.search(r"(?i)\b" + _BUY + r"\s+(\d{1,4})\b", q)
    if m:
        return int(m.group(1))
    # 2) digit + unit noun:  "2 cai", "5 chiec", "3 pcs"
    m = re.search(r"(?i)\b(\d{1,4})\s*(?:cai|chiec|cái|chiếc|pcs?|san\s*pham|sản\s*phẩm)\b", q)
    if m:
        return int(m.group(1))
    # 3) buy-verb / unit + number word:  "mua ba chiec", "lay hai cai"
    m = re.search(r"(?i)\b" + _BUY + r"\s+([a-zàáảãạăâđêôơưèéẹêìíịòóọùúụ]+)", q)
    if m and m.group(1).lower() in _NUMWORD:
        return _NUMWORD[m.group(1).lower()]
    for tok in re.findall(r"[A-Za-zÀ-ỹ]+", q.lower()):
        if tok in _NUMWORD:
            return _NUMWORD[tok]
    # 4) fallback: first standalone 1-4 digit integer (qty leads these orders;
    #    coupon digits like VIP20/SALE15 are glued to letters so \b excludes them)
    if re.search(r"(?i)\b" + _BUY + r"\b", q):
        m = re.search(r"\b(\d{1,4})\b", q)
        if m:
            return int(m.group(1))
    return None


def _wants_shipping(question: str, trace) -> bool:
    if _last_obs(trace, "calc_shipping") is not None:
        return True
    return bool(re.search(r"(?i)\b(giao|ship|gửi|gui|v[aậ]n\s*chuy[eể]n)\b", question or ""))


def _guardrail_answer(question: str, trace):
    """Return a corrected answer string, or None to keep the model's answer.

    Grounded entirely in tool observations (catalog price, validity, shipping),
    so a note-injected fake price can never change the total.
    """
    stock = _last_obs(trace, "check_stock", need_key="found")
    if stock is None:
        return None  # no usable stock data -> let retry / model answer stand

    item = str(stock.get("item") or "sản phẩm")
    if not stock.get("found", False):
        return f"Không tìm thấy sản phẩm {item}. Không thể tạo đơn."
    if not stock.get("in_stock", False):
        return f"Sản phẩm {item} hiện hết hàng nên không thể đặt mua."

    unit = stock.get("unit_price_vnd")
    if not isinstance(unit, (int, float)):
        return None

    ship_obs = _last_obs(trace, "calc_shipping")
    wants_ship = _wants_shipping(question, trace)
    shipping = 0
    if ship_obs is not None:
        if ship_obs.get("error") or ship_obs.get("cost_vnd") is None:
            if wants_ship:
                dest = ship_obs.get("destination") or "địa chỉ này"
                return f"Rất tiếc, hiện không hỗ trợ giao đến {dest}."
        else:
            shipping = int(ship_obs.get("cost_vnd") or 0)
    elif wants_ship:
        return None  # destination requested but no shipping data -> don't guess

    if int(unit) <= 0 or shipping < 0:
        return None  # implausible tool data -> don't override

    # If an unfamiliar tool (e.g. private loyalty/coupon F13) is involved, our
    # floor formula may be incomplete -> let the prompt-driven agent answer.
    if not _only_known_tools(trace):
        return None

    qty = _qty(question)
    if qty is None:
        # Pure stock/price question (no quantity, no delivery): answer the price.
        if not wants_ship:
            return f"{item} còn hàng, đơn giá {int(unit)} VND."
        return None
    if qty <= 0 or qty > 1000:
        return None  # implausible quantity -> let the model answer

    pct = 0
    disc = _last_obs(trace, "get_discount", need_key="valid")
    if disc and disc.get("valid"):
        pct = int(disc.get("percent") or 0)
    if not (0 <= pct <= 100):
        pct = 0  # guard against a garbage discount percent

    subtotal = int(unit) * int(qty)
    discounted = subtotal * (100 - pct) // 100
    total = discounted + shipping
    if total <= 0:
        return None
    return f"Tong cong: {total} VND"


def _looped(trace) -> bool:
    seen = {}
    for step in trace or []:
        a = step.get("action")
        seen[a] = seen.get(a, 0) + 1
        if seen[a] >= 3:
            return True
    return False


def _needs_retry(result) -> bool:
    if not isinstance(result, dict):
        return True
    status = result.get("status")
    if status in ("max_steps", "loop", "no_action", "wrapper_error"):
        return True
    if not (result.get("answer") or "").strip():
        return True
    return False


def mitigate(call_next, question, config, context):
    t0 = time.time()
    cache = context.get("cache")
    lock = context.get("cache_lock")
    qid = context.get("qid")
    turn = context.get("turn_index")

    if logger is not None:
        try:
            set_correlation_id(new_correlation_id())
        except Exception:
            pass

    clean_q = _sanitize(question)

    # --- cache (thread-safe) ---
    cache_key = ("q", clean_q)
    if cache is not None and lock is not None:
        with lock:
            hit = cache.get(cache_key)
        if hit is not None:
            return dict(hit)

    # --- prompt routing: inject our rewritten system prompt ---
    conf = dict(config)
    if _PROMPT:
        conf["system_prompt"] = _PROMPT

    # --- call the agent with retry/backoff ---
    result = None
    attempts = 0
    max_attempts = 3
    span_cm = None
    if _TRACER is not None:
        try:
            span_cm = _TRACER.start_span("mitigate", qid=qid, turn_index=turn)
        except Exception:
            span_cm = None
    try:
        for attempts in range(1, max_attempts + 1):
            rate_limited = False
            with _INFLIGHT:                       # throttle concurrent LLM calls
                try:
                    result = call_next(clean_q, conf)
                except Exception as exc:
                    msg = repr(exc)
                    rate_limited = bool(_RATE_RE.search(msg))
                    result = {"answer": None, "status": "wrapper_error", "steps": 0,
                              "trace": [], "meta": {"error": msg}}
            if not rate_limited:
                rate_limited = bool(_RATE_RE.search(str((result.get("meta") or {}).get("error", ""))))
            if not _needs_retry(result):
                break
            # exponential backoff with jitter; longer when rate-limited
            base = 1.0 if rate_limited else 0.25
            time.sleep(base * (2 ** (attempts - 1)) + random.uniform(0, 0.25))
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:
                pass

    if not isinstance(result, dict):
        result = {"answer": None, "status": "wrapper_error", "steps": 0, "trace": [], "meta": {}}

    trace = result.get("trace") or []
    meta = result.get("meta") or {}

    # --- guardrail: recompute the exact total / refusal from tool data ---
    overridden = False
    try:
        fixed = _guardrail_answer(clean_q, trace)
        if fixed is not None and fixed != (result.get("answer") or ""):
            result["answer"] = fixed
            overridden = True
    except Exception:
        pass

    # --- output redaction: never leak PII in the answer ---
    pii_n = 0
    try:
        red, pii_n = redact(result.get("answer") or "")
        if pii_n:
            result["answer"] = red
    except Exception:
        pii_n = 0

    # --- observability: the only place these signals exist ---
    looped = _looped(trace)
    if logger is not None:
        try:
            usage = meta.get("usage", {}) or {}
            logger.log_event("AGENT_CALL", {
                "qid": qid, "turn_index": turn, "status": result.get("status"),
                "model": meta.get("model", ""), "provider": meta.get("provider", ""),
                "attempts": attempts, "looped": looped, "overridden": overridden,
                "steps": result.get("steps"),
                "tools_used": meta.get("tools_used", []),
                "n_tool_calls": len(trace),
                "latency_ms": meta.get("latency_ms"),
                "wall_ms": int((time.time() - t0) * 1000),
                "usage": usage,
                "cost_usd": cost_from_usage(meta.get("model", ""), usage),
                "pii_in_answer": pii_n,
                "answer_preview": (result.get("answer") or "")[:120],
            })
        except Exception:
            pass

    if lock is not None:
        with lock:
            _STATE["calls"] += 1
            _STATE["ok"] += 1 if result.get("status") == "ok" else 0
            _STATE["looped"] += 1 if looped else 0
            _STATE["overridden"] += 1 if overridden else 0
            _STATE["pii"] += pii_n
            if (result.get("answer") or "").lower().startswith(
                    ("không tìm", "sản phẩm", "rất tiếc")):
                _STATE["refused"] += 1

    # --- cache store ---
    if cache is not None and lock is not None and result.get("status") == "ok":
        with lock:
            cache[cache_key] = dict(result)

    return result
