"""Measure response latency across the four paths the assistant can take.

The router has four materially different execution paths, and conflating them
gives a misleading number:

    verification   an order number alone -> asks for the second field. No model.
    lookup         order number + identity -> deterministic render. No model.
    policy         a policy question      -> embed + retrieve + model.
    followup       a question about an already-verified order -> model.

The two deterministic paths are the point: they never reach the LLM, so their
latency is bounded by the database rather than by inference. Reporting only the
model-backed paths would hide that entirely.

Timing notes:
  * The typewriter animation sleeps 6ms per word (up to 60 words), which can add
    ~0.36s to a short deterministic reply. That is presentation, not work, so
    it is measured separately instead of being folded into the reported figure.
  * Query embeddings are cached with lru_cache, so a repeat policy question
    would report a cache hit. The cache is cleared between iterations to keep
    retrieval cost honest; --cached additionally measures the warm case.
  * Conversation persistence is stubbed out. This script writes no rows, so it
    leaves no conversation history behind.

Examples:
    python scripts/benchmark.py                    # 3 iterations per scenario
    python scripts/benchmark.py --iterations 5
    python scripts/benchmark.py --cached            # also measure warm embeddings
    python scripts/benchmark.py --json out.json     # save raw results

Requires GEMINI_API_KEY, SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env.
Model-backed scenarios spend Gemini quota, so keep --iterations modest.
"""

import argparse
import json
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Running as `python scripts/<name>.py` puts scripts/ on sys.path rather than the
# repository root, so orderbot would not resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from orderbot import chat as chat_module
from orderbot.retriever import _cached_query_embedding, retrieve_policy_matches

REPO_ROOT = Path(__file__).resolve().parent.parent

# A real seeded customer, resolved at runtime rather than hard-coded, so the
# verified-lookup scenarios exercise the same query shape the app uses.
LOOKUP_ORDER_ID = 1


def _sample_identity() -> tuple[int, str]:
    """Return (order_id, email) for a real order, or raise."""
    from orderbot.database import get_order_by_id

    for order_id in range(LOOKUP_ORDER_ID, 60):
        order = get_order_by_id(order_id)
        if order and (order.get("customers") or {}).get("email"):
            return order_id, order["customers"]["email"]
    raise RuntimeError("No seeded order with a customer email found; run scripts/seed_orders.py --insert first.")


@contextmanager
def no_persistence():
    """Stub conversation writes so a benchmark leaves no rows behind."""
    with (
        patch.object(chat_module, "create_conversation", lambda **kwargs: "benchmark"),
        patch.object(chat_module, "append_message", lambda *args, **kwargs: "benchmark"),
        patch("orderbot.responses.time.sleep", lambda _seconds: None),
    ):
        yield


def _fresh_handler(session_id: str = "benchmark"):
    """A handler with no carried-over verification state or DB results."""
    from orderbot.chat import OrderChatHandler
    from orderbot.state import VerificationAttemptGuard, VerificationState

    handler = OrderChatHandler.__new__(OrderChatHandler)
    handler.llm = chat_module.chatmodel()
    handler.llm_with_tools = chat_module.build_llm_with_tools()
    handler.conversation_history = []
    handler.last_db_results = None
    handler.verification = VerificationState()
    handler.verification_guard = VerificationAttemptGuard()
    handler.name_only_verified = False
    handler.conversation_id = None
    handler.session_id = session_id
    return handler


def measure(handler, message: str) -> dict:
    """Run one turn, returning first-token and total timings in milliseconds.

    A turn that degrades to the apology is rejected rather than timed. Model
    calls are rate-limited per minute on the free tier, and a throttled call
    returns almost instantly — recording that as a fast response would invert
    the result.
    """
    from orderbot.routing import FALLBACK_MESSAGE

    steps: list[tuple[str, float]] = []
    start = time.perf_counter()

    def on_step(label: str) -> None:
        steps.append((label, (time.perf_counter() - start) * 1000))

    first_token_ms = None
    text = []
    for delta in handler.stream_response(message, on_step=on_step):
        if delta:
            if first_token_ms is None:
                first_token_ms = (time.perf_counter() - start) * 1000
            text.append(delta)

    total_ms = (time.perf_counter() - start) * 1000
    reply = "".join(text)
    return {
        "first_token_ms": first_token_ms,
        "total_ms": total_ms,
        "steps": steps,
        "chars": len(reply),
        "degraded": FALLBACK_MESSAGE in reply or not reply.strip(),
    }


# --- scenarios -------------------------------------------------------------
# Each is (name, needs_identity) and runs against a fresh handler. Returns the
# measured turn so multi-turn scenarios can be expressed as callables.


def scenario_verification(identity):
    """Order number alone: the router asks for the second identity field."""
    order_id, _ = identity
    handler = _fresh_handler()
    return measure(handler, f"order {order_id}")


def scenario_lookup(identity):
    """Order number + matching email: deterministic render, no model call."""
    order_id, email = identity
    handler = _fresh_handler()
    return measure(handler, f"order {order_id}, email {email}")


def scenario_policy(_identity):
    """A policy question: embedding, pgvector retrieval, then the model."""
    handler = _fresh_handler()
    _cached_query_embedding.cache_clear()
    return measure(handler, "How long after delivery can I return an item?")


def scenario_followup(identity):
    """A question about an already-verified order: model, no re-lookup.

    The follow-up has to be one format_order_follow_up does not answer, or it
    would resolve deterministically and never reach the model. That function
    claims a large slice of order vocabulary -- delivery, tracking, status,
    payment -- so pick something outside it. "why was the delivery late?" does
    NOT qualify: "delivery" matches there and returns an estimate instead.
    """
    order_id, email = identity
    handler = _fresh_handler()
    measure(handler, f"order {order_id}, email {email}")  # establish context
    return measure(handler, "is the item fragile, and can I refuse it at the door?")


SCENARIOS = {
    "verification": (scenario_verification, False),
    "lookup": (scenario_lookup, False),
    "policy": (scenario_policy, True),
    "followup": (scenario_followup, False),
}

# Which paths can reach the model, used only to annotate the report.
USES_MODEL = {"verification": False, "lookup": False, "policy": True, "followup": True}

# A deterministic turn that should have needed the model is a bug in the
# scenario, not a fast result: assert on it rather than reporting a plausible-
# looking sub-millisecond figure for a path that is supposed to be model-backed.
FORCES_MODEL = {"followup"}

# A remote generate call cannot complete faster than this; anything quicker
# means no model was called.
MODEL_FLOOR_MS = 250.0


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "min": round(min(ordered), 1),
        "median": round(statistics.median(ordered), 1),
        "max": round(max(ordered), 1),
        # With 3-5 samples a true 95th percentile is not meaningful, so report
        # the range and label it rather than implying precision we don't have.
        "spread": round(max(ordered) - min(ordered), 1),
    }


def run(iterations: int, include_cached: bool, names: list[str], min_interval: float) -> dict:
    load_dotenv(dotenv_path=str(REPO_ROOT / ".env"))
    import os

    missing = [
        name
        for name, value in (
            ("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
            ("SUPABASE_URL", os.getenv("SUPABASE_URL")),
            ("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}. Add them to .env first.")

    identity = _sample_identity()
    results: dict[str, dict] = {}
    last_model_call = 0.0

    with no_persistence():
        for name in names:
            runner, uses_model = SCENARIOS[name]
            uses_model = uses_model or USES_MODEL[name]

            samples = []
            degraded_count = 0
            for _ in range(iterations + 2):  # allow a couple of retries
                if len(samples) >= iterations:
                    break
                if uses_model:
                    # Free tier allows a handful of generate requests per minute.
                    # Spacing them out avoids throttled samples being recorded as
                    # very fast responses.
                    elapsed = time.monotonic() - last_model_call
                    if elapsed < min_interval:
                        time.sleep(min_interval - elapsed)
                    last_model_call = time.monotonic()
                sample = runner(identity)
                if sample["degraded"]:
                    degraded_count += 1
                    if degraded_count >= 3:
                        # Persistently throttled; stop burning the quota budget.
                        break
                    continue
                samples.append(sample)

            if not samples:
                results[name] = {"uses_model": uses_model, "samples": 0, "degraded": True}
                continue

            first = [s["first_token_ms"] for s in samples if s["first_token_ms"] is not None]
            total = [s["total_ms"] for s in samples]

            entry = {
                "uses_model": uses_model,
                "samples": len(samples),
                "chars": samples[0]["chars"],
                "first_token_ms": _stats(first) if first else None,
                "total_ms": _stats(total),
            }

            # A model-backed scenario finishing in well under a second means the
            # deterministic renderer caught it, so the scenario is no longer
            # measuring what it claims. Flag rather than report.
            if name in FORCES_MODEL and entry["total_ms"]["median"] < MODEL_FLOOR_MS:
                entry["unexpectedly_fast"] = True

            if name == "policy":
                cold = time.perf_counter()
                matches = retrieve_policy_matches("how do I cancel my order")
                entry["retrieval_only_ms"] = round((time.perf_counter() - cold) * 1000, 1)
                entry["retrieval_chunks"] = len(matches)

                if include_cached:
                    # Embeddings are now warm, so this isolates generation cost.
                    elapsed = time.monotonic() - last_model_call
                    if elapsed < min_interval:
                        time.sleep(min_interval - elapsed)
                    last_model_call = time.monotonic()
                    warm = scenario_policy(identity)
                    if not warm["degraded"]:
                        entry["warm_total_ms"] = round(warm["total_ms"], 1)

            results[name] = entry

    return {
        "iterations": len(samples),
        "identity": {"order_id": identity[0]},
        "min_interval_s": min_interval,
        "scenarios": results,
    }


def report(results: dict) -> None:
    print(f"\nResponse latency by path (up to {results['iterations']} run(s) each, ms)\n")

    header = f"{'path':<14} {'model?':<7} {'n':>2} {'first token':>16} {'total':>16}"
    print(header)
    print("-" * len(header))

    def fmt(stats):
        if not stats:
            return "n/a"
        return f"{stats['median']:.0f} (+-{stats['spread']:.0f})"

    degraded = []
    for name, entry in results["scenarios"].items():
        if entry.get("degraded") or not entry.get("samples"):
            degraded.append(name)
            print(f"{name:<14} {'yes' if entry['uses_model'] else 'no':<7} {0:>2} {'all samples rate-limited':>16}")
            continue
        model = "yes" if entry["uses_model"] else "no"
        print(
            f"{name:<14} {model:<7} {entry['samples']:>2} "
            f"{fmt(entry['first_token_ms']):>16} {fmt(entry['total_ms']):>16}"
        )

    print("\nmedian (+- min-max spread). A true p95 needs far more samples than these budgets allow.")

    for name, entry in results["scenarios"].items():
        extras = []
        if "retrieval_only_ms" in entry:
            extras.append(
                f"retrieval alone {entry['retrieval_only_ms']:.0f}ms over {entry.get('retrieval_chunks', '?')} chunks"
            )
        if "warm_total_ms" in entry:
            cold = entry["total_ms"]["median"]
            extras.append(f"warm embedding cache {entry['warm_total_ms']:.0f}ms vs cold {cold:.0f}ms")
        if extras:
            print(f"  {name}: {'; '.join(extras)}")

    usable = [e for e in results["scenarios"].values() if e.get("samples")]
    det = [e["total_ms"]["median"] for e in usable if not e["uses_model"]]
    mod = [e["total_ms"]["median"] for e in usable if e["uses_model"] and not e.get("unexpectedly_fast")]
    if det and mod:
        print(f"\nDeterministic paths {statistics.median(det):.0f}ms vs model-backed {statistics.median(mod):.0f}ms.")
    if degraded:
        print(f"\nNo usable samples for: {', '.join(degraded)} (rate limited; raise --min-interval or retry).")
    suspects = [n for n, e in results["scenarios"].items() if e.get("unexpectedly_fast")]
    if suspects:
        print(f"\nWARNING: {', '.join(suspects)} finished too fast for a model call — the scenario text was")
        print("answered by the deterministic renderer, so it is not measuring the intended path.")
    print("No rows were written: conversation persistence was stubbed for this run.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure response latency across the assistant's four paths.")
    parser.add_argument("--iterations", type=int, default=3, help="Runs per scenario (default: 3).")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=14.0,
        help="Minimum seconds between model-backed calls (default: 14, sized for the free-tier rate limit).",
    )
    parser.add_argument("--cached", action="store_true", help="Also measure policy answers with warm embedding cache.")
    parser.add_argument("--json", type=Path, help="Write raw results to this path.")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=sorted(SCENARIOS),
        help="Restrict to specific scenarios (default: all four).",
    )
    args = parser.parse_args()

    results = run(args.iterations, args.cached, args.only or sorted(SCENARIOS), args.min_interval)
    report(results)

    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nRaw results written to {args.json}")


if __name__ == "__main__":
    main()
