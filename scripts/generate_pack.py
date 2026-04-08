#!/usr/bin/env python
"""CLI for the persona pack calibration pipeline.

End-to-end: web research → LLM class extraction → parallel persona
generation → assemble v3 yaml → load-validate → write to disk.

Examples:

    # Generate the canonical ashare pack into the default path
    uv run python scripts/generate_pack.py --market ashare

    # Generate us-equity with a fixed seed and explicit output path
    uv run python scripts/generate_pack.py --market us-equity \\
        --output personas/us-equity-auto-2026Q2.yaml --seed 42

    # Force fresh research (skip cache)
    uv run python scripts/generate_pack.py --market crude-oil-wti --no-cache

    # Compare auto-generated pack to a hand-written reference pack
    uv run python scripts/generate_pack.py --market ashare \\
        --compare personas/ashare.yaml

The output yaml is always a `<market>-auto-<date>.yaml` next to the
hand-written pack. It is never overwritten in place — humans review
the diff and promote it manually if it looks good.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssfish.persona import load_personas
from ssfish.persona_factory import (
    KNOWN_MARKETS,
    MarketDescriptor,
    generate_persona_pack,
)


log = logging.getLogger("ssfish.generate_pack")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a v2 persona pack from web research",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--market",
        required=True,
        help=f"Market slug. Known: {sorted(KNOWN_MARKETS.keys())}. "
             f"Or pass --search-terms / --native-search-terms / --locale to define "
             f"a new market on the fly.",
    )
    p.add_argument(
        "--search-terms",
        nargs="*",
        default=None,
        help="(optional) Override search terms for an unknown market",
    )
    p.add_argument(
        "--native-search-terms",
        nargs="*",
        default=None,
        help="(optional) Native-language search terms (e.g., '中国A股' for ashare)",
    )
    p.add_argument(
        "--locale",
        default=None,
        help="(optional) BCP47 locale tag (defaults to en-US for unknown markets)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Output yaml path. Defaults to personas/<market>-auto-<date>.yaml",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Force fresh web research (skip cached research data)",
    )
    p.add_argument("--seed", type=int, default=42, help="Deterministic seed for LLM calls")
    p.add_argument(
        "--max-urls",
        type=int,
        default=20,
        help="Max URLs to fetch in research stage",
    )
    p.add_argument(
        "--compare",
        default=None,
        help="Compare the generated pack to an existing hand-written pack",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p.parse_args()


def _resolve_descriptor(args: argparse.Namespace) -> MarketDescriptor:
    if args.market in KNOWN_MARKETS:
        return KNOWN_MARKETS[args.market]
    if not args.search_terms:
        raise SystemExit(
            f"ERROR: market '{args.market}' is not in KNOWN_MARKETS. "
            f"Pass --search-terms / --native-search-terms / --locale to define it."
        )
    return MarketDescriptor(
        slug=args.market,
        locale=args.locale or "en-US",
        search_terms=args.search_terms,
        native_search_terms=args.native_search_terms or [],
    )


def _print_summary(summary: dict) -> None:
    print("\n=== Pipeline Summary ===")
    print(f"  Market:           {summary['market']}")
    print(f"  Output:           {summary['output_path']}")
    print(f"  Personas:         {summary['n_personas']}")
    print(f"  Classes (Stg1):   {summary['n_classes_from_research']}")
    print(f"  Sources fetched:  {summary['n_sources_fetched']}")
    print(f"  Wall clock:       {summary['elapsed_seconds']}s")
    print(f"  Cost:             ${summary['cost_usd']}")
    print(f"  Validates:        {'✓' if summary['validates'] else '✗'} ({summary['validation_message']})")


def _print_compare(generated_path: Path, reference_path: Path) -> None:
    """Show a structural diff between auto-generated and hand-written packs."""
    try:
        gen = load_personas(generated_path)
        ref = load_personas(reference_path)
    except Exception as exc:
        print(f"\n[compare] failed to load packs: {exc}")
        return

    gen_ids = {p.id for p in gen}
    ref_ids = {p.id for p in ref}
    common = gen_ids & ref_ids
    only_gen = gen_ids - ref_ids
    only_ref = ref_ids - gen_ids

    print(f"\n=== Compare: {generated_path.name} vs {reference_path.name} ===")
    print(f"  Generated: {len(gen)} personas")
    print(f"  Reference: {len(ref)} personas")
    print(f"  Common ids: {len(common)}")
    print()

    if only_gen:
        print(f"  Only in generated ({len(only_gen)}):")
        for pid in sorted(only_gen):
            print(f"    + {pid}")
    if only_ref:
        print(f"  Only in reference ({len(only_ref)}):")
        for pid in sorted(only_ref):
            print(f"    - {pid}")

    print(f"\n  Sums comparison:")
    for label, pack in [("generated", gen), ("reference", ref)]:
        sum_h = sum((p.market_share.by_holdings or 0) for p in pack if p.market_share)
        sum_v = sum((p.market_share.by_volume or 0) for p in pack if p.market_share)
        sum_a = sum((p.market_share.by_account_count or 0) for p in pack if p.market_share)
        strategic = sum(1 for p in pack if p.contributes_to_strategic_signal)
        print(f"    {label:10s}  by_holdings={sum_h:.3f}  by_volume={sum_v:.3f}  "
              f"by_account_count={sum_a:.3f}  strategic={strategic}")


async def amain() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    descriptor = _resolve_descriptor(args)
    output = Path(args.output) if args.output else None

    print(f"# Generating pack for market='{descriptor.slug}' (locale={descriptor.locale})")
    print(f"# Search terms (en):     {descriptor.search_terms}")
    print(f"# Search terms (native): {descriptor.native_search_terms}")
    print(f"# Cache:                 {'disabled' if args.no_cache else 'enabled'}")
    print()

    output_path, summary = await generate_persona_pack(
        descriptor=descriptor,
        output_path=output,
        use_cache=not args.no_cache,
        seed=args.seed,
        max_urls=args.max_urls,
    )

    _print_summary(summary)

    if args.compare:
        _print_compare(output_path, Path(args.compare))

    print()
    print(f"# Pack written to: {output_path}")
    print(f"# To use it:")
    print(f"#   uv run python scripts/run_one.py --personas {output_path} --mode sandbox ...")
    print(f"#   OR human-review the diff vs the hand-written pack and promote:")
    print(f"#     cp {output_path} personas/{descriptor.slug}.yaml  # if approved")

    return 0 if summary["validates"] else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
