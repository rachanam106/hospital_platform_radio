"""Local, evidence-preserving assistance and conservative claim checks.

This deliberately does not inspect images or call a generative model. It records
the radiologist's explicit observations and produces a structured draft scaffold.
The verifier is a research baseline, not a clinical entailment model.
"""
import re
from collections import Counter


NEGATIONS = {"no", "not", "without", "absent", "negative", "none", "free"}
STOP_WORDS = {"a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "with", "there", "and", "or", "to", "for", "evidence"}


def sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if part.strip()]


def _terms(text):
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in STOP_WORDS]


def _cosine(left, right):
    a, b = Counter(_terms(left)), Counter(_terms(right))
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    return sum(a[key] * b[key] for key in common) / ((sum(v * v for v in a.values()) ** .5) * (sum(v * v for v in b.values()) ** .5))


def _polarity(text):
    return any(word in NEGATIONS for word in _terms(text))


def verify_claims(claims, evidence):
    """Return per-claim conservative lexical attribution; similarity is not entailment."""
    evidence = [str(item) for item in evidence]
    results = []
    for claim in claims:
        ranked = sorted(((index, _cosine(claim, item)) for index, item in enumerate(evidence)), key=lambda pair: pair[1], reverse=True)
        best_index, score = ranked[0] if ranked else (None, 0.0)
        shared = set(_terms(claim)) & (set(_terms(evidence[best_index])) if best_index is not None else set())
        status, reason = "unsupported", "No supplied observation provides a close lexical match; absence of evidence is not evidence of falsity."
        if score >= .48:
            claim_negative = _polarity(claim)
            evidence_negative = _polarity(evidence[best_index])
            if claim_negative != evidence_negative and len(shared - NEGATIONS) >= 2:
                status, reason = "contradicted", "The closest supplied observation expresses the opposite polarity for overlapping finding terms."
            elif score >= .68:
                status, reason = "supported", "Close lexical attribution to a supplied radiologist observation; this is not clinical entailment."
            else:
                status, reason = "uncertain", "Partial lexical overlap; a human must determine whether the observation supports this claim."
        elif score >= .15:
            status, reason = "uncertain", "Weak or ambiguous lexical overlap; no support conclusion is made."
        results.append({"claim": claim, "status": status, "evidence_index": best_index if score >= .25 else None, "similarity_signal": round(score, 4), "reason": reason})
    return results


def build_assist(evidence_input, modality):
    observations = evidence_input["observations"]
    findings = "\n".join(f"{item['anatomy']} — {item['polarity']}: {item['text']}" for item in observations)
    claims = [sentence for item in observations for sentence in sentences(item["text"])]
    evidence = [item["text"] for item in observations]
    sections = {
        "clinical_indication": evidence_input.get("clinical_indication", ""),
        "technique": evidence_input.get("technique", ""),
        "findings": findings,
        "impression": "",
    }
    checks = verify_claims(claims, evidence)
    statuses = {item["status"] for item in checks}
    overall = "contains_contradictions" if "contradicted" in statuses else "contains_unsupported_claims" if "unsupported" in statuses else "uncertain_claims" if "uncertain" in statuses else "attributed_to_supplied_observations"
    return sections, {
        "schema": "claim-attribution-v1", "modality": modality,
        "assessment": overall, "human_review_required": True, "claims": checks,
        "limitations": ["No images or CT volume were processed.", "Similarity is not entailment or a clinical probability.", "Impression is intentionally left blank for the radiologist."],
    }
