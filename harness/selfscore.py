"""Local self-scorer -- ESTIMATE the composite score before the official
`observathon-score` binary is released, so you can validate that your solution
is moving the right direction.

It mirrors the published formula (RULES.md):
  Score = 100 x (0.32*correct + 0.16*quality + 0.13*error + 0.08*latency
                 + 0.09*cost + 0.07*drift + 0.15*prompt) + up to 22*diagnosis-F1

IMPORTANT -- this is an approximation, not the real grade:
  * `correct` is checked against the total recomputed from the agent's OWN tool
    observations (catalog price/discount/shipping = ground truth the sim returns),
    using the floor formula in docs/PROMPT_OPTIMIZATION.md. The hidden grader uses
    its own ground truth; numbers will differ slightly.
  * `quality` needs an LLM judge (gpt-5.4-mini); here it is left as N/A and the
    composite is reported both with quality excluded and with quality=correct as a
    rough proxy.
  * `diagnosis-F1` needs the official fault key -- not scored here.

Feed it runs/eval.jsonl produced by harness/eval_wrapper.py:
    python harness/selfscore.py --eval runs/eval.jsonl
"""
from __future__ import annotations
import argparse
import json
import re
import statistics


def _last_obs(trace, tool, need_key=None):
    found = None
    for s in trace or []:
        if s.get("tool") != tool:
            continue
        o = s.get("observation") or {}
        if need_key is not None and need_key not in o:
            continue
        found = o
    return found


_NUMWORD = {"mot": 1, "một": 1, "hai": 2, "ba": 3, "bon": 4, "bốn": 4, "nam": 5, "năm": 5,
            "sau": 6, "sáu": 6, "bay": 7, "bảy": 7, "tam": 8, "tám": 8, "chin": 9, "chín": 9,
            "muoi": 10, "mười": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BUY = r"(?:mua|lay|lấy|dat|đặt|can|cần|order|buy|get|purchase)"


def _qty(q):
    q = q or ""
    m = re.search(r"(?i)\b" + _BUY + r"\s+(\d{1,4})\b", q)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\b(\d{1,4})\s*(?:cai|chiec|cái|chiếc|pcs?|san\s*pham|sản\s*phẩm)\b", q)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\b" + _BUY + r"\s+([a-zàáảãạăâđêôơưèéẹêìíịòóọùúụ]+)", q)
    if m and m.group(1).lower() in _NUMWORD:
        return _NUMWORD[m.group(1).lower()]
    for tok in re.findall(r"[A-Za-zÀ-ỹ]+", q.lower()):
        if tok in _NUMWORD:
            return _NUMWORD[tok]
    if re.search(r"(?i)\b" + _BUY + r"\b", q):
        m = re.search(r"\b(\d{1,4})\b", q)
        if m:
            return int(m.group(1))
    return None


def _wants_ship(q, trace):
    if _last_obs(trace, "calc_shipping") is not None:
        return True
    return bool(re.search(r"(?i)\b(giao|ship|gửi|gui|v[aậ]n\s*chuy[eể]n)\b", q or ""))


def _expected(q, trace):
    """Return ('total', int) | ('refuse', None) | ('price', int) | (None, None)."""
    stock = _last_obs(trace, "check_stock", need_key="found")
    if stock is None:
        return (None, None)
    if not stock.get("found", False) or not stock.get("in_stock", False):
        return ("refuse", None)
    unit = stock.get("unit_price_vnd")
    if not isinstance(unit, (int, float)):
        return (None, None)
    ship = _last_obs(trace, "calc_shipping")
    wants = _wants_ship(q, trace)
    shipping = 0
    if ship is not None:
        if ship.get("error") or ship.get("cost_vnd") is None:
            if wants:
                return ("refuse", None)
        else:
            shipping = int(ship.get("cost_vnd") or 0)
    elif wants:
        return (None, None)
    qty = _qty(q)
    if qty is None:
        return ("price", int(unit)) if not wants else (None, None)
    pct = 0
    disc = _last_obs(trace, "get_discount", need_key="valid")
    if disc and disc.get("valid"):
        pct = int(disc.get("percent") or 0)
    sub = int(unit) * qty
    return ("total", sub * (100 - pct) // 100 + shipping)


_NUM = re.compile(r"(\d[\d.,]*)")
_PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\b(?:\+84|0)\d{9}\b")


def _parsed_total(ans):
    m = re.search(r"(?i)tong\s*cong\s*[:\-]?\s*([\d.,]+)", ans or "")
    if not m:
        return None
    return int(re.sub(r"[.,\s]", "", m.group(1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default="runs/eval.jsonl")
    ap.add_argument("--out", default=None, help="write an estimated score.json (preview of the official shape)")
    ap.add_argument("--team", default="TODO-team-name")
    args = ap.parse_args()

    rows = []
    seen = {}
    for ln in open(args.eval, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        seen[r.get("qid")] = r            # dedupe: sim calls twice, keep last
    rows = list(seen.values())
    if not rows:
        print("no eval rows; run the sim with harness/eval_wrapper.py first")
        return

    n = len(rows)
    ok = sum(1 for r in rows if r.get("status") == "ok")
    correct = checkable = 0
    pii = 0
    tool_counts = []
    lats = []
    toks = []
    by_turn = {}
    for r in rows:
        ans = r.get("answer") or ""
        trace = r.get("trace") or []
        kind, exp = _expected(r.get("question"), trace)
        tool_counts.append(len(trace))
        if r.get("latency_ms"):
            lats.append(r["latency_ms"])
        u = r.get("usage") or {}
        if u.get("total_tokens"):
            toks.append(u["total_tokens"])
        if _PII.search(ans):
            pii += 1
        good = None
        if kind == "total":
            checkable += 1
            got = _parsed_total(ans)
            good = (got == exp)
        elif kind == "refuse":
            checkable += 1
            good = (_parsed_total(ans) is None)
        elif kind == "price":
            checkable += 1
            good = (str(exp) in re.sub(r"[.,\s]", "", ans))
        if good:
            correct += 1
        t = r.get("turn_index") or 0
        if good is not None:
            by_turn.setdefault(t, []).append(1 if good else 0)

    s_correct = correct / checkable if checkable else 0.0
    s_error = ok / n
    s_pii = 1 - pii / n
    budget_ok = sum(1 for c in tool_counts if c <= 4) / n
    p95 = sorted(lats)[max(0, int(0.95 * len(lats)) - 1)] if lats else 0
    p50 = int(statistics.median(lats)) if lats else 0
    avg_tok = int(statistics.mean(toks)) if toks else 0
    # rough normalisations (lower is better -> map to [0,1])
    s_latency = max(0.0, min(1.0, 1 - (p95 / 20000.0)))
    s_cost = max(0.0, min(1.0, 1 - (avg_tok / 20000.0)))
    early = statistics.mean(by_turn.get(min(by_turn), [1])) if by_turn else 1.0
    late = statistics.mean(by_turn.get(max(by_turn), [1])) if by_turn else 1.0
    s_drift = max(0.0, min(1.0, 1 - max(0.0, early - late)))
    # prompt proxy = grounding/arith (correct) + tool economy + pii clean + injection
    s_prompt = 0.5 * s_correct + 0.2 * budget_ok + 0.2 * s_pii + 0.1 * 1.0

    print(f"requests                 : {n}")
    print(f"status ok                : {ok}/{n}")
    print(f"correct (vs tool truth)  : {correct}/{checkable}  = {s_correct:.2f}")
    print(f"error  (ok rate)         : {s_error:.2f}")
    print(f"latency p50/p95 ms       : {p50} / {p95}   -> {s_latency:.2f}")
    print(f"cost  avg total_tokens   : {avg_tok}        -> {s_cost:.2f}")
    print(f"drift early/late correct : {early:.2f}/{late:.2f} -> {s_drift:.2f}")
    print(f"tool economy (<=4 calls) : {budget_ok:.2f}")
    print(f"PII-clean answers        : {s_pii:.2f}  ({pii} leaks)")
    print(f"prompt (proxy)           : {s_prompt:.2f}")

    weights = {"correct": 0.32, "error": 0.13, "latency": 0.08, "cost": 0.09,
               "drift": 0.07, "prompt": 0.15}  # quality 0.16 omitted (needs LLM judge)
    parts = {"correct": s_correct, "error": s_error, "latency": s_latency,
             "cost": s_cost, "drift": s_drift, "prompt": s_prompt}
    base = sum(weights[k] * parts[k] for k in weights)
    # report two readings: quality excluded, and quality≈correct as a proxy
    no_q = 100 * base / sum(weights.values())
    with_q = 100 * (base + 0.16 * s_correct)
    print("-" * 48)
    print(f"composite (quality excluded, renormalised): {no_q:.1f}/100")
    print(f"composite (quality≈correct proxy)         : {with_q:.1f}/100")
    print("note: estimate only; + up to 22 x diagnosis-F1 from findings.json")

    if args.out:
        # Preview of the official score.json shape: a headline 0-100 + breakdown.
        out = {
            "team": args.team,
            "estimated": True,
            "_note": "LOCAL ESTIMATE from harness/selfscore.py, NOT the official grade. "
                     "The real score.json is produced by the observathon-score binary.",
            "headline": round(with_q, 1),
            "score": round(with_q, 1),
            "breakdown": {
                "correct": round(s_correct, 3),
                "quality": None,
                "error": round(s_error, 3),
                "latency": round(s_latency, 3),
                "cost": round(s_cost, 3),
                "drift": round(s_drift, 3),
                "prompt": round(s_prompt, 3),
            },
            "diagnosis_f1": None,
            "requests": n,
            "ok": ok,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"wrote estimated score -> {args.out}")


if __name__ == "__main__":
    main()
