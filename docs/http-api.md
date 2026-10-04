# HTTP API

For the page, and for scripts on the same computer. Every route below needs
`Authorization: Bearer <token>` (the token is in the address the program prints), except
the live stream, which takes a ticket. Bodies and answers are JSON unless stated.

An error is `{"error": {"code", "message", "hint", "details"}}` with the codes of
`vispek-hc` (`docs/errors.md` there), plus `UNAUTHORISED`, `FORBIDDEN` and
`INTERNAL_ERROR`. Statuses: 400 a request that makes no sense, 401 no valid token,
403 ultraviolet without permission or a foreign `Host` / `Origin`, 404 unknown name,
405 a method the route does not take, 409 not now (busy, already exists, calibration does
not fit), 413 a body over 1 MB, 415 a body that is not `application/json`, 422 quality,
502 the hardware failed, 503 the device or something it needs is missing, or too many
live streams.

## Device

| Route | Body | Answer |
|---|---|---|
| `GET /api/state` | | the whole state: `connected`, `device`, `leds`, `job`, `library_revision`, `notices`, `preview`, `allow_uv` |
| `GET /api/devices` | | `serial`, `cameras`, `matching_cameras`, `sizes`, `problems` (takes a few seconds) |
| `POST /api/connect` | `simulate`, `port`, `camera`, `unit`, `size` `[w, h]`, `fps`, `source` (all optional) | state |
| `POST /api/disconnect` | | state |
| `POST /api/scene` | `name`: `sample` or `white` (simulated device only) | state |
| `POST /api/led` | `led_id`, `on`, `pwm` (optional: the scan PWM) | state |
| `POST /api/leds/off` | | state; stops a running job first |
| `POST /api/leds/scan-pwm` | `led_id`, `pwm` (null: the model's value) | state |
| `GET /api/camera` | | `available`, `locked`, `editable`, `values` (current, minimum, maximum, step) |
| `POST /api/camera` | `name` (`exposure-time-abs` or `gain`), `value` | as `GET /api/camera` |
| `POST /api/camera/lock` | | as `GET /api/camera` |

## Live picture

| Route | Body or query | Answer |
|---|---|---|
| `POST /api/preview/ticket` | | `ticket`: opens one stream within 30 s |
| `GET /api/preview.mjpg?ticket=` | | `multipart/x-mixed-replace` of JPEG frames |
| `POST /api/preview/options` | `clipping` | state |
| `GET /api/live` | `x`, `y`, `w`, `h` (optional region) | `size`, `mean`, `max`, `saturated`, `sharpness`, `histogram` (3 x 64), `probe` |

## Jobs

| Route | Body | Answer |
|---|---|---|
| `POST /api/jobs` | `kind`: `scan`, `white`, `tune`, `single`, `dark`, `check`; for scans `name`, `channels`, `average`, `pwm`, `min_on_ms`, `reduction`, `white_roi`, `setup_label`, `enclosure`, `unlocked`; for `white` also `auto_pwm`, `calibrate`, `calibration_name` | 202 and the job |
| `POST /api/jobs/stop` | | state |

A job: `id`, `kind`, `state` (`running`, `done`, `failed`, `cancelled`), `phase`, `band`,
`bands`, `led_id`, `capture`, `calibration`, `error`, `warnings`, `stopping`. Its
progress is read from `GET /api/state`.

## Captures and calibrations

`{name}` is a folder name in the data folder: 1-64 letters, digits, `.`, `_`, `-`,
starting with a letter or digit.
Routes that compute values take `calibration` (a name), `output` (default 0) and `force`.

| Route | Query or body | Answer |
|---|---|---|
| `GET /api/captures` | | `captures` (newest first), `revision` |
| `GET /api/captures/{name}` | | the summary of one capture; `clipped` holds `limit` and the `led_ids` whose share of clipped pixels is above it |
| `GET /api/captures/{name}/thumbnail.png` | | PNG |
| `GET /api/captures/{name}/layer` | | `layer` (`L1`, `L2`, `frame`), `unit`, `bands`, `left_out` (bands the calibration does not cover), `views` (which composite and index views these bands allow), `warnings`, `forced` |
| `GET /api/captures/{name}/view.png` | `mode` (`rgb`, `cir`, `uv`, `band:led_NN`, `ndvi`, `ndwi`, `pca`, `heat`), `low`, `high`, `gamma`, `palette`, `overlay` | PNG |
| `GET /api/captures/{name}/scale` | as `view.png` | `low`, `high`, `palette`, `unit`, or null |
| `GET /api/captures/{name}/spectrum` | `x`, `y`, and `w`, `h` for a rectangle; `name` | `geometry`, `layer`, `unit`, `bands` (mean, std, median, min, max, count, rgb) |
| `POST /api/captures/{name}/spectra.csv` | `regions`: `[{name, point or rect}]` | CSV |
| `POST /api/captures/{name}/export` | `format`: `envi`, `tiff`, `npz` | `directory` (inside the data folder), `files`, `layer` |
| `GET /api/calibrations` | | `calibrations` (newest first) |
| `POST /api/calibrations` | `whites`: capture names; `name`; `white_roi` | `id` |
| `GET /api/palettes/{name}.png` | | a colour table, 256 x 1 |
