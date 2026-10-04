# SPDX-License-Identifier: Apache-2.0
"""The data folder of the viewer: captures and calibrations, addressed by name only.

```
<data folder>/
├── captures/<name>/        capture folders, as ``vispek-hc record`` writes them
├── calibrations/<name>/    calibration folders, as ``vispek-hc calibrate`` writes them
├── exports/<name>/         ENVI, TIFF and cube.npz files made on request
└── .cache/thumbnails/      small pictures for the list; safe to delete
```

Nothing here takes a path from a request: a capture is its folder's name, and a name is
letters, digits, ``.``, ``_`` and ``-``. Everything it returns is free of absolute paths.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import threading
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from vispek_hc import (
    HC1500,
    Calibration,
    Capture,
    Cube,
    Region,
    apply_calibration,
    export_envi,
    export_tiff,
    load_capture,
    render,
    spectrum,
    views,
    write_csv,
)
from vispek_hc import make_calibration as build_calibration
from vispek_hc.calibration import GATES
from vispek_hc.errors import (
    ConfigRejectedError,
    FrameNotFoundError,
    InvalidStateError,
    VispekHCError,
)


def _views(present: set[str]) -> dict[str, bool]:
    """Which composite and index views these bands allow."""
    needs = {**views.COMPOSITES, **views.INDICES}
    return {mode: set(needed) <= present for mode, needed in needs.items()}


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
THUMBNAIL_WIDTH = 320
CUBES_KEPT = 3  # a cube of 1280 x 720 x 17 takes about 125 MB
FOLDERS = ("captures", "calibrations", "exports")
EXPORTS = ("envi", "tiff", "npz")


def png(image: NDArray[np.uint8]) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, "PNG", compress_level=1)
    return buffer.getvalue()


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ConfigRejectedError(
            "a name is 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit",
            details={"name": name if isinstance(name, str) else None},
        )
    return name


class Library:
    """Captures and calibrations in one data folder. Safe to use from several threads."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        for folder in FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.revision = 0  # grows whenever the lists may have changed
        self._lock = threading.RLock()
        self._cubes: OrderedDict[tuple[Any, ...], Cube] = OrderedDict()
        self._loaded: tuple[tuple[str, int], Calibration] | None = None

    def changed(self) -> None:
        with self._lock:
            self.revision += 1

    # --- names -----------------------------------------------------------------------

    def _folder(self, kind: str, name: object, what: str) -> Path:
        path = self.root / kind / _check_name(name)
        if path.is_symlink():
            raise ConfigRejectedError(f"{what} '{name}' is a link; links are not followed")
        if not path.is_dir():
            raise FrameNotFoundError(f"there is no {what} named '{name}'")
        return path

    def reserve(self, name: object, prefix: str) -> tuple[str, Path]:
        """A name for a new capture and where it goes. The folder is not made here."""
        folder = self.root / "captures"
        if name is not None:
            chosen = _check_name(name)
            if (folder / chosen).exists() or (folder / chosen).is_symlink():
                raise InvalidStateError(
                    f"a capture named '{chosen}' already exists", hint="choose another name"
                )
            return chosen, folder / chosen
        stem = f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        chosen, count = stem, 1
        while (folder / chosen).exists():
            count += 1
            chosen = f"{stem}-{count}"
        return chosen, folder / chosen

    # --- captures --------------------------------------------------------------------

    def _document(self, path: Path) -> dict[str, Any] | None:
        try:
            document = json.loads((path / "capture.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _summary(self, name: str, document: dict[str, Any] | None) -> dict[str, Any]:
        if document is None:
            return {"id": name, "status": "damaged", "mode": None, "role": None,
                    "started_at_utc": "", "bands": [], "outputs": None}  # fmt: skip
        try:
            recording = document["recording"]
            config = document.get("config", {})
            groups = document.get("groups", [])
            mode = recording["mode"]
            complete = [group for group in groups if group.get("status") == "complete"]
            shown = (complete or groups or [{}])[0] if mode == "scan" else {}
            bands = [
                {
                    "band_id": f"led_{entry['led_id']:02d}",
                    "led_id": entry["led_id"],
                    "nm": entry.get("nm"),
                    "pwm": entry.get("pwm"),
                    "response_dn": entry.get("frame_check", {}).get("response_dn"),
                    "saturated_fraction": entry.get("frame_check", {}).get(
                        "rgb_saturated_fraction"
                    ),
                }
                for entry in shown.get("frames", [])
            ]
            present = {band["band_id"] for band in bands}
            limit = GATES["saturated_max"]  # more than this and a calibration is refused
            clipped = [b["led_id"] for b in bands if (b["saturated_fraction"] or 0) > limit]
            average = int(recording.get("average", 1))
            failure = next((g["failure"] for g in groups if g.get("failure")), None)
            return {
                "id": name,
                "capture_id": document.get("capture_id"),
                "mode": mode,
                "role": recording.get("role"),
                "status": recording.get("status"),
                "started_at_utc": recording.get("started_at_utc") or "",
                "finished_at_utc": recording.get("finished_at_utc"),
                "unit_id": document.get("unit_id"),
                "simulated": bool(document.get("device", {}).get("simulated")),
                "size": config.get("actual_size"),
                "bands": bands,
                "clipped": {"limit": limit, "led_ids": clipped},
                "outputs": len(complete) // average if mode == "scan" else None,
                "average": average,
                "failure": failure,
                "exposure": (config.get("exposure") or {}).get("effective"),
                "gain": config.get("gain"),
                "controls_locked": config.get("controls_locked"),
                "reduction": config.get("reduction"),
                "setup_label": config.get("setup_label"),
                "enclosure": config.get("enclosure"),
                "views": _views(present),
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            return self._summary(name, None)

    def captures(self) -> list[dict[str, Any]]:
        """Every capture of the data folder, newest first."""
        listed = []
        for path in (self.root / "captures").iterdir():
            if path.is_symlink() or not path.is_dir() or not NAME.fullmatch(path.name):
                continue
            if not (path / "capture.json").exists():
                continue  # a folder that is being made, or something else
            listed.append(self._summary(path.name, self._document(path)))
        return sorted(
            listed, key=lambda entry: (entry["started_at_utc"], entry["id"]), reverse=True
        )

    def summary(self, name: object) -> dict[str, Any]:
        path = self._folder("captures", name, "capture")
        return self._summary(path.name, self._document(path))

    def _capture(self, name: object) -> Capture:
        return load_capture(self._folder("captures", name, "capture"))

    # --- calibrations ----------------------------------------------------------------

    def calibration(self, name: object) -> Calibration:
        path = self._folder("calibrations", name, "calibration")
        if not (path / "calibration.json").is_file():
            raise FrameNotFoundError(f"calibration '{path.name}' is not complete")
        key = (path.name, (path / "calibration.json").stat().st_mtime_ns)
        with self._lock:
            if self._loaded is None or self._loaded[0] != key:
                self._loaded = (key, Calibration.load(path))
            return self._loaded[1]

    def calibrations(self) -> list[dict[str, Any]]:
        listed = []
        for path in (self.root / "calibrations").iterdir():
            if path.is_symlink() or not path.is_dir() or not NAME.fullmatch(path.name):
                continue
            try:
                document = json.loads((path / "calibration.json").read_text("utf-8"))
                quality = document["quality"]
                binding = document["binding"]
                listed.append(
                    {
                        "id": path.name,
                        "calibration_id": document.get("calibration_id"),
                        "created_at_utc": document.get("created_at_utc") or "",
                        "acquired_at_utc": document.get("acquired_at_utc"),
                        "bands": len(document["wavelengths_nm"]),
                        "channels": binding.get("channels"),
                        "inputs": [Path(str(entry.get("path", ""))).name
                                   for entry in document.get("inputs", [])],
                        "passed": all(band.get("pass") for band in quality["bands"]),
                        "warnings": list(quality.get("warnings", [])),
                        "white_roi": document.get("white_roi"),
                        "size": binding.get("actual_size"),
                        "exposure": binding.get("exposure"),
                        "unit_id": binding.get("unit_id"),
                    }
                )  # fmt: skip
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return sorted(
            listed, key=lambda entry: (entry["created_at_utc"], entry["id"]), reverse=True
        )

    def make_calibration(
        self,
        whites: list[Any],
        name: object = None,
        white_roi: tuple[float, float, float, float] | None = None,
    ) -> str:
        """Build a calibration from white-reference captures and save it under ``name``."""
        folder = self.root / "calibrations"
        if name is not None:
            _check_name(name)
        if not whites:
            raise ConfigRejectedError("name at least one white-reference capture")
        # Built and saved under the lock: of two requests for one name the second finds
        # it taken. A folder is a calibration once it holds calibration.json, which is
        # written last; what a crash left without it does not take the name.
        with self._lock:
            if name is None:
                stem = f"calibration-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                chosen, count = stem, 1
                while (folder / chosen).exists():
                    count += 1
                    chosen = f"{stem}-{count}"
            else:
                chosen = str(name)
                if (folder / chosen).is_symlink():
                    raise ConfigRejectedError(
                        f"calibration '{chosen}' is a link; links are not followed"
                    )
                if (folder / chosen / "calibration.json").exists():
                    raise InvalidStateError(
                        f"a calibration named '{chosen}' already exists",
                        hint="choose another name",
                    )
            made = build_calibration([self._capture(white) for white in whites], white_roi)
            made.save(folder / chosen)
            self._loaded = None
        self.changed()
        return chosen

    # --- cubes -----------------------------------------------------------------------

    def _cube(self, name: object, calibration: object, output: int, force: bool) -> Cube:
        if type(output) is not int or output < 0:
            raise ConfigRejectedError(
                "the output number is a whole number from 0", details={"output": str(output)}
            )
        path = self._folder("captures", name, "capture")
        key: tuple[Any, ...] = (
            path.name,
            (path / "capture.json").stat().st_mtime_ns if (path / "capture.json").exists() else 0,
            output,
        )
        with self._lock:
            if calibration is not None:
                reference = self.calibration(calibration)
                key = (*key, calibration, reference.sha256(), force)
            if key in self._cubes:
                self._cubes.move_to_end(key)
                return self._cubes[key]
            capture = load_capture(path)
            if capture.mode != "scan":
                raise InvalidStateError(
                    f"'{path.name}' is a {capture.mode} capture: it has frames, no bands",
                    hint="spectra and band views need a scan",
                )
            if calibration is None:
                cube = capture.cube(output)
            else:
                try:
                    # A capture may hold bands the calibration has no white reference
                    # for (UV, when the reference is paper): the others are still good.
                    cube = apply_calibration(
                        capture, reference, output=output, force=force, partial=True
                    )
                except ValueError as error:  # arrays that do not fit, when forced
                    raise InvalidStateError(
                        "the calibration cannot be applied to this capture at all: "
                        "the pictures differ in size or bands",
                        details={"reason": str(error)},
                    ) from error
            self._cubes[key] = cube
            while len(self._cubes) > CUBES_KEPT:
                self._cubes.popitem(last=False)
            return cube

    def layer(
        self, name: object, *, calibration: object = None, output: int = 0, force: bool = False
    ) -> dict[str, Any]:
        """What a capture holds once loaded (and calibrated): layer, unit, bands, warnings."""
        summary = self.summary(name)
        if summary["mode"] != "scan":
            return {"layer": "frame", "unit": "image_code_value", "bands": [], "warnings": []}
        cube = self._cube(name, calibration, output, force)
        applied = cube.metadata.get("calibration") or {}
        left_out = list(applied.get("bands_left_out", []))
        warnings = list(applied.get("warnings", []))
        if left_out:
            leds = ", ".join(str(int(band_id.removeprefix("led_"))) for band_id in left_out)
            warnings.append(
                f"This calibration has no white reference for LED {leds}: "
                "those bands are shown only without a calibration."
            )
        return {
            "layer": cube.metadata["layer"],
            "unit": cube.metadata["unit"],
            "reduction": cube.metadata.get("reduction"),
            "size": [cube.values.shape[1], cube.values.shape[0]],
            "bands": [
                {
                    "band_id": band.band_id,
                    "name": band.name,
                    "center_nm": band.center_nm,
                    "dominant_channel": band.dominant_channel,
                }
                for band in cube.bands
            ],
            "left_out": left_out,
            "views": _views({band.band_id for band in cube.bands}),
            "warnings": warnings,
            "forced": bool(applied.get("forced", force and calibration is not None)),
        }

    # --- pictures --------------------------------------------------------------------

    def view_png(
        self,
        name: object,
        mode: str,
        *,
        calibration: object = None,
        output: int = 0,
        force: bool = False,
        low: float | None = None,
        high: float | None = None,
        gamma: float = 1.0,
        palette: str = "auto",
        overlay: bool = False,
    ) -> bytes:
        """One view of a capture as PNG. A single frame is shown as it is."""
        summary = self.summary(name)
        if summary["mode"] in ("single", "dark"):
            return png(self._capture(name).frame())
        if summary["mode"] != "scan":
            raise InvalidStateError(
                f"a {summary['mode'] or 'damaged'} capture cannot be shown",
                hint="'vispek-hc repair' rebuilds a damaged capture",
            )
        with self._lock:
            cube = self._cube(name, calibration, output, force)
            image = render(
                cube, mode, low=low, high=high, gamma=gamma,
                palette=palette,  # type: ignore[arg-type]
                banner=False,
            )  # fmt: skip
            if overlay:
                image = _marked(image, cube, mode)
            return png(image)

    def scale(
        self,
        name: object,
        mode: str,
        *,
        calibration: object = None,
        output: int = 0,
        force: bool = False,
        low: float | None = None,
        high: float | None = None,
        palette: str = "auto",
    ) -> dict[str, Any] | None:
        """What the two ends of a view's colour table stand for; None when it has none
        (pictures of three bands, single frames)."""
        if self.summary(name)["mode"] != "scan":
            return None
        with self._lock:
            cube = self._cube(name, calibration, output, force)
            limits = views.view_range(cube, mode, low=low, high=high)
        if limits is None:
            return None
        usual = "gray" if mode.startswith("band:") else "spectral"
        return {
            "low": limits[0],
            "high": limits[1],
            "palette": usual if palette == "auto" else palette,
            "unit": "index" if mode in views.INDICES else cube.metadata["unit"],
        }

    def thumbnail(self, name: object) -> bytes:
        """A small picture of a capture for the list, kept in ``.cache/thumbnails``."""
        path = self._folder("captures", name, "capture")
        stamp = (path / "capture.json").stat().st_mtime_ns
        cache = self.root / ".cache" / "thumbnails"
        kept = cache / f"{path.name}.{stamp}.png"
        if kept.is_file():
            return kept.read_bytes()
        summary = self.summary(name)
        with self._lock:
            if summary["mode"] in ("single", "dark"):
                image = self._capture(name).frame()
            else:
                cube = self._cube(name, None, 0, False)
                mode = "rgb" if summary["views"].get("rgb") else f"band:{cube.bands[0].band_id}"
                image = render(cube, mode, banner=False)
        picture = Image.fromarray(image)
        if picture.width > THUMBNAIL_WIDTH:
            height = round(picture.height * THUMBNAIL_WIDTH / picture.width)
            picture = picture.resize((THUMBNAIL_WIDTH, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, "PNG")
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.glob(f"{path.name}.*.png"):
            old.unlink(missing_ok=True)
        kept.write_bytes(buffer.getvalue())
        return buffer.getvalue()

    # --- spectra ---------------------------------------------------------------------

    def spectrum(
        self,
        name: object,
        geometry: dict[str, Any],
        *,
        calibration: object = None,
        output: int = 0,
        force: bool = False,
        label: str = "",
    ) -> dict[str, Any]:
        """Statistics per band of a point ``{"point": [x, y]}`` or ``{"rect": [x, y, w, h]}``."""
        region = Region(label or "region", geometry)
        with self._lock:
            cube = self._cube(name, calibration, output, force)
            rows = spectrum(cube, region)
            selected = region.pixels(*cube.values.shape[:2])
            raw = cube.rgb[selected].mean(axis=0)  # (bands, 3): the camera's own numbers
        index = {band.band_id: position for position, band in enumerate(cube.bands)}
        return {
            "name": label,
            "geometry": geometry,
            "layer": cube.metadata["layer"],
            "unit": cube.metadata["unit"],
            "bands": [
                {**asdict(row), "rgb": [round(float(v), 2) for v in raw[index[row.band_id]]]}
                for row in rows
            ],
        }

    def spectra_csv(
        self,
        name: object,
        regions: list[tuple[str, dict[str, Any]]],
        *,
        calibration: object = None,
        output: int = 0,
        force: bool = False,
    ) -> bytes:
        made = [Region(label, geometry) for label, geometry in regions]
        with self._lock, tempfile.TemporaryDirectory() as folder:
            cube = self._cube(name, calibration, output, force)
            return write_csv(Path(folder) / "spectra.csv", cube, made).read_bytes()

    # --- export ----------------------------------------------------------------------

    def export(
        self,
        name: object,
        kind: str,
        *,
        calibration: object = None,
        output: int = 0,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write a cube as ENVI, TIFF or cube.npz into ``exports/``; never over an old one."""
        if kind not in EXPORTS:
            raise ConfigRejectedError(
                f"unknown export format '{kind}'", details={"available": list(EXPORTS)}
            )
        with self._lock:
            cube = self._cube(name, calibration, output, force)
            parts = [str(name), cube.metadata["layer"]]
            if calibration is not None:
                parts.append(str(calibration))
            stem = "-".join([*parts, kind])
            chosen, count = stem, 1
            while (self.root / "exports" / chosen).exists():
                count += 1
                chosen = f"{stem}-{count}"
            target = self.root / "exports" / chosen
            target.mkdir(parents=True)
            if kind == "envi":
                export_envi(cube, target)
            elif kind == "tiff":
                export_tiff(cube, target)
            else:
                cube.save(target / "cube.npz")
        return {
            "directory": f"exports/{chosen}",
            "files": sorted(path.name for path in target.iterdir()),
            "layer": cube.metadata["layer"],
        }


def palette_png(name: object) -> bytes:
    """A colour table as a picture one pixel high and 256 wide."""
    if not isinstance(name, str) or name not in views.LUTS:
        raise ConfigRejectedError(
            f"unknown palette '{name}'", details={"available": list(views.LUTS)}
        )
    return png(views.LUTS[name][None, :, :])


def _marked(image: NDArray[np.uint8], cube: Cube, mode: str) -> NDArray[np.uint8]:
    """Draw the clipped and invalid pixels of the bands a view uses over the picture."""
    if mode.startswith("band:"):
        used = [mode.removeprefix("band:")]
    else:
        used = list(views.COMPOSITES.get(mode) or views.INDICES.get(mode) or [])
    if not used:
        return image
    mask = np.zeros(cube.mask.shape[:2], np.uint8)
    for band_id in used:
        mask |= cube.mask[:, :, cube.band_index(band_id)]
    layer = views.quality_overlay(mask)
    alpha = layer[..., 3:4].astype(np.float32) / 255
    mixed = image.astype(np.float32) * (1 - alpha) + layer[..., :3].astype(np.float32) * alpha
    return np.round(mixed).astype(np.uint8)


def led_name(led_id: int) -> str:
    try:
        return HC1500.led(led_id).name
    except VispekHCError:
        return f"LED {led_id}"
