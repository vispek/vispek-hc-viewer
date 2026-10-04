# SPDX-License-Identifier: Apache-2.0
"""The data folder: captures and calibrations by name, pictures, spectra, exports."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import vispek_hc
from PIL import Image
from vispek_hc import samples
from vispek_hc.errors import (
    CalibrationMismatchError,
    ConfigRejectedError,
    FrameNotFoundError,
    InvalidStateError,
)

from vispek_hc_viewer import library as library_module
from vispek_hc_viewer.library import Library


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The sample scene and its white reference, recorded once on the simulated device."""
    folder = tmp_path_factory.mktemp("recorded")
    vispek_hc.example_captures(folder)
    with vispek_hc.open_session(simulate=True) as session:
        vispek_hc.record_frames(session, folder / "frame", "single")
        vispek_hc.record(session, folder / "small", vispek_hc.ScanSettings(channels=(5, 9)))
    # The white board again, with the visible LEDs only: a reference that covers fewer
    # bands than the sample, as a reference on paper does for a scan that includes UV.
    rig = vispek_hc.SimulatedRig(size=samples.SIZE, reflectance=samples.illumination())
    with vispek_hc.Session.from_rig(rig) as session:
        session.pair_probe()
        settings = vispek_hc.ScanSettings(channels=tuple(range(5, 12)), role="white")
        vispek_hc.record(session, folder / "white-visible", settings)
    return folder


@pytest.fixture
def library(recorded: Path, tmp_path: Path) -> Library:
    made = Library(tmp_path / "data")
    for name in ("sample", "white", "frame", "small", "white-visible"):
        shutil.copytree(recorded / name, made.root / "captures" / name)
    return made


def picture(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def test_a_new_library_makes_its_folders(tmp_path: Path) -> None:
    made = Library(tmp_path / "fresh")
    assert sorted(p.name for p in made.root.iterdir()) == ["calibrations", "captures", "exports"]
    assert made.captures() == []
    assert made.calibrations() == []


def test_captures_are_listed_newest_first_with_what_a_list_needs(library: Library) -> None:
    listed = library.captures()
    assert {entry["id"] for entry in listed} == {
        "sample",
        "white",
        "frame",
        "small",
        "white-visible",
    }
    times = [entry["started_at_utc"] for entry in listed]
    assert times == sorted(times, reverse=True)
    sample = next(entry for entry in listed if entry["id"] == "sample")
    assert sample["mode"] == "scan"
    assert sample["role"] is None
    assert sample["status"] == "complete"
    assert sample["size"] == [128, 72]
    assert sample["outputs"] == 1
    assert sample["simulated"] is True
    assert [band["led_id"] for band in sample["bands"]] == list(range(5, 18))
    assert sample["bands"][0] == {
        "band_id": "led_05", "led_id": 5, "nm": 455, "pwm": 24,
        "response_dn": sample["bands"][0]["response_dn"], "saturated_fraction": 0.0,
    }  # fmt: skip
    assert sample["views"] == {"rgb": True, "cir": True, "uv": False, "ndvi": True, "ndwi": True}
    white = next(entry for entry in listed if entry["id"] == "white")
    assert white["role"] == "white"
    small = next(entry for entry in listed if entry["id"] == "small")
    assert small["views"]["rgb"] is False, "LEDs 5 and 9 alone make no colour picture"
    frame = next(entry for entry in listed if entry["id"] == "frame")
    assert (frame["mode"], frame["bands"], frame["outputs"]) == ("single", [], None)


def test_a_capture_says_which_bands_clipped_too_much_for_a_calibration(library: Library) -> None:
    # Seen on the device: a white reference recorded too bright is refused as a
    # calibration, and nothing on its card said why once the message had gone.
    assert library.summary("white")["clipped"] == {"limit": 0.02, "led_ids": []}
    path = library.root / "captures" / "white" / "capture.json"
    document = json.loads(path.read_text("utf-8"))
    frames = document["groups"][0]["frames"]
    frames[0]["frame_check"]["rgb_saturated_fraction"] = 0.62
    frames[1]["frame_check"]["rgb_saturated_fraction"] = 0.02  # at the limit: still accepted
    frames[2]["frame_check"]["rgb_saturated_fraction"] = 0.021
    del frames[3]["frame_check"]["rgb_saturated_fraction"]  # unknown is not clipped
    path.write_text(json.dumps(document), "utf-8")
    leds = [frame["led_id"] for frame in frames]
    assert library.summary("white")["clipped"] == {"limit": 0.02, "led_ids": [leds[0], leds[2]]}
    assert library.summary("frame")["clipped"] == {"limit": 0.02, "led_ids": []}


def test_a_damaged_capture_is_listed_as_damaged(library: Library) -> None:
    broken = library.root / "captures" / "broken"
    broken.mkdir()
    (broken / "capture.json").write_text("{ not json", "utf-8")
    (library.root / "captures" / "notes.txt").write_text("not a capture", "utf-8")
    (library.root / "captures" / "empty").mkdir()
    entry = next(entry for entry in library.captures() if entry["id"] == "broken")
    assert entry["status"] == "damaged"
    assert "empty" not in {entry["id"] for entry in library.captures()}
    with pytest.raises(vispek_hc.VispekHCError):
        library.view_png("broken", "rgb")


@pytest.mark.parametrize("name", ["../sample", "a/b", "", ".", "..", "/etc", "x" * 65, "-x", "a b"])
def test_names_that_are_not_plain_names_are_refused(library: Library, name: str) -> None:
    with pytest.raises(ConfigRejectedError):
        library.summary(name)
    with pytest.raises(ConfigRejectedError):
        library.reserve(name, "scan")
    with pytest.raises(ConfigRejectedError):
        library.calibration(name)


def test_a_link_is_not_followed_out_of_the_data_folder(library: Library, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    shutil.copytree(library.root / "captures" / "sample", outside)
    (library.root / "captures" / "linked").symlink_to(outside, target_is_directory=True)
    assert "linked" not in {entry["id"] for entry in library.captures()}
    with pytest.raises(ConfigRejectedError):
        library.summary("linked")


def test_an_unknown_capture_is_not_found(library: Library) -> None:
    with pytest.raises(FrameNotFoundError):
        library.summary("nothing-here")


def test_a_new_capture_gets_a_free_name(library: Library) -> None:
    name, path = library.reserve(None, "scan")
    assert name.startswith("scan-")
    assert path == library.root / "captures" / name
    assert not path.exists(), "the recorder makes the folder"
    path.mkdir()
    again, _ = library.reserve(None, "scan")
    assert again != name
    assert library.reserve("my-scan_1", "scan")[0] == "my-scan_1"
    with pytest.raises(InvalidStateError, match="already"):
        library.reserve("sample", "scan")


def test_a_raw_colour_picture_has_the_size_of_the_capture_and_no_strip(library: Library) -> None:
    image = picture(library.view_png("sample", "rgb"))
    assert image.shape == (72, 128, 3)
    assert not ((image[:12, :, 0] == 160) & (image[:12, :, 1] == 0)).all()
    band = picture(library.view_png("sample", "band:led_13", palette="gray"))
    assert (band[..., 0] == band[..., 1]).all()
    x, y, w, h = samples.PATCHES["leaf"]
    red = picture(library.view_png("sample", "band:led_10", palette="gray"))
    leaf = (slice(y + 2, y + h - 2), slice(x + 2, x + w - 2))
    assert band[leaf].mean() > red[leaf].mean() + 40, "the leaf is bright at 850 nm, dark at 667"


def test_views_that_compare_bands_need_a_calibration(library: Library) -> None:
    with pytest.raises(InvalidStateError):
        library.view_png("sample", "ndvi")
    calibration = library.make_calibration(["white"], "board")
    assert calibration == "board"
    image = picture(library.view_png("sample", "ndvi", calibration="board"))
    assert image.shape == (72, 128, 3)
    assert library.layer("sample", calibration="board")["layer"] == "L2"
    assert library.layer("sample")["layer"] == "L1"


def test_a_calibration_is_saved_listed_and_applied(library: Library) -> None:
    before = library.revision
    assert library.make_calibration(["white"]).startswith("calibration-")
    assert library.revision > before
    listed = library.calibrations()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["bands"] == 13
    assert entry["inputs"] == ["white"]
    assert entry["passed"] is True
    assert entry["exposure"] == 157
    assert (library.root / "calibrations" / entry["id"] / "reference.npz").is_file()
    x, y, w, h = samples.PATCHES["leaf"]
    spectrum = library.spectrum(
        "sample", {"rect": [x + 2, y + 2, w - 4, h - 4]}, calibration=entry["id"]
    )
    assert (spectrum["layer"], spectrum["unit"]) == ("L2", "relative_to_white")
    by_band = {band["band_id"]: band for band in spectrum["bands"]}
    assert by_band["led_12"]["mean"] == pytest.approx(0.55, abs=0.02)
    assert by_band["led_10"]["mean"] == pytest.approx(0.05, abs=0.02)
    assert by_band["led_12"]["count"] == (w - 4) * (h - 4)
    with pytest.raises(InvalidStateError, match="already"):
        library.make_calibration(["white"], entry["id"])


def test_two_requests_for_one_calibration_name_cannot_both_win(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    building = threading.Barrier(2)
    original = library_module.build_calibration

    def slow(*args: object, **kwargs: object) -> object:
        with contextlib.suppress(threading.BrokenBarrierError):
            building.wait(1)  # both get here only if both were let past the name check
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library_module, "build_calibration", slow)
    outcomes: list[str] = []

    def make() -> None:
        try:
            outcomes.append(library.make_calibration(["white"], "same"))
        except InvalidStateError:
            outcomes.append("refused")

    threads = [threading.Thread(target=make) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(outcomes) == ["refused", "same"]


def test_a_calibration_that_fails_does_not_take_its_name(library: Library) -> None:
    with pytest.raises(vispek_hc.VispekHCError):
        library.make_calibration(["frame"], "nothing")  # a single frame is no white scan
    assert not (library.root / "calibrations" / "nothing").exists()
    assert library.make_calibration(["white"], "nothing") == "nothing"


def test_a_half_written_calibration_is_not_found_and_its_name_is_free(library: Library) -> None:
    # What a crash or a full disk leaves: the data, without the document that completes it.
    left = library.root / "calibrations" / "half"
    left.mkdir()
    (left / "reference.npz").write_bytes(b"cut short")
    assert library.calibrations() == []
    with pytest.raises(FrameNotFoundError):
        library.calibration("half")
    with pytest.raises(FrameNotFoundError):
        library.layer("sample", calibration="half")
    assert library.make_calibration(["white"], "half") == "half"
    assert library.layer("sample", calibration="half")["layer"] == "L2"


@pytest.mark.parametrize("output", ["0", 1.5, [], None, -1, True])
def test_the_output_number_is_a_whole_number(library: Library, output: object) -> None:
    with pytest.raises(ConfigRejectedError):
        library.view_png("sample", "rgb", output=output)  # type: ignore[arg-type]
    with pytest.raises(ConfigRejectedError):
        library.export("sample", "envi", output=output)  # type: ignore[arg-type]


def test_a_calibration_that_does_not_fit_is_refused_unless_forced(library: Library) -> None:
    library.make_calibration(["white"], "board")
    with pytest.raises(CalibrationMismatchError) as caught:
        library.layer("small", calibration="board")
    assert caught.value.details["differing"]
    with pytest.raises(vispek_hc.VispekHCError):
        library.layer("small", calibration="board", force=True)  # another picture size


def test_a_calibration_with_fewer_bands_is_applied_to_the_bands_it_has(library: Library) -> None:
    # Seen on the device: a scan of LEDs 1-17 lost NDVI, PCA and the mean view because
    # the white reference (on paper, which fluoresces under UV) held LEDs 5-17 only.
    library.make_calibration(["white-visible"], "visible")
    layer = library.layer("sample", calibration="visible")
    assert layer["layer"] == "L2"
    assert [band["band_id"] for band in layer["bands"]] == [f"led_{n:02d}" for n in range(5, 12)]
    assert layer["left_out"] == [f"led_{n}" for n in range(12, 18)]
    assert layer["views"] == {"rgb": True, "cir": False, "uv": False, "ndvi": False, "ndwi": False}
    assert any("LED 12, 13, 14, 15, 16, 17" in warning for warning in layer["warnings"])
    assert picture(library.view_png("sample", "pca", calibration="visible")).shape == (72, 128, 3)
    with pytest.raises(ConfigRejectedError):
        library.view_png("sample", "band:led_13", calibration="visible")
    spectrum = library.spectrum("sample", {"point": [100, 60]}, calibration="visible")
    assert len(spectrum["bands"]) == 7
    whole = library.layer("sample")
    assert whole["left_out"] == []
    assert whole["views"] == library.summary("sample")["views"]


def test_spectra_of_points_and_rectangles(library: Library) -> None:
    point = library.spectrum("sample", {"point": [100, 60]})
    assert (point["layer"], point["unit"]) == ("L1", "image_code_value")
    assert [band["center_nm"] for band in point["bands"]] == sorted(
        band["center_nm"] for band in point["bands"]
    )
    assert point["bands"][0]["total"] == 1
    assert len(point["bands"][0]["rgb"]) == 3
    rect = library.spectrum("sample", {"rect": [10, 10, 4, 4]}, label="A")
    assert rect["name"] == "A"
    assert rect["bands"][0]["total"] == 16
    with pytest.raises(ConfigRejectedError):
        library.spectrum("sample", {"point": [5000, 5000]})
    with pytest.raises(ConfigRejectedError):
        library.spectrum("sample", {"circle": [1, 2, 3]})
    with pytest.raises(InvalidStateError):
        library.spectrum("frame", {"point": [1, 1]})


def test_spectra_as_csv(library: Library) -> None:
    text = library.spectra_csv(
        "sample", [("leaf", {"rect": [20, 20, 5, 5]}), ("board", {"point": [2, 2]})]
    ).decode("utf-8")
    lines = text.strip().splitlines()
    assert lines[0].startswith("capture_id,group_id,roi_id,name,geometry_json,layer,unit")
    assert len(lines) == 1 + 2 * 13


def test_a_single_frame_is_shown_as_it_is(library: Library) -> None:
    image = picture(library.view_png("frame", "rgb"))
    assert image.shape == (36, 64, 3)
    assert library.layer("frame")["layer"] == "frame"


def test_marked_pixels_are_drawn_over_the_picture(library: Library) -> None:
    plain = picture(library.view_png("sample", "band:led_09"))
    marked = picture(library.view_png("sample", "band:led_09", overlay=True))
    assert plain.shape == marked.shape
    # Nothing clips in the simulated scene: the overlay changes nothing.
    assert (plain == marked).all()


def test_the_scale_of_a_picture_and_its_colour_table(library: Library) -> None:
    library.make_calibration(["white"], "board")
    assert library.scale("sample", "rgb") is None, "three bands have no single scale"
    band = library.scale("sample", "band:led_09")
    assert band is not None
    assert (band["palette"], band["unit"]) == ("gray", "image_code_value")
    assert 5 <= band["low"] < band["high"] <= 255
    index = library.scale("sample", "ndvi", calibration="board")
    assert index == {"low": -1.0, "high": 1.0, "palette": "spectral", "unit": "index"}
    chosen = library.scale("sample", "band:led_09", low=10, high=20, palette="viridis")
    assert chosen == {"low": 10.0, "high": 20.0, "palette": "viridis", "unit": "image_code_value"}
    assert library.scale("frame", "rgb") is None
    with pytest.raises(InvalidStateError):
        library.scale("sample", "ndvi")
    strip = picture(library_module.palette_png("viridis"))
    assert strip.shape == (1, 256, 3)
    assert strip[0, 0].tolist() == [68, 1, 84]
    assert strip[0, 255].tolist() == [253, 231, 37]
    with pytest.raises(ConfigRejectedError):
        library_module.palette_png("rainbow")


def test_the_cube_is_loaded_once_for_many_pictures(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = []
    original = library_module.load_capture

    def counting(path: Path, *args: object, **kwargs: object) -> vispek_hc.Capture:
        loads.append(path)
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library_module, "load_capture", counting)
    for mode in ("rgb", "cir", "band:led_05", "band:led_06"):
        library.view_png("sample", mode)
    library.spectrum("sample", {"point": [3, 3]})
    assert len(loads) == 1


def test_thumbnails_are_small_and_kept(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    first = library.thumbnail("sample")
    image = picture(first)
    assert image.shape[1] <= library_module.THUMBNAIL_WIDTH
    assert image.shape[0] * 128 == image.shape[1] * 72
    assert list((library.root / ".cache" / "thumbnails").iterdir())
    assert picture(library.thumbnail("frame")).shape[1] == 64, "small pictures are not enlarged"
    assert picture(library.thumbnail("small")).ndim == 3, "no colour bands: the first band"

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("rendered again")

    monkeypatch.setattr(library_module, "render", fail)
    assert Library(library.root).thumbnail("sample") == first


def test_exports_go_into_the_data_folder(library: Library) -> None:
    library.make_calibration(["white"], "board")
    envi = library.export("sample", "envi")
    assert envi["directory"] == "exports/sample-L1-envi"
    assert "data.hdr" in envi["files"]
    assert (library.root / "exports" / "sample-L1-envi" / "data.bsq").is_file()
    tiff = library.export("sample", "tiff", calibration="board")
    assert tiff["directory"] == "exports/sample-L2-board-tiff"
    npz = library.export("sample", "npz", calibration="board")
    assert npz["files"] == ["cube.npz"]
    again = library.export("sample", "envi")
    assert again["directory"] == "exports/sample-L1-envi-2", "an export is never overwritten"
    with pytest.raises(ConfigRejectedError):
        library.export("sample", "docx")


def test_nothing_absolute_leaves_the_library(library: Library) -> None:
    library.make_calibration(["white"], "board")
    told = json.dumps(
        [library.captures(), library.calibrations(), library.export("sample", "envi")]
    )
    assert str(library.root) not in told
    assert str(Path.home()) not in told
