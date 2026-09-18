"""LLM tailoring: produce a tailored CV (markdown) and, only if the posting has
application questions, draft answers grounded in the CV.

GROUNDING RULE (non-negotiable, stated in the system prompt): the model reorders,
reweights, and rephrases what is ALREADY in the master CV to match the JD. It
never invents experience, employers, dates, titles, or skills the candidate does
not have. If a JD asks for something absent from the CV, it stays absent. A
fabricated line is worse than a weaker application.
"""
from __future__ import annotations

import json
import logging

from .acquire import Job
from .llm import complete

log = logging.getLogger(__name__)

GROUNDING = (
    "You tailor a candidate's CV to a specific job. ABSOLUTE RULE: use ONLY facts "
    "present in the master CV JSON provided. Reorder, reweight, and rephrase to "
    "match the job description. NEVER invent or imply experience, employers, dates, "
    "titles, metrics, or skills the candidate does not have. If the job asks for "
    "something absent from the CV, leave it absent — do not fabricate it. A weaker "
    "but truthful application is required; a fabricated line is a failure."
)


def _cv_block(master_cv: dict) -> str:
    # Put the static master CV first in the user message so DeepSeek prefix
    # caching applies across jobs within a run.
    return "MASTER CV (source of truth, JSON):\n" + json.dumps(master_cv, ensure_ascii=False)


def tailor_cv(job: Job, master_cv: dict, cfg, client) -> str:
    system = (
        GROUNDING + "\n\nOutput a complete tailored CV in clean Markdown: a short "
        "summary, then Experience (most relevant first, rephrased toward the role), "
        "Skills (ordered by relevance to the JD), and Education. No preamble, no "
        "commentary, Markdown only."
    )
    user = (
        f"{_cv_block(master_cv)}\n\n"
        f"TARGET ROLE: {job.title} at {job.company}\n"
        f"JOB DESCRIPTION:\n{job.description[:6000]}\n\n"
        "Produce the tailored CV now."
    )
    return complete(system, user, max_tokens=int(cfg.llm.get("max_tokens_cv", 1500)),
                    client=client, cfg=cfg)


def draft_answers(job: Job, master_cv: dict, cfg, client) -> list[dict[str, str]]:
    if not job.questions:
        return []
    system = (
        GROUNDING + "\n\nYou are drafting answers to a job application's questions, "
        "grounded strictly in the master CV. Return a JSON array of objects with "
        '"question" and "answer" keys. Each answer is concise (2-5 sentences), first '
        "person, truthful. If the CV lacks the basis for an honest answer, say so "
        "plainly in the answer rather than inventing. Return JSON only."
    )
    qlist = "\n".join(f"- {q}" for q in job.questions)
    user = (
        f"{_cv_block(master_cv)}\n\n"
        f"ROLE: {job.title} at {job.company}\n"
        f"APPLICATION QUESTIONS:\n{qlist}\n\n"
        "Return the JSON array now."
    )
    raw = complete(system, user, max_tokens=int(cfg.llm.get("max_tokens_answers", 800)),
                   client=client, cfg=cfg)
    return _parse_answers(raw, job.questions)


def _parse_answers(raw: str, questions: list[str]) -> list[dict[str, str]]:
    text = raw.strip()
    # tolerate ```json fences
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        data = json.loads(text)
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, dict) and "answer" in item:
                    out.append({"question": str(item.get("question", "")),
                                "answer": str(item.get("answer", ""))})
            if out:
                return out
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: keep the raw text against the first question so nothing is lost.
    log.warning("Could not parse answers JSON; storing raw text.")
    return [{"question": questions[0] if questions else "Answers", "answer": raw.strip()}]


def tailor(job: Job, master_cv: dict, cfg, client) -> Job:
    """Populate job.tailored_cv and job.answers in place."""
    job.tailored_cv = tailor_cv(job, master_cv, cfg, client)
    job.answers = draft_answers(job, master_cv, cfg, client)
    return job
