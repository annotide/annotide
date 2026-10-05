"""Business features and whether the licence in force has them (LIC-32, LIC-33).

Everything an annotator or ML engineer works with is in every edition; these
are what an organisation's IT, security and compliance need. A key unlocks
the ids it lists in ``features``. See ``docs/LICENSING.md`` ("Editions",
"Business features and a lapsed licence").
"""

from __future__ import annotations

import enum
from typing import Final

from app.services.licensing.license import LicenseStatus
from app.services.licensing.state import EffectiveLicense, is_restricted


class Feature(enum.StrEnum):
    """A Business feature id, as a key's ``features`` lists it."""

    SSO = "sso"
    SCIM = "scim"
    PATH_PERMISSIONS = "path_permissions"
    AUDIT_HISTORY = "audit_history"
    SEAT_REPORT = "seat_report"
    SHAREPOINT = "sharepoint"
    ML_PLATFORMS = "ml_platforms"
    QUALITY = "quality"


#: Every Business feature id, in the order the licence page lists them.
BUSINESS_FEATURES: Final[tuple[str, ...]] = tuple(feature.value for feature in Feature)

#: What each one is called where it is refused.
FEATURE_NAMES: Final[dict[Feature, str]] = {
    Feature.SSO: "Single sign-on",
    Feature.SCIM: "SCIM provisioning",
    Feature.PATH_PERMISSIONS: "Folder-level permissions",
    Feature.AUDIT_HISTORY: "Audit history beyond 30 days",
    Feature.SEAT_REPORT: "The seat report",
    Feature.SHAREPOINT: "The SharePoint connector",
    Feature.ML_PLATFORMS: "ML platform integration",
    Feature.QUALITY: "Quality control",
}


def licensed_features(licence: EffectiveLicense) -> frozenset[str]:
    """The Business features the licence in force unlocks.

    A key's features count while it is valid, or expired and still within its
    grace period. A build without vendor keys can hold no valid key, so it
    enforces nothing, as for seats.
    """
    if not licence.enforcing:
        return frozenset(BUSINESS_FEATURES)
    if licence.license is None:
        return frozenset()
    if licence.status is LicenseStatus.EXPIRED and is_restricted(licence):
        return frozenset()
    return frozenset(licence.license.features) & frozenset(BUSINESS_FEATURES)


def has_feature(licence: EffectiveLicense, feature: Feature) -> bool:
    """Whether ``feature`` is licensed."""
    return feature.value in licensed_features(licence)


def refusal_message(feature: Feature) -> str:
    """What a caller refused for ``feature`` is told (403 `license-feature`)."""
    return (
        f"{FEATURE_NAMES[feature]} is part of the Business edition. An administrator can "
        "start a free 30-day trial or add a key under Settings → Licence."
    )
