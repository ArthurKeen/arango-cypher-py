"""Pluggable LLM provider interface for the NL→Cypher / NL→AQL pipelines.

Keeps network-facing concerns (HTTP, auth, model selection) isolated from
the prompt-construction and schema-analysis code in ``_core`` and
``_aql``.  Per PRD §1.2, providers receive pre-rendered ``system`` and
``user`` strings and never touch physical schema details on their own.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM backends that generate text from a prompt.

    ``generate`` accepts pre-rendered ``system`` and ``user`` strings and
    returns ``(response_text, usage_dict)`` where *usage_dict* contains
    ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``.  The
    caller is responsible for rendering the system prompt (see
    :class:`arango_cypher.nl2cypher.PromptBuilder` for the NL→Cypher
    path); providers no longer format schema context themselves, which
    keeps the §1.2 invariant auditable at a single site and lets future
    waves extend the prompt without touching every provider.
    """

    def generate(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:
        """Return ``(content, usage_dict)`` for the given system/user pair."""
        ...


class _BaseChatProvider:
    """Shared HTTP-based chat completion logic for OpenAI-compatible APIs."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        timeout: int = 30,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._extra_headers = extra_headers or {}

    def _chat(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:
        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {}) or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = 0
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0) or 0)
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "cached_tokens": cached,
        }


class OpenAIProvider(_BaseChatProvider):
    """OpenAI-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENAI_API_KEY``
      - ``base_url`` / ``OPENAI_BASE_URL``  (default: OpenAI)
      - ``model``   / ``OPENAI_MODEL``      (default: gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        return self._chat(system, user)


class OpenRouterProvider(_BaseChatProvider):
    """OpenRouter-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENROUTER_API_KEY``
      - ``model``   / ``OPENROUTER_MODEL``   (default: openai/gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            model=model or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        return self._chat(system, user)


_ANTHROPIC_CACHE_BREAKPOINT = "## Examples"
"""Boundary between the cached prefix (prelude + schema) and the
per-request suffix (few-shot examples, resolved entities, user question).

PromptBuilder renders ``## Examples`` as the first *per-question*
section, so everything above that header is static per mapping and a
safe target for Anthropic's ``cache_control: {type: "ephemeral"}``
directive.  If neither WP-25.1 few-shot examples nor WP-25.2 resolved
entities are present, the whole system prompt is static and we mark it
all cached.
"""


def split_system_for_anthropic_cache(system: str) -> list[dict[str, Any]]:
    """Produce Anthropic's `system: [...]` content blocks for prompt caching.

    Splits ``system`` at the first :data:`_ANTHROPIC_CACHE_BREAKPOINT`
    (``## Examples``) into a cached prefix and an uncached suffix.  When
    no breakpoint is present the whole string is a single cached block.

    Returned shape matches Anthropic's Messages API::

        [
          {"type": "text", "text": "<prelude + schema>",
           "cache_control": {"type": "ephemeral"}},
          {"type": "text", "text": "<examples + resolved entities>"},
        ]

    This is exposed as a standalone function so the future
    :class:`AnthropicProvider` and downstream tests can share the exact
    same split logic.
    """
    if not system:
        return [{"type": "text", "text": "", "cache_control": {"type": "ephemeral"}}]
    idx = system.find(_ANTHROPIC_CACHE_BREAKPOINT)
    if idx == -1:
        return [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    prefix = system[:idx].rstrip("\n")
    suffix = system[idx:]
    blocks: list[dict[str, Any]] = [{
        "type": "text",
        "text": prefix,
        "cache_control": {"type": "ephemeral"},
    }]
    if suffix:
        blocks.append({"type": "text", "text": suffix})
    return blocks


class AnthropicProvider:
    """Stub Claude provider exposing Anthropic's `cache_control` shape (WP-25.4).

    Not wired end-to-end yet — the point of this class is to pin the
    *interface* for future work so downstream callers can rely on the
    cache-control split even before the HTTP path is implemented.  Call
    :meth:`build_system_blocks` to get the Anthropic-style payload that a
    full implementation would pass as the ``system`` field of the
    Messages API.

    The standalone :func:`split_system_for_anthropic_cache` is the
    source of truth for the split logic; this class is a thin wrapper
    that records construction-time config so an HTTP path can be added
    later without changing the public surface.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-3-5-sonnet-latest",
        )

    def build_system_blocks(self, system: str) -> list[dict[str, Any]]:
        """Return Anthropic `system=[...]` blocks with cache_control markers."""
        return split_system_for_anthropic_cache(system)

    def generate(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:  # pragma: no cover - stub
        raise NotImplementedError(
            "AnthropicProvider is a stub (WP-25.4). Wire up anthropic-py "
            "in a follow-up and pass build_system_blocks(system) as the "
            "`system` field of the Messages API request."
        )


def get_llm_provider() -> OpenAIProvider | OpenRouterProvider | None:
    """Create an LLM provider from environment configuration.

    Resolution order:
    1. ``LLM_PROVIDER=openai``      → :class:`OpenAIProvider`
    2. ``LLM_PROVIDER=openrouter``  → :class:`OpenRouterProvider`
    3. Auto-detect: if ``OPENROUTER_API_KEY`` is set and ``OPENAI_API_KEY``
       is not, use :class:`OpenRouterProvider`; otherwise
       :class:`OpenAIProvider`.

    Returns ``None`` when no API key is available.
    """
    explicit = os.environ.get("LLM_PROVIDER", "").lower().strip()
    if explicit == "openrouter":
        p = OpenRouterProvider()
        return p if p.api_key else None
    if explicit == "openai":
        p = OpenAIProvider()
        return p if p.api_key else None

    has_openai = bool(os.environ.get("OPENAI_API_KEY", ""))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    if has_openrouter and not has_openai:
        return OpenRouterProvider()
    if has_openai:
        return OpenAIProvider()
    return None


_DEFAULT_PROVIDER: _BaseChatProvider | None = None
_DEFAULT_PROVIDER_RESOLVED = False


def _get_default_provider() -> _BaseChatProvider | None:
    """Lazily create a default LLM provider via :func:`get_llm_provider`."""
    global _DEFAULT_PROVIDER, _DEFAULT_PROVIDER_RESOLVED
    if _DEFAULT_PROVIDER_RESOLVED:
        return _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = get_llm_provider()
    _DEFAULT_PROVIDER_RESOLVED = True
    return _DEFAULT_PROVIDER
