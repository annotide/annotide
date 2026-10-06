"""Python SDK for the Annotide API (API-3).

from annotide import Client

with Client("https://annotate.example.com", api_key="...") as client:
    snapshot = client.take_snapshot(project_id, "2026-q3")
    client.export(project_id, "coco", "dataset.zip", snapshot_id=snapshot["id"])
"""

from annotide.client import Client
from annotide.errors import AnnotationError, ApiError, JobFailedError, JobTimeoutError

__version__ = "0.1.0"

__all__ = [
    "AnnotationError",
    "ApiError",
    "Client",
    "JobFailedError",
    "JobTimeoutError",
    "__version__",
]
