"""Evaluate live RAG retrieval against the curated policy-question set.

Run after `scripts/seed_knowledge.py --reingest`:
    python scripts/evaluate_rag.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orderbot.retriever import MIN_SIMILARITY, retrieve_policy_matches

REPO_ROOT = Path(__file__).resolve().parent.parent

CASES_PATH = REPO_ROOT / "eval" / "rag_eval_cases.json"

LIVENESS_QUERY = "what is the return window"

LIVENESS_EXPECTED_DOC = "Returns Policy"


def assert_retrieval_works() -> None:
    """Fail fast when retrieval is broken rather than scoring it as a pass.

    ``retrieve_policy_matches`` swallows every error and returns ``[]``, which
    is indistinguishable from a legitimate no-match. Every negative case would
    therefore "pass" during an outage, inflating no-match accuracy to 100%.
    """
    matches = retrieve_policy_matches(LIVENESS_QUERY)
    if not matches:
        raise SystemExit(
            "Retrieval returned no results for the liveness query "
            f'"{LIVENESS_QUERY}". Check that seed_knowledge.py has been run and that '
            "Supabase and the embedding API are reachable; scoring now would be meaningless."
        )
    titles = {match.get("doc_title") for match in matches}
    if LIVENESS_EXPECTED_DOC not in titles:
        raise SystemExit(
            f'Retrieval for "{LIVENESS_QUERY}" returned {sorted(titles)} but not '
            f'"{LIVENESS_EXPECTED_DOC}". The corpus is missing or stale; re-run '
            "seed_knowledge.py --reingest."
        )


def evaluate(cases: list[dict]) -> tuple[list[dict], dict[str, float | int]]:
    results = []
    for case in cases:
        matches = retrieve_policy_matches(case["question"])
        returned_docs = list(dict.fromkeys(match.get("doc_title", "Unknown") for match in matches))
        expected_docs = set(case["expected_docs"])
        relevant_ranks = [index for index, title in enumerate(returned_docs, start=1) if title in expected_docs]
        passed = not returned_docs if not expected_docs else bool(relevant_ranks)
        reciprocal_rank = 1 / relevant_ranks[0] if relevant_ranks else 0.0
        recall_at_k = len(expected_docs.intersection(returned_docs)) / len(expected_docs) if expected_docs else 0.0
        results.append(
            {
                **case,
                "returned_docs": returned_docs,
                "scores": [round(match.get("similarity", 0), 3) for match in matches],
                "reciprocal_rank": reciprocal_rank,
                "recall_at_k": recall_at_k,
                "passed": passed,
            }
        )

    positive = [result for result in results if result["expected_docs"]]
    negative = [result for result in results if not result["expected_docs"]]
    stats = {
        "cases": len(results),
        "passed": sum(result["passed"] for result in results),
        "retrieval_hit_rate": sum(result["passed"] for result in positive) / len(positive) if positive else 0,
        "no_match_accuracy": sum(result["passed"] for result in negative) / len(negative) if negative else 0,
        "mean_reciprocal_rank": (
            sum(result["reciprocal_rank"] for result in positive) / len(positive) if positive else 0
        ),
        "mean_recall_at_k": sum(result["recall_at_k"] for result in positive) / len(positive) if positive else 0,
    }
    return results, stats


def main() -> None:
    assert_retrieval_works()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results, stats = evaluate(cases)
    print(f"RAG evaluation ({stats['cases']} cases, min similarity {MIN_SIMILARITY:.2f})")
    print(f"Pass rate: {stats['passed']}/{stats['cases']}")
    print(f"Retrieval hit rate: {stats['retrieval_hit_rate']:.1%}")
    print(f"No-match accuracy: {stats['no_match_accuracy']:.1%}")
    print(f"Mean reciprocal rank: {stats['mean_reciprocal_rank']:.3f}")
    print(f"Mean recall@k: {stats['mean_recall_at_k']:.1%}")
    print("\nFailures:")
    failures = [result for result in results if not result["passed"]]
    if not failures:
        print("None")
        return
    for result in failures:
        expected = ", ".join(result["expected_docs"]) or "no result"
        actual = ", ".join(result["returned_docs"]) or "no result"
        print(f"- {result['id']}: expected {expected}; got {actual}; scores={result['scores']}")


if __name__ == "__main__":
    main()
