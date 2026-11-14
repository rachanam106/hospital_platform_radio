"""Reproducible, synthetic-only test of claim attribution heuristics."""
import json
from pathlib import Path
from datetime import datetime, timezone
from clinics.ai_assistance import verify_claims

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "datasets" / "synthetic_claims.json").read_text(encoding="utf-8"))
OUT = ROOT / "results" / "synthetic_verification.json"


def overlap_baseline(claim, evidence):
    left = set(claim.lower().replace(".", "").split())
    right = set(" ".join(evidence).lower().replace(".", "").split())
    return "supported" if left and len(left & right) / len(left) >= .25 else "unsupported"


def binary_metrics(rows, predicted):
    truth = [row["label"] == "supported" for row in rows]
    guess = [item == "supported" for item in predicted]
    tp = sum(a and b for a, b in zip(truth, guess))
    fp = sum(not a and b for a, b in zip(truth, guess))
    fn = sum(a and not b for a, b in zip(truth, guess))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"supported_precision": round(precision, 4), "supported_recall": round(recall, 4), "supported_f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0}


def evaluate(rows):
    baseline, proposed, details = [], [], []
    for row in rows:
        base = overlap_baseline(row["claim"], row["evidence"])
        result = verify_claims([row["claim"]], row["evidence"])[0]
        baseline.append(base)
        proposed.append(result["status"])
        details.append({**row, "baseline": base, "proposed": result})
    labels = sorted({r["label"] for r in rows} | set(proposed))
    exact = sum(row["label"] == pred for row, pred in zip(rows, proposed)) / len(rows)
    confusion = {actual: {guess: sum(r["label"] == actual and p == guess for r, p in zip(rows, proposed)) for guess in labels} for actual in labels}
    return {"n": len(rows), "baseline_supported_binary": binary_metrics(rows, baseline), "proposed_supported_binary": binary_metrics(rows, proposed), "proposed_exact_status_accuracy_including_abstention": round(exact, 4), "proposed_labels": labels, "proposed_confusion_matrix": confusion, "details": details}


results = {
    "protocol": CASES["protocol"],
    "warning": CASES["warning"],
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "seed": 7,
    "method_note": "Fixed hand-authored cases; this is not a sampled clinical dataset and has no patient-level train/test split. No model training or image processing.",
    "by_modality": {modality: evaluate([row for row in CASES["cases"] if row["modality"] == modality]) for modality in sorted({r["modality"] for r in CASES["cases"]})},
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
print(json.dumps({key: {name: value for name, value in section.items() if name != "details"} for key, section in results["by_modality"].items()}, indent=2))
print(f"Wrote {OUT}")
