"""JSON logs (CLAUDE.md rule 2): every line names trace_id, detector and version where they
apply; never content, never a detector's exception message (only its class name)."""

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

from traceblame_detectors.engine import DetectorRun, RunStatus

LOGGER = "traceblame_detectors"
FIELDS = "fields"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        line.update(getattr(record, FIELDS, {}))
        return json.dumps(line, sort_keys=True)


def setup(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    return logger


def log_runs(logger: logging.Logger, runs: Iterable[DetectorRun]) -> None:
    for r in runs:
        fields = {
            "trace_id": r.trace_id,
            "detector": r.detector,
            "version": r.detector_version,
            "revision_ms": r.revision_ms,
            "status": r.status.value,
            "findings": len(r.findings),
        }
        if r.status is RunStatus.ERROR:
            logger.warning(
                "detector run failed", extra={FIELDS: {**fields, "error_kind": r.error_kind}}
            )
        else:
            logger.debug("detector run", extra={FIELDS: fields})
