"""Import format parsers (COCO / YOLO / Pascal VOC / CVAT / Label Studio), EXP-6.

Imports only from `app.schemas`, per the dependency rule in docs/CONTRACTS.md.
Importing this package registers every built-in importer in `IMPORTERS`.
"""

from app.importers.base import (
    IMPORTERS,
    ImportedItem,
    Importer,
    ImportFile,
    ImportFormatError,
    expand_archive,
    get_importer,
    is_archive,
    register_importer,
)
from app.importers.coco import CocoImporter
from app.importers.cvat import CvatImporter
from app.importers.label_studio import LabelStudioImporter
from app.importers.voc import VocImporter
from app.importers.yolo import YoloImporter

__all__ = [
    "IMPORTERS",
    "CocoImporter",
    "CvatImporter",
    "ImportFile",
    "ImportFormatError",
    "ImportedItem",
    "Importer",
    "LabelStudioImporter",
    "VocImporter",
    "YoloImporter",
    "expand_archive",
    "get_importer",
    "is_archive",
    "register_importer",
]
