"""Deterministic, explainable match score.

Both master_match and tailored_match are keyword/skill coverage percentages
(0-100) between a CV text and the JD. We extract candidate required-skill terms
from the JD, then measure what fraction the CV covers. No LLM, no vibe number —
the score is reproducible and defensible.
"""
from __future__ import annotations

import re
from typing import Iterable

# A compact tech/skill lexicon. Multi-word phrases are checked as phrases; the
# rest as word-boundary tokens. Extend freely — the score stays deterministic.
SKILL_LEXICON = {
    # languages
    "python", "go", "golang", "java", "kotlin", "scala", "ruby", "rust", "c++",
    "typescript", "javascript", "php", "elixir", "clojure",
    # backend / infra
    "django", "flask", "fastapi", "rails", "spring", "node", "express",
    "postgresql", "postgres", "mysql", "mongodb", "redis", "cassandra",
    "kafka", "rabbitmq", "grpc", "graphql", "rest", "microservices",
    "docker", "kubernetes", "terraform", "aws", "gcp", "azure",
    "ci/cd", "kubernetes", "serverless", "lambda", "prometheus", "grafana",
    # concepts
    "system design", "distributed systems", "event-driven", "api design",
    "scalability", "observability", "sql", "nosql", "tdd", "agile",
    "machine learning", "data pipelines", "etl", "message queue",
}

_MULTIWORD = {s for s in SKILL_LEXICON if " " in s or "/" in s or "-" in s}
_SINGLE = SKILL_LEXICON - _MULTIWORD

_STOP = {"and", "or", "the", "with", "for", "to", "of", "in", "a", "an", "you",
         "we", "our", "will", "have", "are", "is", "as", "on", "at", "be"}


def _normalize(text: str) -> str:
    return re.sub(r"[^\w+#/.\- ]", " ", (text or "").lower())


def extract_jd_terms(jd_text: str) -> set[str]:
    """Required-skill terms the JD mentions, from the lexicon plus any
    prominent capitalized tech tokens."""
    norm = _normalize(jd_text)
    found: set[str] = set()
    for phrase in _MULTIWORD:
        if phrase in norm:
            found.add(phrase)
    tokens = set(re.findall(r"[a-z][a-z0-9+#.]{1,}", norm))
    found |= (tokens & _SINGLE)
    # Also pick up "X years of <TERM>" style requirements already covered by lexicon.
    if not found:
        # fall back to salient nouns so the score is never divide-by-zero
        found = {t for t in tokens if len(t) > 3 and t not in _STOP}
    return found


def _cv_text_from_terms(terms: Iterable[str], cv_text: str) -> set[str]:
    norm = _normalize(cv_text)
    tokens = set(re.findall(r"[a-z][a-z0-9+#.]{1,}", norm))
    covered = set()
    for term in terms:
        if " " in term or "/" in term or "-" in term:
            if term in norm:
                covered.add(term)
        elif term in tokens:
            covered.add(term)
    return covered


def coverage_score(cv_text: str, jd_text: str) -> int:
    """Percentage of JD required-skill terms present in the CV (0-100)."""
    terms = extract_jd_terms(jd_text)
    if not terms:
        return 0
    covered = _cv_text_from_terms(terms, cv_text)
    return round(100 * len(covered) / len(terms))


def cv_to_text(cv: dict) -> str:
    """Flatten the structured master CV into one searchable string."""
    parts: list[str] = [cv.get("summary", "")]
    parts.extend(cv.get("skills", []))
    for exp in cv.get("experience", []):
        parts.append(exp.get("title", ""))
        parts.append(exp.get("company", ""))
        parts.extend(exp.get("highlights", []))
        parts.extend(exp.get("stack", []))
    for edu in cv.get("education", []):
        parts.append(edu.get("degree", ""))
    return "\n".join(p for p in parts if p)
