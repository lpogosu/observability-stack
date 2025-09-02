"""CLI entry point: `python -m loadgen --scenario error-burst`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from types import FrameType

from loadgen.driver import LoadGenerator, UrllibTransport
from loadgen.scenarios import SCENARIOS, Scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadgen",
        description="Drive the sample application into the states the alerts are written for.",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="normal",
        help="which failure mode to reproduce",
    )
    parser.add_argument(
        "--target",
        default="http://sample-app:8000",
        help="base URL of the sample application",
    )
    parser.add_argument(
        "--report-every",
        type=int,
        default=30,
        help="seconds between progress lines (0 disables them)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the scenario catalogue and exit",
    )
    return parser


def describe(scenario: Scenario) -> str:
    lines = [f"{scenario.name}: {scenario.description}"]
    offset = 0
    for phase in scenario.phases:
        lines.append(
            f"  t+{offset // 60:>3}m  {phase.name:<9} {phase.rps:>5.1f} req/s "
            f"for {phase.duration_seconds // 60}m"
        )
        offset += phase.duration_seconds
    expected = ", ".join(scenario.expected_alerts) or "none - this is the quiet baseline"
    lines.append(f"  expected alerts: {expected}")
    return "\n".join(lines)


def _progress(generator: LoadGenerator, interval: int, done: threading.Event) -> None:
    while not done.wait(interval):
        logging.getLogger("loadgen").info("%s", generator.stats.summary())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    if args.list:
        print("\n\n".join(describe(SCENARIOS[name]) for name in sorted(SCENARIOS)))
        return 0

    scenario = SCENARIOS[args.scenario]
    generator = LoadGenerator(
        target=args.target,
        scenario=scenario,
        transport=UrllibTransport(),
    )

    logger = logging.getLogger("loadgen")
    logger.info("starting scenario\n%s", describe(scenario))

    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        logger.info("stop requested, draining")
        generator.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    done = threading.Event()
    reporter: threading.Thread | None = None
    if args.report_every > 0:
        reporter = threading.Thread(
            target=_progress, args=(generator, args.report_every, done), daemon=True
        )
        reporter.start()

    started = time.monotonic()
    stats = generator.run()
    done.set()
    if reporter is not None:
        reporter.join(timeout=1.0)

    logger.info("finished in %ds: %s", int(time.monotonic() - started), stats.summary())
    if stats.sent == 0:
        logger.error("no requests were sent - is %s reachable?", args.target)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
