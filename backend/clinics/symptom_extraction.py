"""Small rule-based symptom span extractor for synthetic/prototype use only."""
import re


PATTERNS = {
    "shortness of breath": r"\b(?:shortness of breath|breathlessness|dyspnea|sob)\b",
    "chest pain": r"\bchest pain\b",
    "cough": r"\bcough(?:ing)?\b",
    "fever": r"\bfever(?:ish)?\b",
    "wheezing": r"\bwheez(?:e|es|ing)\b",
    "hemoptysis": r"\bhemoptysis\b",
    "sputum": r"\bsputum\b",
    "fatigue": r"\bfatigue\b",
    "nausea": r"\bnausea\b",
    "vomiting": r"\bvomit(?:ing)?\b",
    "headache": r"\bheadache\b",
    "dizziness": r"\bdizz(?:y|iness)\b",
    "weakness": r"\bweakness\b",
}
NEGATION = re.compile(r"\b(?:no|not|denies|denied|without|never|negative for|doesn't have|does not have)\b", re.I)
UNCERTAINTY = re.compile(r"\b(?:possible|possibly|perhaps|maybe|might have|may have|uncertain about|not sure if)\b|\?", re.I)
DURATION = re.compile(r"\b(?:for|since|over the past|lasting)\s+(?:the\s+)?[\w-]+(?:\s+[\w-]+){0,3}", re.I)


def _sentence_bounds(text, start, end):
    left = max(text.rfind(mark, 0, start) for mark in (".", "!", "?", "\n")) + 1
    stops = [position for mark in (".", "!", "?", "\n") if (position := text.find(mark, end)) >= 0]
    right = min(stops) if stops else len(text)
    return left, right


def extract_symptoms(text):
    """Extract only a small explicit vocabulary; return spans/attributes, never diagnoses."""
    mentions = []
    for normalized, pattern in PATTERNS.items():
        for match in re.finditer(pattern, text, re.I):
            left, right = _sentence_bounds(text, match.start(), match.end())
            context = text[left:right]
            relative_start = match.start() - left
            prefix = context[max(0, relative_start - 64):relative_start]
            contrast = list(re.finditer(r"\b(?:but|however|although|except)\b|;", prefix, re.I))
            if contrast:
                prefix = prefix[contrast[-1].end():]
            if UNCERTAINTY.search(prefix):
                assertion = "uncertain"
            elif NEGATION.search(prefix):
                assertion = "absent"
            else:
                assertion = "present"
            duration_matches = list(DURATION.finditer(context))
            duration_match = min(duration_matches, key=lambda item: min(abs(item.start() - relative_start), abs(item.end() - relative_start))) if duration_matches and min(min(abs(item.start() - relative_start), abs(item.end() - relative_start)) for item in duration_matches) <= 80 else None
            mentions.append({
                "text": match.group(0), "normalized": normalized,
                "start": match.start(), "end": match.end(),
                "assertion": assertion,
                "duration_text": duration_match.group(0) if duration_match else "",
                "confidence": None,
            })
    return sorted(mentions, key=lambda item: (item["start"], item["end"]))
