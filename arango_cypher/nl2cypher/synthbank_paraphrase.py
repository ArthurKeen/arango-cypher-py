"""Paraphrase synthbank questions, guarded for faithfulness (step 4).

Port of the paraphrase half of ``arango-sparql-py``'s bank generator. A
templated question ("How many Person are there for The Matrix?") is faithful
by construction but reads like a template; paraphrases make BM25 retrieval
match how people actually ask. The risk is a paraphrase that *changes* the
question — drops the filler, flips "highest" to "lowest", loses the count —
and so teaches the model a wrong pairing. :func:`slot_preserving` is the
primary guard against that: pure, offline, deterministic, never an LLM.

The one change from the SPARQL side is the provider. There it constructed an
OpenAI-compatible client whenever an API key was set; here any
:class:`arango_query_core.nl.providers.LLMProvider` is accepted, and one must
be passed explicitly — a configured credential is not consent to spend on it.

The guard and prompt are language-agnostic (text plus a ``ShapeTemplate``).
They live here, not in ``arango_query_core``, only because promoting them is a
cross-repo release; they are a promotion candidate once SPARQL adopts the
provider protocol too.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from arango_query_core.nl.synthbank import ShapeTemplate

logger = logging.getLogger(__name__)

__all__ = ["paraphrase", "slot_preserving"]

#: Ranking shapes are always rendered descending/"highest" by the binder, so
#: the guard checks that fixed direction rather than a per-example value.
_SUPERLATIVE_POSITIVE = frozenset({"most", "highest", "largest", "greatest", "top"})
_SUPERLATIVE_NEGATIVE = frozenset(
    {"least", "lowest", "smallest", "cheapest", "fewest", "minimum", "bottom", "worst"}
)

#: Binding keys whose value a faithful paraphrase must keep verbatim.
_FILLER_BINDING_KEYS = ("filler_value", "threshold")

_SYSTEM_PROMPT = (
    "Paraphrase the given natural-language question in a single sentence. Keep "
    "every named entity, number, currency amount, and unit EXACTLY as written in "
    "the question -- do NOT reformat a number (e.g. do not change a decimal "
    "separator or add/drop digits), do NOT translate, abbreviate, or drop any "
    "currency, and do NOT shorten or rephrase any named entity. Preserve the "
    "question's exact intent (do not drop or flip any filter, count, ordering, or "
    "negation). Return ONLY the paraphrased question text, no extra commentary."
)


def _normalize(text: str) -> str:
    """Unify a decimal comma to a period and collapse whitespace — nothing more.

    Deliberately narrow: no digit, currency code or token is ever stripped, so
    a paraphrase that changes a value (``0,38`` -> ``0,39``) or a currency
    (``EUR`` -> ``USD``) still fails the filler check.
    """
    return re.sub(r"\s+", " ", re.sub(r"(?<=\d),(?=\d)", ".", text)).strip()


def _contains_token(text: str, token: str) -> bool:
    """Whole-word match, so ``no`` does not match inside ``not`` or ``know``."""
    return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text) is not None


def slot_preserving(paraphrase_text: str, template: ShapeTemplate, binding: dict[str, Any]) -> bool:
    """Whether *paraphrase_text* keeps the question *template* + *binding* asks.

    Faithful iff:

    1. every bound filler (``filler_value``, ``threshold``) still appears,
       case-insensitively, modulo decimal-comma and whitespace;
    2. for ``top_n`` / ``offset``, no opposite-direction superlative appears and
       a same-direction one does — the intent flip this guard exists to catch —
       and ``offset`` also keeps its ordinal, since dropping "second" silently
       turns it into a ``top_n`` question (a check the SPARQL guard lacked);
    3. otherwise, for a shape with an ``intent_lexicon``, at least one of its
       tokens appears (a count must still read as a count).

    Tokens match as whole words. The SPARQL guard matched substrings, which
    let ``no`` (negation lexicon) match inside ``know`` or ``not``, and let
    ``top`` match inside ``stop``.
    """
    text = _normalize(paraphrase_text.lower())

    for key in _FILLER_BINDING_KEYS:
        value = binding.get(key)
        if value is None:
            continue
        if _normalize(str(value).strip().lower()) not in text:
            return False

    if template.name in ("top_n", "offset"):
        if any(_contains_token(text, t) for t in _SUPERLATIVE_NEGATIVE):
            return False
        if not any(_contains_token(text, t) for t in _SUPERLATIVE_POSITIVE):
            return False
        if template.name == "offset":
            return any(_contains_token(text, t) for t in template.intent_lexicon)
        return True

    if not template.intent_lexicon:
        return True
    return any(_contains_token(text, t) for t in template.intent_lexicon)


def paraphrase(
    question: str,
    template: ShapeTemplate,
    binding: dict[str, Any],
    *,
    provider: Any,
    k: int = 3,
) -> list[str]:
    """Up to *k* guard-passing paraphrases of *question*.

    *provider* is an ``LLMProvider`` (``generate(system, user) -> (text,
    usage)``). Candidates failing :func:`slot_preserving`, repeats, and empty
    responses are discarded; the budget is ``k * 5`` attempts, after which the
    example ships with fewer paraphrases (or none) — never a crash. From the
    second accepted paraphrase on, the prompt lists those already accepted and
    asks for a distinct one; without that, the SPARQL regen runs settled into
    near-repeats and never reached *k*.

    A provider error on one attempt is logged and counted against the budget
    rather than aborting the bank: one flaky call must not discard an
    otherwise good example.
    """
    if provider is None or k <= 0:
        return []

    accepted: list[str] = []
    seen = {question.strip().lower()}
    for _attempt in range(k * 5):
        if len(accepted) >= k:
            break
        user = question
        if accepted:
            already = "\n".join(f"- {p}" for p in accepted)
            user = (
                f"{question}\n\nAlready-produced paraphrases (produce a NEW, DISTINCT "
                f"paraphrase -- not a near-repeat of any of these):\n{already}"
            )
        try:
            text, _usage = provider.generate(_SYSTEM_PROMPT, user)
        except Exception as exc:  # noqa: BLE001 — logged and budgeted, see docstring
            logger.warning("synthbank paraphrase: provider call failed for %r: %s", question, exc)
            continue
        candidate = (text or "").strip()
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        if slot_preserving(candidate, template, binding):
            accepted.append(candidate)
        else:
            logger.debug("synthbank paraphrase rejected by slot guard: %r", candidate)
    return accepted
