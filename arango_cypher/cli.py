"""CLI entry point for arango-cypher-py: Cypher → AQL transpiler."""

# ruff: noqa: B008  — typer.Option / typer.Argument in signatures is idiomatic
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from arango_query_core import CoreError, MappingBundle, MappingSource, mapping_from_wire_dict
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from ._env import read_arango_password

app = typer.Typer(
    name="arango-cypher-py",
    help="Cypher → AQL transpiler for ArangoDB",
    no_args_is_help=True,
)
console = Console(stderr=True)
out = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_mapping(
    mapping_file: Path | None,
    mapping_json: str | None,
) -> MappingBundle | None:
    """Build a MappingBundle from a file path or inline JSON string."""
    raw: dict[str, Any] | None = None

    if mapping_file is not None:
        try:
            raw = json.loads(mapping_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            console.print(f"[red]Failed to read mapping file: {exc}[/red]")
            raise typer.Exit(1) from exc
    elif mapping_json is not None:
        try:
            raw = json.loads(mapping_json)
        except json.JSONDecodeError as exc:
            console.print(f"[red]Invalid mapping JSON: {exc}[/red]")
            raise typer.Exit(1) from exc

    if raw is None:
        return None

    return mapping_from_wire_dict(
        raw,
        source=MappingSource(
            kind="explicit",
            notes=f"from {mapping_file}" if mapping_file else "inline JSON",
        ),
    )


def _read_cypher(cypher: str | None) -> str:
    """Return cypher from argument or stdin; exit if empty."""
    if cypher is not None:
        return cypher
    if not sys.stdin.isatty():
        cypher = sys.stdin.read().strip()
    if not cypher:
        console.print("[red]No Cypher query provided. Pass as argument or pipe via stdin.[/red]")
        raise typer.Exit(1)
    return cypher


def _connection_target(host: str | None, port: int | None) -> tuple[str, str]:
    """``(url, auth_method)`` from flags, then ``ARANGO_URL``, then host/port env.

    Explicit ``--host`` / ``--port`` win, as they always have. Otherwise
    ``ARANGO_URL`` — the variable the service and the deploy tooling read —
    names the coordinator, so an HTTPS cluster is reachable at all (the old
    ``http://host:port`` form never was). Auth is JWT for HTTPS (platform
    clusters such as prod.demo refuse HTTP Basic) and Basic for HTTP;
    ``ARANGO_AUTH_METHOD=basic|jwt`` overrides.
    """
    if host or port:
        url = f"http://{host or os.getenv('ARANGO_HOST', 'localhost')}:{port or int(os.getenv('ARANGO_PORT', '8529'))}"
    else:
        url = os.getenv("ARANGO_URL", "").strip() or (
            f"http://{os.getenv('ARANGO_HOST', 'localhost')}:{os.getenv('ARANGO_PORT', '8529')}"
        )
    method = os.getenv("ARANGO_AUTH_METHOD", "").strip().lower()
    if method not in ("basic", "jwt"):
        method = "jwt" if url.lower().startswith("https://") else "basic"
    return url.rstrip("/"), method


def _connect(
    host: str | None,
    port: int | None,
    db: str | None,
    user: str | None,
    password: str | None,
) -> Any:
    """Create a python-arango StandardDatabase from flags / env vars / defaults."""
    from arango import ArangoClient

    url, auth_method = _connection_target(host, port)
    d: str = db or os.getenv("ARANGO_DB") or "_system"
    u: str = user or os.getenv("ARANGO_USER") or "root"
    pw = password if password is not None else read_arango_password(caller="arango_cypher.cli")
    client = ArangoClient(hosts=url)
    return client.db(d, username=u, password=pw, auth_method=auth_method)


def _parse_params(params_json: str | None) -> dict[str, Any] | None:
    if params_json is None:
        return None
    try:
        return json.loads(params_json)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid --params JSON: {exc}[/red]")
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@app.command()
def translate(
    cypher: str = typer.Argument(None, help="Cypher query (reads stdin if omitted)"),
    mapping_file: Path = typer.Option(None, "--mapping-file", "-m", help="Path to mapping JSON file"),
    mapping_json: str = typer.Option(None, "--mapping-json", help="Inline mapping JSON"),
    extensions: bool = typer.Option(True, "--extensions/--no-extensions"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    params_json: str = typer.Option(None, "--params", "-p", help="Query parameters as JSON"),
) -> None:
    """Translate Cypher to AQL. Reads from stdin if no argument given."""
    from arango_cypher.api import translate as do_translate

    cypher = _read_cypher(cypher)
    bundle = _load_mapping(mapping_file, mapping_json)
    if bundle is None:
        console.print(
            "[red]No mapping provided.[/red] "
            "Use [bold]--mapping-file[/bold] or [bold]--mapping-json[/bold], "
            "or use the [bold]run[/bold] subcommand to auto-acquire from a live database."
        )
        raise typer.Exit(1)

    params = _parse_params(params_json)

    try:
        result = do_translate(cypher, mapping=bundle, params=params)
    except CoreError as exc:
        console.print(f"[red]Translation error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        out.print(json.dumps({"aql": result.aql, "bind_vars": result.bind_vars}, indent=2))
    else:
        out.print(Syntax(result.aql, "sql", theme="monokai"))
        if result.bind_vars:
            out.print("\n[bold]Bind variables:[/bold]")
            out.print(json.dumps(result.bind_vars, indent=2))


@app.command()
def run(
    cypher: str = typer.Argument(None, help="Cypher query (reads stdin if omitted)"),
    mapping_file: Path = typer.Option(None, "--mapping-file", "-m", help="Path to mapping JSON file"),
    mapping_json: str = typer.Option(None, "--mapping-json", help="Inline mapping JSON"),
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    params_json: str = typer.Option(None, "--params", "-p", help="Query parameters as JSON"),
) -> None:
    """Translate and execute Cypher against ArangoDB."""
    from arango_cypher.api import execute as do_execute
    from arango_cypher.schema_acquire import get_mapping

    cypher = _read_cypher(cypher)
    params = _parse_params(params_json)

    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    bundle = _load_mapping(mapping_file, mapping_json)
    if bundle is None:
        console.print("[dim]No mapping file provided — acquiring from database…[/dim]")
        try:
            bundle = get_mapping(database)
        except Exception as exc:
            console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
            raise typer.Exit(1) from exc

    try:
        cursor = do_execute(cypher, db=database, mapping=bundle, params=params)
        rows = list(cursor)
    except CoreError as exc:
        console.print(f"[red]Execution error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        out.print(json.dumps(rows, indent=2, default=str))
    else:
        _print_result_table(rows)


@app.command()
def mapping(
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    strategy: str = typer.Option("auto", "--strategy", "-s", help="auto | analyzer | heuristic"),
    owl_output: Path = typer.Option(None, "--owl-output", help="Write OWL Turtle to file"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Print mapping summary for a database."""
    from arango_query_core import MappingResolver

    from arango_cypher.schema_acquire import get_mapping

    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    try:
        bundle = get_mapping(database, strategy=strategy, include_owl=bool(owl_output))
    except Exception as exc:
        console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
        raise typer.Exit(1) from exc

    resolver = MappingResolver(bundle)
    summary = resolver.schema_summary()

    if json_output:
        out.print(json.dumps(summary, indent=2, default=str))
    else:
        _print_mapping_summary(summary)

    if owl_output and bundle.owl_turtle:
        owl_output.write_text(bundle.owl_turtle)
        console.print(f"[green]OWL Turtle written to {owl_output}[/green]")


@app.command()
def synthbank(
    output: Path = typer.Option(..., "--output", "-o", help="Bank YAML to write"),
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    mapping_file: Path = typer.Option(
        None, "--mapping-file", "-m", help="Mapping JSON (default: acquire live)"
    ),
    seed: int = typer.Option(0, "--seed", help="Sampling seed; same seed + data = same bank"),
    paraphrase: bool = typer.Option(
        False, "--paraphrase/--no-paraphrase", help="Add LLM paraphrases (uses LLM_PROVIDER / API-key env)"
    ),
    k: int = typer.Option(3, "--k", help="Paraphrases per example"),
    report_file: Path = typer.Option(None, "--report", help="Write the per-shape yield report as JSON"),
) -> None:
    """Generate a synthetic few-shot bank from a live database.

    Every example is sampled from real data, rendered as Cypher, translated and
    executed; only non-empty results are kept. Load the bank by adding its path
    to NL2CYPHER_FEWSHOT_BANKS.
    """
    from arango_cypher.nl2cypher.synthbank_binder import (
        TranspilingExecutor,
        generate_bank_with_report,
        write_bank,
    )
    from arango_cypher.schema_acquire import get_mapping

    provider = None
    if paraphrase:
        from arango_query_core.nl.providers import get_llm_provider

        provider = get_llm_provider()
        if provider is None:
            console.print("[red]--paraphrase needs an LLM provider: set LLM_PROVIDER and its API key.[/red]")
            raise typer.Exit(1)

    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    bundle = _load_mapping(mapping_file, None)
    if bundle is None:
        try:
            bundle = get_mapping(database)
        except Exception as exc:
            console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
            raise typer.Exit(1) from exc

    try:
        bank, report = generate_bank_with_report(
            bundle, TranspilingExecutor(database, bundle), seed=seed, provider=provider, k_paraphrases=k
        )
    except Exception as exc:
        console.print(f"[red]Bank generation failed:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    written = write_bank(bank, output, source=f"database {database.name!r}, seed {seed}")
    if report_file is not None:
        report_file.write_text(json.dumps(report, indent=2, default=str))

    table = Table(title="Synthbank yield")
    table.add_column("Shape")
    table.add_column("Kept", justify="right")
    table.add_column("Dropped", justify="right")
    for shape, entry in report.items():
        if shape != "_profile":
            table.add_row(shape, str(entry["kept"]), str(entry["dropped"]))
    console.print(table)
    console.print(
        f"[green]{len(bank['examples'])} examples ({written} entries with paraphrases) written to {output}[/green]"
    )


#: LLM backends ``mine-examples`` can be pointed at, by name.
_MINING_PROVIDERS = ("openai", "anthropic", "openrouter")


def _mining_provider(name: str, model: str | None) -> Any:
    """The named provider, or exit: mining spends on an LLM only when told which."""
    from arango_query_core.nl import providers as llm

    factories = {
        "openai": lambda: llm.OpenAIProvider(model=model, temperature=0.0, timeout=120),
        "anthropic": lambda: llm.AnthropicProvider(model=model, temperature=0.0, timeout=120),
        "openrouter": lambda: llm.OpenRouterProvider(model=model, temperature=0.0, timeout=120),
    }
    if name not in factories:
        console.print(f"[red]--provider must be one of {', '.join(_MINING_PROVIDERS)}[/red]")
        raise typer.Exit(1)
    provider = factories[name]()
    if not getattr(provider, "api_key", None):
        console.print(f"[red]No API key for {name}: set its *_API_KEY variable.[/red]")
        raise typer.Exit(1)
    return provider


@app.command("mine-examples")
def mine_examples(
    provider_name: str = typer.Option(
        ..., "--provider", help=f"LLM that drafts question + Cypher: {', '.join(_MINING_PROVIDERS)}"
    ),
    model: str = typer.Option(None, "--model", help="Model id (default: the provider's default)"),
    graph: str = typer.Option(None, "--graph", "-g", help="Only saved queries of this named graph"),
    host: str = typer.Option(None, "--host", help="ArangoDB host (default: ARANGO_URL)"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    mapping_file: Path = typer.Option(
        None, "--mapping-file", "-m", help="Mapping JSON (default: acquire live)"
    ),
    include_builtins: bool = typer.Option(
        False, "--include-builtins", help="Also mine the visualizer's defaults"
    ),
    max_attempts: int = typer.Option(3, "--max-attempts", min=1, max=6, help="Drafts per saved query"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify but write nothing to the database"),
    report_file: Path = typer.Option(
        None, "--report", help="Write every outcome (incl. failed drafts) as JSON"
    ),
) -> None:
    """Mine the database's saved AQL queries for verified NL -> Cypher examples.

    Reads the graph visualizer's saved queries and canvas actions and the query
    editor's saves, runs each read-only as the reference, has the LLM draft a
    question and Cypher, and keeps a draft only if its translated AQL returns the
    same documents. Verified examples are written to the arango_cypher_examples
    collection in the same database, where the Workbench shows them.
    """
    from arango_query_core import mapping_hash

    from arango_cypher.nl2cypher._core import _build_schema_summary
    from arango_cypher.query_mining.miner import mine_database
    from arango_cypher.query_mining.store import collection_name, save_outcomes
    from arango_cypher.schema_acquire import get_mapping

    provider = _mining_provider(provider_name, model)
    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    bundle = _load_mapping(mapping_file, None)
    if bundle is None:
        try:
            bundle = get_mapping(database, graph_name=graph)
        except Exception as exc:
            console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
            raise typer.Exit(1) from exc

    try:
        outcomes, skipped = mine_database(
            database,
            bundle,
            provider,
            schema_summary=_build_schema_summary(bundle),
            graph=graph,
            include_builtins=include_builtins,
            max_attempts=max_attempts,
        )
    except Exception as exc:
        console.print(f"[red]Mining failed:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    table = Table(title=f"Saved queries mined from {database.name!r}" + (f" ({graph})" if graph else ""))
    table.add_column("Saved query")
    table.add_column("Result")
    table.add_column("Question / reason")
    for o in outcomes:
        if o.example is not None:
            table.add_row(
                o.query.name, f"[green]verified[/green] ({o.example.verdict.kind})", o.example.question
            )
        else:
            table.add_row(o.query.name, "[yellow]rejected[/yellow]", o.reason)
    console.print(table)
    for where, why in skipped:
        console.print(f"[dim]skipped {where}: {why}[/dim]")

    if report_file is not None:
        report = [
            {
                "source": f"{o.query.source}/{o.query.key}",
                "name": o.query.name,
                "verified": o.verified,
                "question": o.example.question if o.example else None,
                "cypher": o.example.cypher if o.example else None,
                "reason": o.reason,
                "failed_attempts": [a.__dict__ for a in o.failed_attempts],
            }
            for o in outcomes
        ]
        report_file.write_text(json.dumps({"outcomes": report, "skipped": skipped}, indent=2, default=str))

    verified = sum(o.verified for o in outcomes)
    if dry_run:
        console.print(f"[green]{verified} of {len(outcomes)} verified[/green] (dry run: nothing written)")
        return
    model_id = f"{provider_name}:{getattr(provider, 'model', '') or 'default'}"
    counts = save_outcomes(database, outcomes, mapping_hash=mapping_hash(bundle), model=model_id)
    console.print(
        f"[green]{verified} of {len(outcomes)} verified[/green]; {counts['saved']} saved, "
        f"{counts['removed']} stale removed, {counts['kept']} kept (provider failed) in {collection_name()}"
    )


@app.command()
def doctor(
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
) -> None:
    """Check connectivity, collections, and schema analyzer availability."""
    h = host or os.getenv("ARANGO_HOST", "localhost")
    p = port or int(os.getenv("ARANGO_PORT", "8529"))
    d = db or os.getenv("ARANGO_DB", "_system")

    out.print(f"[bold]Target:[/bold] http://{h}:{p}  db={d}")
    out.print()

    # --- connectivity ---
    try:
        database = _connect(host, port, db, user, password)
        database.version()
        out.print("[green]✓[/green] ArangoDB connection … OK")
    except Exception as exc:
        out.print(f"[red]✗[/red] ArangoDB connection … FAILED ({exc})")
        database = None

    # --- collections ---
    if database is not None:
        try:
            cols = database.collections()
            user_cols = [c["name"] for c in cols if isinstance(c, dict) and not c["name"].startswith("_")]
            out.print(f"[green]✓[/green] Collections … {len(user_cols)} user collection(s)")
            if user_cols:
                out.print(f"    {', '.join(sorted(user_cols)[:20])}")
        except Exception as exc:
            out.print(f"[red]✗[/red] Collections … FAILED ({exc})")

    # --- schema analyzer ---
    try:
        import schema_analyzer  # noqa: F401

        out.print("[green]✓[/green] arangodb-schema-analyzer … installed")
    except ImportError:
        out.print("[yellow]○[/yellow] arangodb-schema-analyzer … not installed (optional)")

    # --- classify ---
    bundle = None
    if database is not None:
        try:
            from arango_cypher.schema_acquire import classify_schema

            schema_type = classify_schema(database)
            out.print(f"[green]✓[/green] Schema classification … {schema_type}")
        except Exception as exc:
            out.print(f"[yellow]○[/yellow] Schema classification … skipped ({exc})")

    # --- VCI checks ---
    if database is not None:
        try:
            from arango_query_core import MappingResolver

            from arango_cypher.schema_acquire import get_mapping

            if bundle is None:
                bundle = get_mapping(database)
            resolver = MappingResolver(bundle)
            vci_issues: list[tuple[str, str, str]] = []
            for rtype in resolver.all_relationship_types():
                rmap = resolver.resolve_relationship(rtype)
                if rmap.get("style") != "GENERIC_WITH_TYPE":
                    continue
                if resolver.has_vci(rtype):
                    continue
                edge_coll = rmap.get("edgeCollectionName", "?")
                type_field = rmap.get("typeField", "type")
                vci_issues.append((rtype, edge_coll, type_field))

            if vci_issues:
                out.print()
                out.print(f"[yellow]⚠[/yellow]  Missing VCI indexes ({len(vci_issues)} relationship(s)):")
                seen_colls: set[str] = set()
                for rtype, edge_coll, type_field in vci_issues:
                    out.print(
                        f"    [yellow]•[/yellow] '{rtype}' on edge collection '{edge_coll}' (type field: '{type_field}')"
                    )
                    if edge_coll not in seen_colls:
                        seen_colls.add(edge_coll)
                        out.print(
                            f"      [dim]Suggestion:[/dim] "
                            f'db.{edge_coll}.ensureIndex({{ type: "persistent", fields: ["{type_field}"], inBackground: true }})'
                        )
            else:
                has_gwt = any(
                    resolver.resolve_relationship(rt).get("style") == "GENERIC_WITH_TYPE"
                    for rt in resolver.all_relationship_types()
                )
                if has_gwt:
                    out.print("[green]✓[/green] VCI indexes … all GENERIC_WITH_TYPE relationships covered")
        except Exception as exc:
            out.print(f"[yellow]○[/yellow] VCI check … skipped ({exc})")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_result_table(rows: list[Any]) -> None:
    if not rows:
        out.print("[dim]No results.[/dim]")
        return

    if isinstance(rows[0], dict):
        table = Table(show_header=True, header_style="bold cyan")
        keys = list(rows[0].keys())
        for k in keys:
            table.add_column(k)
        for row in rows:
            table.add_row(*(str(row.get(k, "")) for k in keys))
        out.print(table)
    else:
        for row in rows:
            out.print(row)


def _print_mapping_summary(summary: dict[str, Any]) -> None:
    entities = summary.get("entities", [])
    rels = summary.get("relationships", [])

    if entities:
        t = Table(title="Entities", show_header=True, header_style="bold cyan")
        t.add_column("Label")
        t.add_column("Collection")
        t.add_column("Style")
        t.add_column("Properties")
        for e in entities:
            props = ", ".join(e.get("properties", {}).keys()) or "—"
            t.add_row(e["label"], e.get("collection", ""), e.get("style", ""), props)
        out.print(t)

    if rels:
        t = Table(title="Relationships", show_header=True, header_style="bold cyan")
        t.add_column("Type")
        t.add_column("Edge Collection")
        t.add_column("Style")
        t.add_column("Domain → Range")
        for r in rels:
            dr = f"{r.get('domain', '?')} → {r.get('range', '?')}"
            t.add_row(r["type"], r.get("edgeCollection", ""), r.get("style", ""), dr)
        out.print(t)

    if not entities and not rels:
        out.print("[dim]Empty mapping — no entities or relationships found.[/dim]")
