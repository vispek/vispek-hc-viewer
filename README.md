# vispek-hc-viewer

A local web viewer for the Vispek HC1500 multispectral camera: live picture, LED control,
scans, white references, band pictures, indices and spectra, in one page served to your
own computer. It is built on the [`vispek-hc`](https://github.com/vispek/vispek-hc-camera) SDK and adds nothing
to what a capture is: everything it records is an ordinary capture folder that the
`vispek-hc` command line reads too.

> **Ultraviolet.** LEDs 1 and 2 of the HC1500 emit UV-C, LEDs 3 and 4 UV-A. UV-C injures
> eyes and skin and is invisible. The viewer cannot light LEDs 1-4 unless it is started
> with `--allow-uv`. Read [`SAFETY.md`](https://github.com/vispek/vispek-hc-camera/blob/main/SAFETY.md) of `vispek-hc` before you do that. If the program
> is killed (not ended with Ctrl-C), an LED stays lit: unplug every cable of the device,
> the camera cable too, to be sure.

中文说明：[README.zh-CN.md](README.zh-CN.md)

## Status

Pre-release (0.1). Tried on one real HC1500 on macOS (Apple silicon): live picture, LED
control, scans of LEDs 5-17, a white reference on white paper with the calibration built
from it, pictures and spectra. Linux: built, not tried on a device. Windows: not
supported, because the SDK cannot lock the camera parameters there yet.

## Start

```bash
python3 -m venv hc-env && source hc-env/bin/activate
pip install vispek-hc-viewer        # installs the vispek-hc SDK as well
vispek-hc setup                     # macOS: compiles the camera-control helper, once
vispek-hc-viewer --simulate         # no hardware: the simulated device
vispek-hc-viewer                    # the real device
```

The program prints an address such as `http://127.0.0.1:8642/#token=...` and opens it.
The part after `#` is the key to the page; whoever has it can drive the device, so do not
paste it anywhere. Ctrl-C in the terminal ends the viewer and switches every LED off.

A real device needs what `vispek-hc` needs: `ffmpeg` on the `PATH`, camera access for the
terminal the viewer was started from (macOS asks the first time), and on macOS the
camera-control helper that `vispek-hc setup` compiles. `vispek-hc check` says what is
missing.

| Option | |
|---|---|
| `--data-dir DIR` | where captures, calibrations and exports go (default `./vispek-hc-data`) |
| `--port N` | port on 127.0.0.1 (default 8642; 0 picks a free one) |
| `--simulate` | open the simulated device only; no hardware is touched |
| `--allow-uv` | permit LEDs 1-4 |
| `--no-browser` | do not open the page |
| `-v` | log every request |

## The page

- **Device** (left): connect and disconnect; which port and camera; "Check" blinks LED 6
  and reports whether the camera saw it, the delay, and the black level. "Tune" is for a
  new unit with the white board in view.
- **Illumination** (left): the 17 LEDs. A switch lights one LED at the PWM beside it;
  lighting another switches the first off. "S" stores that PWM as the one scans use.
  LEDs 1-4 are locked unless the viewer was started with `--allow-uv`.
- **Camera** (left): exposure and gain. Changing them makes earlier white references
  unusable for new scans, because a calibration only fits scans made with the same
  settings.
- **Live** (centre): the camera's picture, about ten times a second. "Show clipping"
  draws every pixel at 255 in magenta. Drag a rectangle to read its mean; the histogram,
  the clipped share and a sharpness number (for focusing the lens) are on the right.
- **Recording** (bottom left): "Scan" records one frame per chosen LED and a dark frame;
  "White reference" does the same with the white board in view and builds a calibration;
  "Frame" and "Dark" save single pictures. "Channels & options" chooses the LEDs, averaging and
  what is written into the capture.
- **Captures** (bottom): everything in the data folder. Click one to open it.
- **A capture** (centre and right): colour pictures (RGB, CIR, UV), single bands (arrow
  keys step through them), NDVI, NDWI, principal components and the mean, with a colour
  scale. Calibrated pictures are shown on a fixed range of 0 to 1 by default, so that
  white is white and two captures can be compared; "Auto" stretches each picture by
  itself, "Manual" takes your own limits. Choose a calibration to see values relative to the white board; without one the
  picture is marked RAW and the views that compare bands are switched off, because on raw
  data they would only show how hard each LED was driven. Click for the spectrum of a
  pixel, drag for a region (mean and standard deviation), keep up to eight, save them as
  CSV. "Export" writes ENVI, TIFF or `cube.npz` into the data folder.

Mouse: wheel zooms, right-drag (or Alt-drag) moves, double-click fits.

## A first session

1. Connect. Press "Check": it should say "paired" and black level 5, 5, 5 (in a dark box).
2. Put the white board in view, press "White reference". A calibration with the same
   name appears.
3. Put the sample in view, press "Scan".
4. The scan opens with the calibration applied. Click and drag on it for spectra.

## The data folder

```
vispek-hc-data/
├── captures/<name>/        capture folders (see docs/data-format.md of vispek-hc)
├── calibrations/<name>/    calibration folders
├── exports/<name>/         ENVI, TIFF and cube.npz files made with "Export"
└── .cache/thumbnails/      small pictures for the list; safe to delete
```

Copy capture folders made with `vispek-hc record` into `captures/` and press "Refresh"
to see them. The viewer never deletes or overwrites a capture, a calibration or an
export.

## What it does to keep the device and the computer safe

- It listens on 127.0.0.1 only, and nothing can change that.
- Every request needs the token from the address the program printed.
- LEDs 1-4 are refused by the server, not just hidden in the page, unless `--allow-uv`.
- While a scan runs, nothing else can switch an LED.
- An LED lit by hand goes off after 30 s without contact from a page (the page was
  closed, the computer slept, or the browser put a long-hidden page to sleep).
- Ending the program switches every LED off and says so if that could not be confirmed.

More in [docs/security.md](docs/security.md).

## Limits

- One device, and one page driving it at a time. A second page sees the same state.
- The picture and the numbers are 8-bit code values after the camera's own processing.
  They are not radiance; "relative to white" is an index that is comparable only between
  scans made with identical settings (see `docs/calibration.md` of `vispek-hc`).
- Video captures are listed but cannot be shown.

## Development

```bash
uv sync --group dev      # takes ../vispek-hc-camera when it is there, PyPI otherwise: --no-sources
uv run pytest            # no hardware, no network
uv run ruff format --check . && uv run ruff check . && uv run mypy
```

[AGENTS.md](AGENTS.md) is the guide for people and coding agents changing the code;
[docs/http-api.md](docs/http-api.md) lists every route.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
