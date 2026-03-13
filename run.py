#!/usr/bin/env python3
"""CLI entry point for the multi-site autoblogger.

Usage:
    python run.py --config configs/aiprofilephotomaker.yaml
    python run.py --config configs/svicloudtvbox.yaml --dry-run
    python run.py --config configs/mysite.yaml --max-posts 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-site autoblogger pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run.py --config configs/aiprofilephotomaker.yaml\n"
            "  python run.py --config configs/svicloudtvbox.yaml --dry-run\n"
            "  python run.py --config configs/mysite.yaml --max-posts 1\n"
        ),
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        required=True,
        help="Path to site YAML config file",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--dry-run", "--test",
        action="store_true",
        dest="dry_run",
        help="Simulate all operations without publishing",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        help="Override max number of posts to generate this run",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load environment variables
    if args.env and args.env.exists():
        load_dotenv(args.env)

    if not args.config.exists():
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    # Import here to allow .env to be loaded first
    from src.pipeline import BlogPipeline

    pipeline = BlogPipeline(config_path=args.config, dry_run=args.dry_run)
    try:
        stats = pipeline.run(max_posts=args.max_posts)
        if stats.get("stopped"):
            print("Pipeline stopped (emergency stop file detected)")
            sys.exit(0)

        print(f"\n{'='*50}")
        print(f"Pipeline complete for: {pipeline.config.get('site', {}).get('name', 'unknown')}")
        print(f"  Topics found:     {stats.get('topics_found', 0)}")
        print(f"  Unique topics:    {stats.get('unique_topics', 0)}")
        print(f"  Posts attempted:  {stats.get('attempts', 0)}")
        print(f"  Posts published:  {stats.get('published', 0)}")
        print(f"  Posts staged:     {stats.get('staged', 0)}")
        print(f"  Skipped (dupes):  {stats.get('skipped_dupes', 0)}")
        print(f"  Skipped (fuzzy):  {stats.get('skipped_fuzzy', 0)}")
        print(f"  Dry run:          {stats.get('dry_run', False)}")
        print(f"{'='*50}")

    except Exception as exc:
        pipeline.log.error("Pipeline failed: %s", exc)
        raise
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()
