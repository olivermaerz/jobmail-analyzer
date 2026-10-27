#!/usr/bin/env python3
"""CLI entrypoint for the job-application mail analyzer.

Defaults: Apple mbox at ./mbox -> ./data, always enrich (browser on
block), always write events + applications CSV/JSON.

Normal runs: progress bars on the TTY, detailed logs in data/analyze.log
(tail -f). --debug keeps chatty console logs.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

from jobmail.browser_enrich import HostRegistry, host_of
from jobmail.classify import Classifier
from jobmail.cluster import cluster_events
from jobmail.enrich import enrich_applications
from jobmail.gemini_rate import DEFAULT_MODEL, GeminiModelPool
from jobmail.ingest import get_source, list_sources
from jobmail.report import write_reports
from jobmail.store import load_events, processed_ids, save_events

logger = logging.getLogger("analyze")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = Path(args.log_file) if args.log_file else out_dir / "analyze.log"
    _setup_logging(debug=args.debug, log_path=log_path)

    if not args.debug:
        print(f"Logging to {log_path}  (tail -f {log_path})", flush=True)

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.error("GEMINI_API_KEY missing — put it in .env")
        print("GEMINI_API_KEY missing — put it in .env", file=sys.stderr)
        return 1

    # --model forces a single model; otherwise GEMINI_MODELS round-robin
    model_override = args.model if args.model_set else None
    pool = GeminiModelPool.from_env(
        state_path=out_dir / "gemini_quota.json",
        model_override=model_override,
    )
    logger.info("Gemini pool: %s", pool.summary())
    if not args.debug:
        print(f"Gemini: {pool.summary()}", flush=True)

    if args.browser_reauth is not None:
        registry = HostRegistry(out_dir / "browser_hosts.json")
        if args.browser_reauth == "":
            registry.clear()
            logger.info("cleared all browser host login state")
        else:
            host = host_of(args.browser_reauth) or args.browser_reauth.lower()
            registry.clear(host)
            logger.info("cleared browser login state for %s", host)

    source = get_source(args.source)
    input_path = Path(args.input)

    existing = [] if args.reclassify_all else load_events(out_dir)
    seen = processed_ids(existing)
    logger.info(
        "loaded %s existing events (%s unique ids)",
        len(existing),
        len(seen),
    )

    catalog_apps = cluster_events(existing) if existing else []

    limit = args.limit if args.debug or args.limit is not None else None
    if args.debug and args.limit is None:
        limit = 5

    new_emails = []
    skipped = 0
    for email in source.iter_emails(input_path):
        if not args.reclassify_all and email.message_id in seen:
            skipped += 1
            continue
        new_emails.append(email)
        if limit is not None and len(new_emails) >= limit:
            break

    logger.info(
        "ingest=%s path=%s new=%s skipped_existing=%s limit=%s",
        source.name,
        input_path,
        len(new_emails),
        skipped,
        limit,
    )
    if not args.debug:
        print(
            f"Classify {len(new_emails)} new emails "
            f"(skipped {skipped} already done)",
            flush=True,
        )

    classifier = Classifier(api_key=api_key, pool=pool)
    new_events = []
    catalog_events = list(existing)

    # checkpoint so Ctrl+C doesn't throw away a long classify run
    events_so_far = list(existing)

    progress = tqdm(
        new_emails,
        desc="Classifying",
        unit="mail",
        disable=not sys.stderr.isatty(),
    )
    for email in progress:
        progress.set_postfix_str((email.subject or "")[:40], refresh=False)
        logger.info("classifying: %s", email.subject[:90])
        try:
            event = classifier.classify(
                email,
                catalog_apps=catalog_apps,
                catalog_events=catalog_events,
            )
        except Exception:
            logger.exception("failed on %s — aborting run", email.message_id)
            # still flush what we have
            save_events(out_dir, events_so_far)
            progress.close()
            return 1
        logger.debug(
            " -> type=%s company=%s role=%s key=%s conf=%.2f",
            event.event_type,
            event.company,
            event.role_title,
            event.application_key,
            event.confidence,
        )
        new_events.append(event)
        catalog_events.append(event)

        if args.reclassify_all:
            replace_ids = {e.message_id for e in new_events}
            events_so_far = [
                e for e in existing if e.message_id not in replace_ids
            ] + new_events
        else:
            events_so_far = existing + new_events
        save_events(out_dir, events_so_far)

    progress.close()

    events = events_so_far
    applications = cluster_events(events)

    force_hosts: set[str] = set()
    if args.browser_reauth:
        force_hosts.add(host_of(args.browser_reauth) or args.browser_reauth.lower())

    enrich_applications(
        applications,
        out_dir,
        api_key=api_key,
        skip=args.skip_enrich,
        skip_browser=args.skip_browser_enrich,
        force_reauth_hosts=force_hosts or None,
        pool=pool,
        show_progress=sys.stderr.isatty(),
    )

    paths = write_reports(out_dir, events, applications)
    logger.info("done. artifacts:")
    for name, path in paths.items():
        logger.info("  %s: %s", name, path)
    if not args.debug:
        print("Done.", flush=True)
        for name, path in paths.items():
            print(f"  {name}: {path}", flush=True)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze job-application mail exports into CSV/JSON reports.",
    )
    parser.add_argument(
        "--source",
        default="apple",
        choices=list_sources(),
        help="ingest plugin (default: apple)",
    )
    parser.add_argument(
        "--input",
        default="mbox",
        help="path to the mail export (default: ./mbox)",
    )
    parser.add_argument(
        "--out",
        default="data",
        help="output directory (default: ./data)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="log file path (default: <out>/analyze.log)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose console logs; classify only first N emails (default N=5)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max new emails to classify this run",
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="don't fetch job ads",
    )
    parser.add_argument(
        "--skip-browser-enrich",
        action="store_true",
        help="if httpx is blocked, don't open Chrome — mark blocked",
    )
    parser.add_argument(
        "--browser-reauth",
        nargs="?",
        const="",
        default=None,
        help="clear login state for HOST (or all if omitted) and force re-login",
    )
    parser.add_argument(
        "--reclassify-all",
        action="store_true",
        help="ignore Message-ID store and reclassify ingested mail",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "force a single Gemini model (default: use GEMINI_MODELS "
            f"from .env, else {DEFAULT_MODEL})"
        ),
    )
    args = parser.parse_args(argv)
    # detect explicit --model so we don't override the multi-model pool
    # when the user left the default alone
    args.model_set = any(
        a == "--model" or a.startswith("--model=")
        for a in (argv if argv is not None else sys.argv[1:])
    )
    return args


def _setup_logging(*, debug: bool, log_path: Path) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    if debug:
        console.setLevel(logging.DEBUG)
    else:
        # progress bar owns the TTY; only surface problems on console
        console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    root.addHandler(console)

    if not os.getenv("JOBMAIL_HTTP_DEBUG"):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("google_genai").setLevel(logging.INFO)


if __name__ == "__main__":
    sys.exit(main())
