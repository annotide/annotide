"""Tests for CVAT `<track>` (video) import (EXP-6)."""

from __future__ import annotations

from app.importers import ImportFile
from app.importers.cvat import CvatImporter


def _video_xml(tracks: list[str], *, size: str = "150", fps: str | None = "25") -> str:
    fps_tag = f"<fps>{fps}</fps>" if fps is not None else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<annotations>"
        "<version>1.1</version>"
        "<meta><task>"
        "<name>demo</name>"
        f"<size>{size}</size>"
        f"{fps_tag}"
        "<original_size><width>1920</width><height>1080</height></original_size>"
        "<source>video.mp4</source>"
        "</task></meta>"
        f"{''.join(tracks)}"
        "</annotations>"
    )


def test_cvat_video_import_yields_one_item_with_meta() -> None:
    body = (
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0"/>'
        "</track>"
    )
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert len(items) == 1
    item = items[0]
    assert item.path == "video.mp4"
    assert item.media_type == "video"
    assert item.width == 1920
    assert item.height == 1080
    assert item.meta == {"frame_count": 150, "fps": 25.0}


def test_cvat_video_import_keeps_keyframes_and_outside_drops_interpolated() -> None:
    body = (
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0"/>'
        '<box frame="1" keyframe="0" outside="0" occluded="0" '
        'xtl="11.0" ytl="21.0" xbr="101.0" ybr="201.0"/>'
        '<box frame="2" keyframe="0" outside="1" occluded="0" '
        'xtl="12.0" ytl="22.0" xbr="102.0" ybr="202.0"/>'
        "</track>"
    )
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    shapes = items[0].shapes
    frames = sorted(shape["frame"] for shape in shapes)
    assert frames == [0, 2]
    outside_shape = next(shape for shape in shapes if shape["frame"] == 2)
    assert outside_shape["outside"] is True
    assert outside_shape["keyframe"] is False


def test_cvat_video_import_one_track_id_per_track_shared_across_frames() -> None:
    body = (
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0"/>'
        '<box frame="5" keyframe="1" outside="0" occluded="0" '
        'xtl="15.0" ytl="25.0" xbr="105.0" ybr="205.0"/>'
        "</track>"
    )
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    track_ids = {shape["track_id"] for shape in items[0].shapes}
    assert len(track_ids) == 1


def test_cvat_video_import_distinct_tracks_get_distinct_ids() -> None:
    body = (
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0"/>'
        "</track>"
        '<track id="1" label="bus">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="10.0" ytl="20.0" xbr="100.0" ybr="200.0"/>'
        "</track>"
    )
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    track_ids = [shape["track_id"] for shape in items[0].shapes]
    assert len(set(track_ids)) == 2


def test_cvat_video_import_polygon_and_points_tracks() -> None:
    body = (
        '<track id="0" label="road">'
        '<polygon frame="0" keyframe="1" outside="0" occluded="0" '
        'points="0.0,0.0;100.0,0.0;50.0,100.0"/>'
        "</track>"
        '<track id="1" label="keypoints">'
        '<points frame="0" keyframe="1" outside="0" occluded="0" points="1.0,2.0;3.0,4.0"/>'
        "</track>"
    )
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    by_type = {shape["type"] for shape in items[0].shapes}
    assert by_type == {"polygon", "point"}


def test_cvat_video_import_unsupported_shape_warns() -> None:
    body = '<track id="0" label="m"><mask frame="0" keyframe="1" rle="0,1"/></track>'
    xml = _video_xml([body])
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].shapes == []
    assert any("not supported" in warning for warning in items[0].warnings)


def test_cvat_video_import_missing_fps_omits_it_from_meta() -> None:
    body = (
        '<track id="0" label="car">'
        '<box frame="0" keyframe="1" outside="0" occluded="0" '
        'xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
        "</track>"
    )
    xml = _video_xml([body], fps=None)
    files = [ImportFile(path="annotations.xml", data=xml.encode())]

    items = list(CvatImporter().parse(files))

    assert items[0].meta == {"frame_count": 150}


def test_cvat_mixed_image_and_track_files_both_parsed() -> None:
    image_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<annotations><version>1.1</version>"
        '<image id="0" name="frame1.jpg" width="100" height="100">'
        '<box label="car" xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
        "</image></annotations>"
    )
    video_xml = _video_xml(
        [
            '<track id="0" label="car">'
            '<box frame="0" keyframe="1" outside="0" occluded="0" '
            'xtl="1.0" ytl="1.0" xbr="10.0" ybr="10.0"/>'
            "</track>"
        ]
    )
    files = [
        ImportFile(path="images.xml", data=image_xml.encode()),
        ImportFile(path="video.xml", data=video_xml.encode()),
    ]

    items = list(CvatImporter().parse(files))

    media_types = {item.media_type for item in items}
    assert media_types == {None, "video"}
    assert len(items) == 2
