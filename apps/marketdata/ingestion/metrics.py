"""Metric calculation hook.

Formulas belong to later milestones. The ingestion pipeline only promises that
a configured calculator runs once, after a job's rows are committed.
"""

from typing import Protocol

from apps.marketdata.models import IngestionJob


class MetricCalculator(Protocol):
    def on_job_completed(self, *, job: IngestionJob) -> None:
        """Persist or refresh metrics derived from the job's accepted rows."""
