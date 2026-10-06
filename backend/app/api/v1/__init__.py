"""Version 1 of the public REST API.

Every router is registered here and nowhere else, so the surface of ``/api/v1``
can be read off one file.

Routers are imported as ``router as <name>_router`` rather than as modules:
``from app.api.v1 import annotations`` would be shadowed by the module-level
``annotations`` name that ``from __future__ import annotations`` binds.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.annotations import router as annotations_router
from app.api.v1.api_keys import router as api_keys_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.comments import router as comments_router
from app.api.v1.connectors import router as connectors_router
from app.api.v1.health import router as health_router
from app.api.v1.items import router as items_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.license import router as license_router
from app.api.v1.licensing import router as licensing_router
from app.api.v1.members import router as members_router
from app.api.v1.mfa import router as mfa_router
from app.api.v1.ml_platforms import router as ml_platforms_router
from app.api.v1.models import router as models_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.projects import router as projects_router
from app.api.v1.quality import router as quality_router
from app.api.v1.scim import router as scim_router
from app.api.v1.snapshots import router as snapshots_router
from app.api.v1.stats import router as stats_router
from app.api.v1.storage import router as storage_router
from app.api.v1.storage_events import router as storage_events_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.users import router as users_router
from app.api.v1.webhooks import router as webhooks_router

api_router = APIRouter()

api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(mfa_router)
api_router.include_router(api_keys_router)
api_router.include_router(users_router)
api_router.include_router(scim_router)
api_router.include_router(projects_router)
api_router.include_router(items_router)
api_router.include_router(members_router)
api_router.include_router(tasks_router)
api_router.include_router(annotations_router)
api_router.include_router(comments_router)
api_router.include_router(notifications_router)
api_router.include_router(connectors_router)
api_router.include_router(storage_events_router)
api_router.include_router(ml_platforms_router)
api_router.include_router(models_router)
api_router.include_router(jobs_router)
api_router.include_router(snapshots_router)
api_router.include_router(stats_router)
api_router.include_router(quality_router)
api_router.include_router(license_router)
api_router.include_router(licensing_router)
api_router.include_router(audit_router)
api_router.include_router(storage_router)
api_router.include_router(uploads_router)
api_router.include_router(webhooks_router)

__all__ = ["api_router"]
