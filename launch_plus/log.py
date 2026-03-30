"""Project-wide logging for launch-plus.

All modules use ``logging.getLogger("launch_plus")`` or child loggers.
The CLI attaches a :class:`DiagnosticCollector` handler to capture
warnings and errors for structured output (exit codes, summary stats).
"""

from __future__ import annotations

import logging

# Project-wide logger — all modules use this or child loggers.
logger = logging.getLogger("launch_plus")


class DiagnosticCollector(logging.Handler):
    """Captures warning/error log records for structured CLI output.

    Attach to the ``launch_plus`` logger at the CLI level::

        collector = DiagnosticCollector()
        logging.getLogger("launch_plus").addHandler(collector)
        try:
            ...  # run resolution
        finally:
            logging.getLogger("launch_plus").removeHandler(collector)
            print(f"{len(collector.errors)} error(s), {len(collector.warnings)} warning(s)")
    """

    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            self.errors.append(msg)
        elif record.levelno >= logging.WARNING:
            self.warnings.append(msg)

    def reset(self) -> None:
        self.warnings.clear()
        self.errors.clear()
