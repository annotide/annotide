"""Tests for app.importers.cvat: CVAT XML 1.1 "images" export parsing (EXP-6)."""

from __future__ import annotations

import pytest

from app.importers import IMPORTERS, ImportFile, ImportFormatError, get_importer
from app.importers.cvat import CvatImporter

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cvat_xml(images: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<annotations>"
        "<version>1.1</version>"
        "<meta><task><name>demo</name></task></meta>"
        f"{''.join(images)}"
        "</annotations>"
    )


def _image(name: str, width: int, height: int, body: str, image_id: int = 0) -> str:
    return f'<image id="{image_id}" name="{name}" width="{width}" height="{height}">{body}</image>'


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_cvat_importer_registered() -> None:
    assert isinstance(get_importer("cvat"), CvatImporter)
    assert IMPORTERS["cvat"] is get_importer("cvat")


# ---------------------------------------------------------------------------
# happy path — every shape type
# ---------------------------------------------------------------------------


def test_cvat_importer_happy_path_all_shape_types() -> None:
    body = (
        '<box label="car" xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0" occluded="1">'
        '<attribute name="color">red</attribute>'
        "</box>"
        '<polygon label="road" points="0.0,0.0;100.0,0.0;50.0,100.0" occluded="0">'
        '<attribute name="wet">true</attribute>'
        "</polygon>"
        '<polyline label="lane" points="0.0,0.0;10.0,10.0" />'
        '<points label="keypoints" points="1.0,2.0;3.0,4.0" />'
        '<tag label="scene"><attribute name="weather">sunny</attribute></tag>'
    )
    xml = _cvat_xml([_image("frame1.jpg", 640, 480, body)])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert len(items) == 1
    item = items[0]
    assert item.path == "frame1.jpg"
    assert item.width == 640
    assert item.height == 480
    assert item.coordinates == "pixel"
    assert item.warnings == []
    assert item.classification == {"scene": True, "scene.weather": "sunny"}

    by_type = {shape["type"]: shape for shape in item.shapes if shape["type"] != "point"}
    box = by_type["bbox"]
    assert box["class"] == "car"
    assert box["bbox"] == [10.0, 20.0, 100.0, 200.0]
    assert box["attributes"] == {"color": "red", "occluded": True}

    polygon = by_type["polygon"]
    assert polygon["class"] == "road"
    assert polygon["points"] == [[0.0, 0.0], [100.0, 0.0], [50.0, 100.0]]
    assert polygon["attributes"] == {"wet": True}

    polyline = by_type["polyline"]
    assert polyline["class"] == "lane"
    assert polyline["points"] == [[0.0, 0.0], [10.0, 10.0]]

    points = [shape for shape in item.shapes if shape["type"] == "point"]
    assert points == [
        {"type": "point", "class": "keypoints", "point": [1.0, 2.0]},
        {"type": "point", "class": "keypoints", "point": [3.0, 4.0]},
    ]


def test_cvat_importer_missing_size_is_none() -> None:
    xml = _cvat_xml(
        [
            '<image id="0" name="frame1.jpg">'
            '<box label="car" xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
            "</image>"
        ]
    )
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].width is None
    assert items[0].height is None


def test_cvat_importer_degenerate_box_is_skipped_with_warning() -> None:
    body = '<box label="car" xtl="50.0" ytl="50.0" xbr="50.0" ybr="90.0"/>'
    xml = _cvat_xml([_image("frame1.jpg", 100, 100, body)])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].shapes == []
    assert any("degenerate" in warning for warning in items[0].warnings)


def test_cvat_importer_degenerate_polygon_is_skipped_with_warning() -> None:
    body = '<polygon label="road" points="0.0,0.0;10.0,10.0"/>'
    xml = _cvat_xml([_image("frame1.jpg", 100, 100, body)])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].shapes == []
    assert any("fewer than 3 points" in warning for warning in items[0].warnings)


def test_cvat_importer_unsupported_shapes_warn_and_are_skipped() -> None:
    body = (
        '<mask label="m" rle="0,1" left="0" top="0" width="1" height="1"/>'
        '<ellipse label="e" cx="1" cy="1" rx="1" ry="1"/>'
        '<cuboid label="c" xtl1="0" ytl1="0" xbr1="1" ybr1="1"'
        ' xtl2="0" ytl2="0" xbr2="1" ybr2="1"/>'
        '<skeleton label="s"></skeleton>'
    )
    xml = _cvat_xml([_image("frame1.jpg", 100, 100, body)])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 4


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_cvat_importer_malformed_xml_raises() -> None:
    files = [ImportFile(path="bad.xml", data=b"<annotations><image></annotations>")]

    with pytest.raises(ImportFormatError):
        list(CvatImporter().parse(files))


def test_cvat_importer_wrong_root_element_raises_if_nothing_usable() -> None:
    files = [ImportFile(path="other.xml", data=b"<not_annotations/>")]

    with pytest.raises(ImportFormatError):
        list(CvatImporter().parse(files))


def test_cvat_importer_ignores_non_xml_files() -> None:
    body = '<box label="car" xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
    xml = _cvat_xml([_image("frame1.jpg", 100, 100, body)])
    files = [
        ImportFile(path="frame1.jpg", data=b"\x89PNG..."),
        ImportFile(path="README.txt", data=b"hello"),
        ImportFile(path="annotations.xml", data=xml.encode()),
    ]

    items = list(CvatImporter().parse(files))

    assert len(items) == 1


def test_cvat_importer_no_usable_files_raises() -> None:
    files = [ImportFile(path="readme.txt", data=b"hello")]

    with pytest.raises(ImportFormatError):
        list(CvatImporter().parse(files))


def test_cvat_importer_track_based_video_export_is_parsed() -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<annotations>"
        "<version>1.1</version>"
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0" '
        'outside="0" occluded="0"/>'
        "</track>"
        "</annotations>"
    )
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert len(items) == 1
    assert items[0].media_type == "video"
    assert items[0].shapes[0]["frame"] == 0
    assert items[0].shapes[0]["keyframe"] is True


# ---------------------------------------------------------------------------
# multiple images in one file
# ---------------------------------------------------------------------------


def test_cvat_importer_multiple_images() -> None:
    body1 = '<box label="car" xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
    body2 = '<box label="bus" xtl="2.0" ytl="2.0" xbr="20.0" ybr="20.0"/>'
    xml = _cvat_xml(
        [
            _image("frame1.jpg", 100, 100, body1, image_id=0),
            _image("frame2.jpg", 200, 200, body2, image_id=1),
        ]
    )
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert [item.path for item in items] == ["frame1.jpg", "frame2.jpg"]
    assert items[0].shapes[0]["class"] == "car"
    assert items[1].shapes[0]["class"] == "bus"
