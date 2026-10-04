# Changelog

The format follows [Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-04

The first release: a local web page for the Vispek HC1500, built on `vispek-hc` 0.1.0.
Live picture with clipping, histogram and sharpness; LED control with the UV gate;
camera parameters locked in darkness; scans, white references and calibrations; RGB,
CIR, UV, band, NDVI, NDWI, PCA and mean views; spectra and CSV; ENVI, TIFF and
`cube.npz` export; the simulated device. Tried on one device on macOS (Apple silicon).

### Added

- Installed from PyPI with the SDK it needs (`pip install vispek-hc-viewer`); CI tests
  against the SDK release on PyPI.

- The device state carries `gain_state`, and "locked" now also means: the gain the
  camera keeps was settled in darkness, and the stream was not reopened since. A lock
  with the lid open is refused (`GAIN_NOT_CONFIRMED`) and the Device card says what to
  do; a reopened stream shows `LOCK_STALE`. Measured on the device: the camera keeps a
  gain from the moment of the lock, up to 4.4 times apart, that no control reads back.
- A calibration that covers fewer bands than a capture holds is applied to the bands it
  has: a scan of LEDs 1-17 with a white reference of LEDs 5-17 keeps reflectance, NDVI,
  NDWI, PCA and the mean view for 5-17; the UV bands are shown with "None (raw)". The
  layer reports `left_out` and `views`. Found on the device: such a scan lost every view
  that needs a calibration.
- A capture's summary names the bands that clipped too much for a calibration
  (`clipped`), and its card says so. A white reference that is refused for clipping
  keeps its message on screen and offers "Search the PWM and record again". Found on the
  device: three white references in a row were recorded too bright, and once the message
  had gone nothing said why no calibration could be built.
- Clicking "UV locked" explains how ultraviolet is permitted (only at program start).
  "Parameters locked" says what it means, on the card and as a tooltip.
- `vispek-hc-viewer`: a server on 127.0.0.1 and one page. Token in the address fragment,
  `Authorization: Bearer` on every request, single-use tickets for the live stream,
  `Host` and `Origin` checks, a content security policy without inline code.
- Device: connect and disconnect, device listing, parameter lock, "Check" (pairing
  probe and black level), "Tune".
- Illumination: one LED at a time at a chosen PWM, the PWM a unit scans with, all off.
  LEDs 1-4 only with `--allow-uv`, enforced by the server.
- Camera: exposure and gain, stored in the unit's profile and locked again.
- Live picture as multipart JPEG, clipping overlay, histogram, clipped share, sharpness,
  mean of a region.
- Jobs on their own thread with progress and stop: scan, white reference (optionally
  with the PWM search, and building a calibration), single frame, dark frame.
- An LED lit by hand goes off after 30 s without contact from a page.
- Captures and calibrations by name in one data folder; thumbnails; pictures (RGB, CIR,
  UV, band, NDVI, NDWI, principal components, mean) with stretch, gamma, colour tables,
  a colour scale and marks for clipped and invalid pixels; spectra of points and
  rectangles; CSV; ENVI, TIFF and `cube.npz` export.
- The simulated device of `vispek-hc` in real time (`--simulate`), with the sample or the
  white board in front of its camera.

- Display range: "Standard" (0 to 1 for calibrated pictures, the 1st to 99th percentile
  for raw data), "Auto" and "Manual". A uniform white target used to be shown as its own
  noise, stretched.

### Verified on hardware

One HC1500 on macOS (Apple silicon), 2026-10-02: connecting, the live picture under LED 6
and LED 13, all off, a scan of LEDs 5-17 started from the page, band pictures and spectra
of that scan, the 30-second switch-off with the page closed, and with a sheet of white
paper as the target: a white reference with the PWM search, the calibration built from
it, and three scans that came out at 1.000 of it in every band. Not verified on
hardware: a real reflectance standard, UV LEDs, Linux and Windows.
