"""Evaluate live RAG retrieval against the curated policy-question set.

Run after `seed_knowledge.py --reingest`:
    python evaluate_rag.py
"""

import json
from pathlib import Path

from retriever import MIN_SIMILARITY, retrieve_policy_matches


CASES_PATH = Path("eval/rag_eval_cases.json")


def evaluate(cases: list[dict]) -> tuple[list[dict], dict[str, float | int]]:
    results = []
    for case in cases:
        matches = retrieve_policy_matches(case["question"])
        returned_docs = list(dict.fromkeys(match.get("doc_title", "Unknown") for match in matches))
        expected_docs = set(case["expected_docs"])
        passed = not returned_docs if not expected_docs else bool(expected_docs.intersection(returned_docs))
        results.append({**case, "returned_docs": returned_docs, "scores": [round(match.get("similarity", 0), 3) for match in matches], "passed": passed})

    positive = [result for result in results if result["expected_docs"]]
    negative = [result for result in results if not result["expected_docs"]]
    stats = {
        "cases": len(results),
        "passed": sum(result["passed"] for result in results),
        "retrieval_hit_rate": sum(result["passed"] for result in positive) / len(positive) if positive else 0,
        "no_match_accuracy": sum(result["passed"] for result in negative) / len(negative) if negative else 0,
    }
    return results, stats


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results, stats = evaluate(cases)
    print(f"RAG evaluation ({stats['cases']} cases, min similarity {MIN_SIMILARITY:.2f})")
    print(f"Pass rate: {stats['passed']}/{stats['cases']}")
    print(f"Retrieval hit rate: {stats['retrieval_hit_rate']:.1%}")
    print(f"No-match accuracy: {stats['no_match_accuracy']:.1%}")
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
