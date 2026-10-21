"""Consistent stderr logging for CLI scripts and GitHub Actions."""

import logging
import os


def add_debug_argument(parser):
    parser.add_argument("--debug", action="store_true", help="Enable detailed diagnostic logging")


def configure_logging(debug=False):
    sources = []
    if debug:
        sources.append("--debug")
    if os.environ.get("RUNNER_DEBUG") == "1":
        sources.append("RUNNER_DEBUG=1")
    if os.environ.get("ACTIONS_STEP_DEBUG", "").lower() == "true":
        sources.append("ACTIONS_STEP_DEBUG=true")
    logging.basicConfig(
        level=logging.DEBUG if sources else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    if sources:
        logging.getLogger(__name__).info(
            "DEBUG MODE ENABLED (%s). HTTP logs include method, URL, status and timing; "
            "request/response headers and bodies are omitted.", ", ".join(sources),
        )
