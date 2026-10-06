"""Tests for app.importers.voc: Pascal VOC XML parsing (EXP-6)."""

from __future__ import annotations

import pytest

from app.importers import IMPORTERS, ImportFile, ImportFormatError, get_importer
from app.importers.voc import VocImporter

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _voc_object(
    name: str,
    bbox: tuple[float, float, float, float] | None = None,
    *,
    polygon: list[tuple[float, float]] | None = None,
    difficult: str | None = None,
    truncated: str | None = None,
    occluded: str | None = None,
    pose: str | None = None,
) -> str:
    parts = [f"<name>{name}</name>"]
    if pose is not None:
        parts.append(f"<pose>{pose}</pose>")
    if truncated is not None:
        parts.append(f"<truncated>{truncated}</truncated>")
    if occluded is not None:
        parts.append(f"<occluded>{occluded}</occluded>")
    if difficult is not None:
        parts.append(f"<difficult>{difficult}</difficult>")
    if bbox is not None:
        x_min, y_min, x_max, y_max = bbox
        parts.append(
            f"<bndbox><xmin>{x_min}</xmin><ymin>{y_min}</ymin>"
            f"<xmax>{x_max}</xmax><ymax>{y_max}</ymax></bndbox>"
        )
    if polygon is not None:
        points = "".join(f"<point><x>{x}</x><y>{y}</y></point>" for x, y in polygon)
        parts.append(f"<polygon>{points}</polygon>")
    return f"<object>{''.join(parts)}</object>"


def _voc_xml(
    filename: str | None,
    width: int | None,
    height: int | None,
    objects: list[str],
) -> str:
    size = f"<size><width>{width}</width><height>{height}</height></size>" if width else ""
    fname = f"<filename>{filename}</filename>" if filename else ""
    return f"<annotation>{fname}{size}{''.join(objects)}</annotation>"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_voc_importer_registered() -> None:
    assert isinstance(get_importer("voc"), VocImporter)
    assert IMPORTERS["voc"] is get_importer("voc")


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_voc_importer_happy_path_bbox_and_attributes() -> None:
    xml = _voc_xml(
        "car1.jpg",
        640,
        480,
        [
            _voc_object(
                "car",
                bbox=(10.0, 20.0, 100.0, 200.0),
                difficult="1",
                truncated="0",
                occluded="1",
                pose="Frontal",
            )
        ],
    )
    files = [ImportFile(path="car1.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert len(items) == 1
    item = items[0]
    assert item.path == "car1.jpg"
    assert item.width == 640
    assert item.height == 480
    assert item.coordinates == "pixel"
    assert item.warnings == []
    assert item.shapes == [
        {
            "type": "bbox",
            "class": "car",
            "bbox": [10.0, 20.0, 100.0, 200.0],
            "attributes": {
                "difficult": True,
                "truncated": False,
                "occluded": True,
                "pose": "Frontal",
            },
        }
    ]


def test_voc_importer_polygon_extension() -> None:
    xml = _voc_xml(
        "road.jpg",
        100,
        100,
        [_voc_object("road", polygon=[(0.0, 0.0), (100.0, 0.0), (50.0, 100.0)])],
    )
    files = [ImportFile(path="road.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert items[0].shapes == [
        {
            "type": "polygon",
            "class": "road",
            "points": [[0.0, 0.0], [100.0, 0.0], [50.0, 100.0]],
        }
    ]


def test_voc_importer_missing_size_is_none() -> None:
    xml = _voc_xml(
        "car1.jpg",
        None,
        None,
        [_voc_object("car", bbox=(1.0, 1.0, 10.0, 10.0))],
    )
    files = [ImportFile(path="car1.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert items[0].width is None
    assert items[0].height is None


def test_voc_importer_missing_filename_falls_back_to_stem() -> None:
    xml = _voc_xml(None, 100, 100, [_voc_object("car", bbox=(1.0, 1.0, 10.0, 10.0))])
    files = [ImportFile(path="fallback_name.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert items[0].path == "fallback_name"
    assert any("filename" in warning for warning in items[0].warnings)


def test_voc_importer_degenerate_bbox_is_skipped_with_warning() -> None:
    xml = _voc_xml(
        "car1.jpg",
        100,
        100,
        [_voc_object("car", bbox=(50.0, 50.0, 50.0, 90.0))],
    )
    files = [ImportFile(path="car1.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert items[0].shapes == []
    assert any("degenerate" in warning for warning in items[0].warnings)


def test_voc_importer_object_without_bndbox_or_polygon_warns() -> None:
    xml = "<annotation><filename>x.jpg</filename><object><name>car</name></object></annotation>"
    files = [ImportFile(path="x.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert items[0].shapes == []
    assert any("no <bndbox> or <polygon>" in warning for warning in items[0].warnings)


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_voc_importer_malformed_xml_raises() -> None:
    files = [ImportFile(path="bad.xml", data=b"<annotation><object></annotation>")]

    with pytest.raises(ImportFormatError):
        list(VocImporter().parse(files))


def test_voc_importer_wrong_root_element_raises_if_nothing_usable() -> None:
    files = [ImportFile(path="other.xml", data=b"<not_annotation/>")]

    with pytest.raises(ImportFormatError):
        list(VocImporter().parse(files))


def test_voc_importer_ignores_non_xml_files() -> None:
    xml = _voc_xml("car1.jpg", 100, 100, [_voc_object("car", bbox=(1.0, 1.0, 10.0, 10.0))])
    files = [
        ImportFile(path="car1.jpg", data=b"\x89PNG..."),
        ImportFile(path="readme.txt", data=b"hello"),
        ImportFile(path="car1.xml", data=xml.encode()),
    ]

    items = list(VocImporter().parse(files))

    assert len(items) == 1


def test_voc_importer_no_usable_files_raises() -> None:
    files = [ImportFile(path="readme.txt", data=b"hello")]

    with pytest.raises(ImportFormatError):
        list(VocImporter().parse(files))


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def _render_voc(
    filename: str,
    width: int,
    height: int,
    boxes: list[tuple[str, float, float, float, float]],
) -> str:
    objects = [
        _voc_object(name, bbox=(x_min, y_min, x_max, y_max))
        for name, x_min, y_min, x_max, y_max in boxes
    ]
    return _voc_xml(filename, width, height, objects)


def test_voc_importer_round_trip() -> None:
    boxes = [("car", 1.0, 2.0, 3.0, 4.0), ("person", 10.0, 20.0, 30.0, 40.0)]
    xml = _render_voc("scene.jpg", 800, 600, boxes)
    files = [ImportFile(path="scene.xml", data=xml.encode())]

    items = list(VocImporter().parse(files))

    assert len(items) == 1
    item = items[0]
    assert item.path == "scene.jpg"
    assert item.width == 800
    assert item.height == 600
    assert [(s["class"], *s["bbox"]) for s in item.shapes] == boxes
