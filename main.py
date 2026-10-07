"""Command-line entry point for the Supplement Store Scraper.

Run from the project root with the virtualenv active:

    python main.py serve                            # FastAPI app, docs at /docs
    python main.py scrape                           # scrape every category
    python main.py scrape --category Protein --details
    python main.py db-check                         # verify SQL Server connectivity

`app.config` reads the environment at import time and `app.db` builds its
settings at module level, so every module is imported inside the command that
needs it: a missing .env value then surfaces as one readable line instead of a
traceback raised before main() even runs.
"""

import argparse
import logging
import sys


def _configure_logging(verbose: bool) -> None:
    """The app modules all use logging.getLogger(__name__) but configure nothing;
    the entry point owns that decision."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # A full scrape makes hundreds of requests; urllib3's per-connection
    # chatter would bury our own progress lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    import uvicorn

    from app.config import AppSettings

    settings = AppSettings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port
    reload = args.reload or settings.reload

    print(f"Swagger UI: http://{host}:{port}/docs")
    # Pass the import string, not the app object: --reload needs to re-import it.
    uvicorn.run("app.api:app", host=host, port=port, reload=reload)
    return 0


def cmd_scrape(args) -> int:
    """Run the same job the API runs in the background, but in the foreground
    so the shell (or a scheduled task) can see the result and the exit code."""
    from app import jobs

    job = jobs.start_job(args.category, args.details)
    print(f"Job {job.job_id}: category={args.category or 'all'}, details={args.details}")

    jobs.run_job(job)  # never raises; problems are recorded on the job itself

    print(f"  status     : {job.status}")
    print(f"  categories : {job.categories_done}/{job.categories_total}")
    print(f"  products   : {job.products_scraped}")
    print(f"  inserted   : {job.inserted}")
    print(f"  updated    : {job.updated}")
    print(f"  skipped    : {job.skipped}")
    for error in job.errors:
        print(f"  error      : {error}", file=sys.stderr)

    return 0 if job.status == "completed" else 1


def cmd_db_check(args) -> int:
    import pyodbc

    from app.db import get_connection, settings

    auth = "Windows auth" if settings.trusted_connection else f"user {settings.user}"
    print(f"Connecting to {settings.server} / {settings.database} ({auth}) ...")
    try:
        with get_connection() as conn:
            database, login = conn.cursor().execute(
                "SELECT DB_NAME(), SUSER_SNAME()"
            ).fetchone()
    except pyodbc.Error as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK - connected to '{database}' as '{login}'")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scrape supplementstores.lk into SQL Server, or serve the API over it.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-level logging")
    sub = parser.add_subparsers(dest="command", metavar="{serve,scrape,db-check}")

    p_serve = sub.add_parser("serve", help="run the FastAPI app (default command)")
    p_serve.add_argument("--host", help="override APP_HOST")
    p_serve.add_argument("--port", type=int, help="override APP_PORT")
    p_serve.add_argument("--reload", action="store_true", help="restart on code changes")
    p_serve.set_defaults(func=cmd_serve)

    p_scrape = sub.add_parser("scrape", help="scrape now and wait for it to finish")
    p_scrape.add_argument("--category", help="category name or slug; omit to scrape all")
    p_scrape.add_argument(
        "--details",
        action="store_true",
        help="also open each product page for SKU, stock text and gallery (much slower)",
    )
    p_scrape.set_defaults(func=cmd_scrape)

    p_check = sub.add_parser("db-check", help="verify the database connection and exit")
    p_check.set_defaults(func=cmd_db_check)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    # Bare `python main.py` is the common case: serve.
    args = parser.parse_args(argv if argv is not None else (sys.argv[1:] or ["serve"]))
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except RuntimeError as exc:  # raised by config._require for a missing .env value
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
