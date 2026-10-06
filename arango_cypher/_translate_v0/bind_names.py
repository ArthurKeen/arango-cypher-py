"""Bind-variable names: user parameters versus the names translation generates.

The emitters bind some internal values under fixed names (``@collection``,
``typeValue``, ...). Two things follow. A user parameter of the same name must
not be overwritten, and a ``UNION``, whose branches are translated separately,
must rename an internal name that two branches bind to different values.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError

_BIND_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _param_clash(name: str) -> CoreError:
    shown = f"${name}" if _BIND_NAME_RE.fullmatch(name) else repr(name)
    return CoreError(
        f"Parameter {shown} has the same name as a bind variable the translation generates "
        "internally; rename the parameter",
        code="UNSUPPORTED",
    )


def _refuse_overwritten_params(params: dict[str, Any] | None, bind_vars: dict[str, Any]) -> None:
    """Refuse a user parameter whose value the translation replaced.

    The emitters bind some internal values under fixed names (``typeValue``,
    ``@collection``, ...). A user parameter of the same name was overwritten in
    place, so the user's ``$typeValue`` silently read the label instead.
    """
    for name, value in (params or {}).items():
        if name in bind_vars and bind_vars[name] is not value and bind_vars[name] != value:
            raise _param_clash(name)


#: AQL text a bind-name rename must leave alone: string literals (single or
#: double quoted) and quoted identifiers (backticks or forward ticks), each
#: with AQL's backslash escapes. Cypher string literals are copied into AQL
#: verbatim, so a value such as 'a @@collection b' must not be rewritten. An
#: unterminated quote runs to the end of the text rather than being retried
#: from every later position, which kept the scan linear.
_AQL_QUOTED = r"""'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?|`(?:\\.|[^`\\])*`?|´(?:\\.|[^´\\])*´?"""


def _rename_bind_references(text: str, key: str, renamed: str) -> str:
    """Rewrite references to bind variable *key* in AQL *text* to *renamed*.

    A reference is ``@key`` (``@@name`` for a collection parameter, whose key
    is ``@name``) as a whole token, outside quoted text.
    """
    pattern = re.compile(f"({_AQL_QUOTED})" + r"|(?<![\w@])@" + re.escape(key) + r"(?!\w)")
    return pattern.sub(lambda m: m.group(1) if m.group(1) is not None else "@" + renamed, text)


def _merge_bind_vars(
    target: dict[str, Any],
    source: dict[str, Any],
    text: str,
    *,
    branch_index: int,
    user_params: frozenset[str],
) -> str:
    """Merge one UNION branch's bind vars into *target*; return its AQL text.

    Every branch is translated on its own, so the translator's internal names
    (``@@collection``, ``@uTypeValue``, ...) repeat across branches. Equal
    values merge — that is also how a user ``$param`` shared by branches stays
    one bind variable. An internal name already bound to a *different* value
    (two branches over different collections) is renamed in this branch — in
    its bind vars and, outside quoted text, its AQL — to ``<name>_u<branch>``,
    reusing an earlier renamed slot that holds the same value.

    A user parameter is never renamed: when one shares its name with an
    internal bind variable of a different value, renaming would rebind the
    user's own reference to the internal value, so this refuses instead.

    Why rename afterwards rather than allocate unique names while emitting:
    the core emitters assign fixed names (``bind_vars["@collection"] = ...``
    alongside a literal ``@@collection`` in the text) at some twenty sites;
    routing them all through :func:`naming._pick_bind_key` is a larger change
    than this UNION-only merge, and this rename is quote-aware.
    """
    for k, v in source.items():
        if k not in target:
            target[k] = v
            continue
        if target[k] == v:
            continue
        if k in user_params:
            raise _param_clash(k)
        slot = re.compile(re.escape(k) + r"_u\d+(?:_\d+)?")
        renamed = next(
            (key for key, val in target.items() if val == v and slot.fullmatch(key) and key not in source),
            None,
        )
        if renamed is None:
            renamed = f"{k}_u{branch_index}"
            suffix = 1
            while renamed in target or renamed in source:
                renamed = f"{k}_u{branch_index}_{suffix}"
                suffix += 1
            target[renamed] = v
        text = _rename_bind_references(text, k, renamed)
    return text
