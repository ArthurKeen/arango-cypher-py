"""Synthbank paraphrase: the offline faithfulness guard and the provider loop.

Mock fidelity: :class:`ScriptedProvider` mirrors
``arango_query_core.nl.providers.LLMProvider.generate(system, user) ->
(text, usage_dict)`` exactly, so a change to the protocol breaks these tests.
"""

from __future__ import annotations

import pytest
from arango_query_core.nl.synthbank import SHAPE_CATALOG

from arango_cypher.nl2cypher.synthbank_paraphrase import paraphrase, slot_preserving

SHAPES = {t.name: t for t in SHAPE_CATALOG}


class ScriptedProvider:
    """Returns queued responses in order; records every (system, user) call."""

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append((system, user))
        if not self.responses:
            return "", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


# -- slot_preserving ---------------------------------------------------------


def test_dropping_the_filler_is_rejected() -> None:
    binding = {"filler_value": "The Matrix"}

    assert slot_preserving("Who acted in The Matrix?", SHAPES["category_filter"], binding)
    assert not slot_preserving("Who acted in that film?", SHAPES["category_filter"], binding)


def test_a_reformatted_decimal_passes_but_a_changed_value_fails() -> None:
    binding = {"filler_value": "0,38 EUR"}

    assert slot_preserving("What is the price of 0.38 EUR?", SHAPES["lookup"], binding)
    assert not slot_preserving("What is the price of 0.39 EUR?", SHAPES["lookup"], binding)
    assert not slot_preserving("What is the price of 0.38 USD?", SHAPES["lookup"], binding)


def test_a_count_must_still_read_as_a_count() -> None:
    binding = {"filler_value": "Keanu Reeves"}

    assert slot_preserving("How many people follow Keanu Reeves?", SHAPES["scalar_count"], binding)
    assert not slot_preserving("Which people follow Keanu Reeves?", SHAPES["scalar_count"], binding)


def test_a_flipped_superlative_is_rejected() -> None:
    """The intent flip the guard exists for: 'highest' paraphrased as 'lowest'."""
    shape = SHAPES["top_n"]

    assert slot_preserving("Which movie has the highest release year?", shape, {})
    assert not slot_preserving("Which movie has the lowest release year?", shape, {})
    assert not slot_preserving("Which movie was released?", shape, {})


def test_offset_must_keep_its_ordinal() -> None:
    """Dropping 'second' silently turns the question into top_n."""
    shape = SHAPES["offset"]

    assert slot_preserving("Which person has the second-highest birth year?", shape, {})
    assert not slot_preserving("Which person has the highest birth year?", shape, {})


def test_lexicon_tokens_match_whole_words() -> None:
    """``no`` (negation lexicon) must not match inside ``know`` or ``not``."""
    shape = SHAPES["negation"]

    assert slot_preserving("Which people have no directed credit?", shape, {})
    assert not slot_preserving("Which people do you know about?", shape, {})


def test_the_threshold_is_a_filler_too() -> None:
    shape = SHAPES["grouped_aggregation"]

    assert slot_preserving("Which movies have more than 3 actors?", shape, {"threshold": 3})
    assert not slot_preserving("Which movies have more than a few actors?", shape, {"threshold": 3})


# -- paraphrase ----------------------------------------------------------------


QUESTION = "How many Person are there for Keanu Reeves?"
BINDING = {"filler_value": "Keanu Reeves"}


def test_no_provider_means_no_paraphrases_and_no_call() -> None:
    assert paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=None) == []


def test_only_guard_passing_distinct_paraphrases_are_kept() -> None:
    provider = ScriptedProvider(
        "How many people are linked to Keanu Reeves?",
        "How many people are linked to Keanu Reeves?",  # repeat: discarded
        "Which people follow him?",  # drops filler and count: rejected
        QUESTION,  # the original: discarded
        "What is the number of people connected to Keanu Reeves?",
        "Count the people associated with Keanu Reeves.",
    )

    got = paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=provider, k=3)

    assert got == [
        "How many people are linked to Keanu Reeves?",
        "What is the number of people connected to Keanu Reeves?",
        "Count the people associated with Keanu Reeves.",
    ]


def test_later_attempts_ask_for_something_new() -> None:
    provider = ScriptedProvider("How many people follow Keanu Reeves?", "Count those tied to Keanu Reeves.")

    paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=provider, k=2)

    assert "Already-produced paraphrases" not in provider.calls[0][1]
    assert "How many people follow Keanu Reeves?" in provider.calls[1][1]


def test_the_budget_is_bounded() -> None:
    provider = ScriptedProvider(*["Which people follow him?"] * 50)

    assert paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=provider, k=3) == []
    assert len(provider.calls) == 15  # k * 5


def test_a_provider_error_costs_one_attempt_not_the_example() -> None:
    provider = ScriptedProvider(RuntimeError("rate limited"), "How many people follow Keanu Reeves?")

    got = paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=provider, k=1)

    assert got == ["How many people follow Keanu Reeves?"]


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_makes_no_calls(k: int) -> None:
    provider = ScriptedProvider("anything")

    assert paraphrase(QUESTION, SHAPES["scalar_count"], BINDING, provider=provider, k=k) == []
    assert provider.calls == []
