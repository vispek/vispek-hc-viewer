// SPDX-License-Identifier: Apache-2.0
// Vispek HC Viewer: the page. No framework, no build step. It asks the local server for
// everything (/api/...), with the token the server handed over in the address fragment.
"use strict";

// --- small helpers ---------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "on") for (const [name, run] of Object.entries(value)) node.addEventListener(name, run);
    else if (key === "data") Object.assign(node.dataset, value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function s(tag, attrs, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

// Appends children, leaving out what is null, undefined or false.
function put(node, ...children) {
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

const fixed = (value, digits = 1) =>
  value === null || value === undefined || Number.isNaN(value) ? "–" : Number(value).toFixed(digits);

function localTime(utc) {
  if (!utc) return "";
  const date = new Date(utc);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// A colour to recognise an LED by. Ultraviolet and infrared have none; they get a hint.
function wavelengthColour(nm) {
  if (nm < 380) return "#b58cff";
  if (nm > 750) {
    const t = Math.min(1, (nm - 750) / 300);
    const mix = (a, b) => Math.round(a + (b - a) * t);
    return `rgb(${mix(168, 110)}, ${mix(52, 92)}, ${mix(44, 96)})`;
  }
  let r = 0, g = 0, b = 0;
  if (nm < 440) { r = (440 - nm) / 60; b = 1; }
  else if (nm < 490) { g = (nm - 440) / 50; b = 1; }
  else if (nm < 510) { g = 1; b = (510 - nm) / 20; }
  else if (nm < 580) { r = (nm - 510) / 70; g = 1; }
  else if (nm < 645) { r = 1; g = (645 - nm) / 65; }
  else { r = 1; }
  const level = (v) => Math.round(255 * Math.pow(Math.max(0, v), 0.8));
  return `rgb(${level(r)}, ${level(g)}, ${level(b)})`;
}

const REGION_COLOURS = ["#4cc9f0", "#ff5d8f", "#ffd166", "#06d6a0", "#ff9f1c", "#b388ff", "#a3e635", "#f8f9fa"];
const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

// --- token and requests ----------------------------------------------------------------

const token = (() => {
  const found = /token=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (found) {
    try { sessionStorage.setItem("vispek-hc-viewer-token", found[1]); } catch { /* private mode */ }
    history.replaceState(null, "", location.pathname);
    return found[1];
  }
  try { return sessionStorage.getItem("vispek-hc-viewer-token"); } catch { return null; }
})();

// A new address in a tab that already shows the page changes only the fragment: no load.
window.addEventListener("hashchange", () => {
  if (/token=/.test(location.hash)) location.reload();
});

class ApiError extends Error {
  constructor(error, status) {
    super(error.message || "the request failed");
    this.code = error.code || `HTTP_${status}`;
    this.hint = error.hint || null;
    this.details = error.details || {};
    this.status = status;
  }
}

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  if (options.method === "POST") headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.method === "POST" ? JSON.stringify(options.body || {}) : undefined,
    cache: "no-store",
  });
  if (response.status === 401) {
    curtain("This page has no valid token. Open the address that vispek-hc-viewer printed when it started.");
  }
  if (!response.ok) {
    let error = { message: response.statusText };
    try { error = (await response.json()).error; } catch { /* not JSON */ }
    throw new ApiError(error, response.status);
  }
  const type = response.headers.get("Content-Type") || "";
  return type.startsWith("application/json") ? response.json() : response.blob();
}

const get = (path) => api(path);
const post = (path, body) => api(path, { method: "POST", body });

// Pictures need the token too, so they are fetched and shown from memory. Whoever asks
// for the address of one gives it back (URL.revokeObjectURL) once the picture is loaded.
async function pictureUrl(path) {
  return URL.createObjectURL(await api(path));
}

// The colour tables are few and small: they are kept for as long as the page lives.
const paletteBars = new Map();
function paletteUrl(name) {
  if (!paletteBars.has(name)) {
    paletteBars.set(name, pictureUrl(`/api/palettes/${name}.png`));
    paletteBars.get(name).catch(() => paletteBars.delete(name));
  }
  return paletteBars.get(name);
}

// Puts a fetched picture on the stage and gives the one before it back.
let shownUrl = null;
function showPicture(url) {
  const before = shownUrl;
  shownUrl = url && url.startsWith("blob:") ? url : null;
  picture.src = url;
  if (before && before !== url) URL.revokeObjectURL(before);
}

function query(parameters) {
  const parts = [];
  for (const [key, value] of Object.entries(parameters)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    parts.push(`${key}=${encodeURIComponent(value === true ? 1 : value)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

// --- state -----------------------------------------------------------------------------

const S = {
  server: null,          // the last answer of /api/state
  offline: false,
  tab: "live",
  devices: null,         // the last answer of /api/devices
  camera: null,          // the last answer of /api/camera
  cameraFor: null,
  captures: [],
  calibrations: [],
  revision: -1,
  capture: null,         // what the capture tab shows
  preferredCalibration: null,
  regions: [],
  regionCount: 0,
  live: null,            // the last answer of /api/live
  probe: null,           // a region of the live picture: {x, y, w, h}
  streaming: false,
  manualPwm: {},
  seenJob: null,
  seenNotice: null,
  view: { scale: 1, x: 0, y: 0, fit: true, width: 0, height: 0 },
  options: loadOptions(),
  busy: new Set(),
};

function loadOptions() {
  const defaults = {
    channels: null, average: 1, reduction: "dominant", enclosure: "darkbox", label: "",
    autoPwm: false, calibrate: true, whiteRoi: false, unlocked: false,
  };
  try { return { ...defaults, ...JSON.parse(localStorage.getItem("vispek-hc-viewer-options") || "{}") }; }
  catch { return defaults; }
}

function saveOptions() {
  try { localStorage.setItem("vispek-hc-viewer-options", JSON.stringify(S.options)); } catch { /* private mode */ }
}

const connected = () => Boolean(S.server && S.server.connected);

// Why the camera parameters are not locked, and what to do, in the page's own words.
function lockNote(error) {
  if (!error) return "The camera parameters are not locked.";
  if (error.code === "GAIN_NOT_CONFIRMED") return "The camera saw light when its parameters were locked, so the gain it keeps from that moment is unknown. Close the lid of the dark box, then press “Lock again” in the Camera card.";
  if (error.code === "LOCK_STALE") return "The camera stream was reopened: its parameters are no longer known to be locked. Close the lid and press “Lock again”; the next scan also does it.";
  return `${error.message}. Scans need “Without a parameter lock” in the options.`;
}
const jobRunning = () => Boolean(S.server && S.server.job && S.server.job.state === "running");
const ledOf = (id) => (S.server ? S.server.leds.find((led) => led.led_id === id) : null);

// --- toasts and the curtain ------------------------------------------------------------

function toast(level, title, text, extra = {}) {
  const node = h("div", { class: `toast ${level}` },
    h("div", { class: "toast-title" }, title, extra.code ? h("span", { class: "toast-code", text: `  ${extra.code}` }) : null),
    h("button", { class: "toast-close", type: "button", title: "Dismiss", text: "×", on: { click: () => node.remove() } }),
    text ? h("div", { class: "toast-text", text }) : null,
    extra.hint ? h("div", { class: "toast-hint", text: extra.hint }) : null,
    extra.actions ? h("div", { class: "toast-actions" }, extra.actions.map((action) =>
      h("button", { class: "button small", type: "button", text: action.label, on: { click: () => { node.remove(); action.run(); } } }))) : null);
  $("toasts").append(node);
  while ($("toasts").children.length > 5) $("toasts").firstChild.remove();
  if (!extra.sticky) setTimeout(() => node.remove(), level === "error" ? 16000 : 7000);
  return node;
}

function fail(error, title) {
  if (error instanceof ApiError) {
    toast("error", title, error.message, { hint: error.hint, code: error.code });
  } else {
    toast("error", title, "The viewer program does not answer. Is it still running?");
  }
}

// The page cannot go on (no token, or one the server no longer accepts): say so and stop asking.
function curtain(text) {
  $("curtain-text").textContent = text;
  $("curtain").hidden = false;
  S.stopped = true;
  clearTimeout(timer);
  showPicture(BLANK);
}

// Runs one request for a control: the control cannot be used twice while it runs.
async function act(key, title, work) {
  if (S.busy.has(key)) return undefined;
  S.busy.add(key);
  render();
  try {
    return await work();
  } catch (error) {
    fail(error, title);
    return undefined;
  } finally {
    S.busy.delete(key);
    render();
  }
}

// --- top bar ---------------------------------------------------------------------------

function chip(kind, ...content) {
  return h("span", { class: `chip ${kind}` }, ...content);
}

function renderTopbar() {
  const chips = clear($("chips"));
  const server = S.server;
  if (S.offline || !server) {
    put(chips, chip("bad", h("span", { class: "dot" }), "The viewer program does not answer"));
  } else if (!server.connected) {
    put(chips, chip("", h("span", { class: "dot" }), server.connecting ? "Connecting…" : "No device connected"));
  } else {
    const device = server.device;
    put(chips, chip("good", h("span", { class: "dot" }), "Connected ", h("b", { text: device.unit_id })));
    if (device.simulated) put(chips, chip("warn", "Simulated device"));
    if (device.size) put(chips, chip("", h("b", { text: `${device.size[0]}×${device.size[1]}` }), device.fps ? h("b", { text: ` ${fixed(device.fps, 1)} fps` }) : null));
    const lock = device.locked ? chip("good", "Parameters locked") : chip("warn", "Parameters not locked");
    lock.title = device.locked ? "White balance, exposure and gain are fixed and were read back: scans are comparable."
      : "The camera may adjust white balance or exposure by itself: scans are not comparable. Use “Lock again” in the Camera card.";
    put(chips, lock);
    if (device.stale) put(chips, chip("bad", "No frames"));
    const job = server.job;
    if (job && job.state === "running") put(chips, chip("warn", `${jobName(job.kind)} running`));
  }
  const uv = $("uv-chip");
  uv.className = server && server.allow_uv ? "chip uv" : "chip";
  uv.textContent = server ? (server.allow_uv ? "UV enabled" : "UV locked") : "";
  uv.title = server && server.allow_uv
    ? "LEDs 1-4 can be lit. UV-C injures eyes and skin: keep the light source shielded."
    : "LEDs 1-4 cannot be lit. Click to read how they are permitted.";
  $("all-off").disabled = !connected() || S.busy.has("all-off");
}

// --- device ----------------------------------------------------------------------------

let deviceShape = "";

function renderDevice() {
  const server = S.server;
  const body = $("device-body");
  if (!server) return;
  const shape = JSON.stringify([
    server.connected, server.connecting, server.simulate_only, S.devices, S.busy.has("connect"),
    server.connected ? [server.device.simulated, server.device.scenes] : null,
  ]);
  if (shape !== deviceShape) {
    deviceShape = shape;
    clear(body);
    if (server.connected) buildConnected(body, server.device);
    else buildConnect(body, server);
  }
  if (server.connected) updateConnected(server.device);
}

function option(value, label, selected) {
  return h("option", { value, text: label, selected });
}

function buildConnect(body, server) {
  const busy = server.connecting || S.busy.has("connect");
  if (server.simulate_only) {
    put(body, h("p", { class: "note", text: "The viewer was started with --simulate: it opens the simulated device only." }));
  } else {
    const devices = S.devices;
    const ports = devices ? devices.serial : [];
    const cameras = devices ? devices.cameras : [];
    put(body, 
      h("label", { class: "check" }, h("input", { type: "checkbox", id: "connect-simulate" }), "Use the simulated device"),
      h("label", { class: "field" }, h("span", { class: "field-label", text: "Light source (serial port)" }),
        h("select", { class: "input", id: "connect-port" }, option("", ports.length === 1 ? `Automatic (${ports[0]})` : "Automatic"),
          ports.map((port) => option(port, port)))),
      h("label", { class: "field" }, h("span", { class: "field-label", text: "Camera" }),
        h("select", { class: "input", id: "connect-camera" },
          option("", devices && devices.matching_cameras.length === 1 ? `Automatic (${devices.matching_cameras[0]})` : "Automatic"),
          cameras.map((name) => option(name, name)))),
      h("div", { class: "field-row" },
        h("label", { class: "field" }, h("span", { class: "field-label", text: "Picture" }),
          h("select", { class: "input", id: "connect-size" },
            (devices ? devices.sizes : [[1280, 720]]).map(([w, hh]) => option(`${w}x${hh}`, `${w}×${hh}`, w === 1280)))),
        h("label", { class: "field" }, h("span", { class: "field-label", text: "Frames / s" }),
          h("input", { class: "input", id: "connect-fps", type: "number", min: 1, max: 60, step: 1, value: 10 }))),
      h("label", { class: "field" }, h("span", { class: "field-label", text: "Frames come as" }),
        h("select", { class: "input", id: "connect-source", title: "Uncompressed frames are the measurement path. MJPEG is faster (set 60 frames/s) but its values depend on the decoder." },
          option("uncompressed", "Uncompressed (for measuring)"), option("mjpeg", "MJPEG (faster, for looking)"))),
    );
    if (devices && devices.problems.length) {
      put(body, h("p", { class: "note warn", text: devices.problems.map((p) => p.message).join(" ") }));
    } else if (devices && !ports.length) {
      put(body, h("p", { class: "note warn", text: "No light-source serial port was found. It is on the USB1 socket of the device." }));
    }
  }
  put(body, h("div", { class: "row-actions" },
    h("button", { class: "button primary", type: "button", id: "connect-go", disabled: busy, text: busy ? "Connecting…" : "Connect", on: { click: connect } }),
    server.simulate_only ? null : h("button", { class: "button ghost", type: "button", disabled: S.busy.has("devices"), text: "Find devices", on: { click: findDevices } })));
  if (busy) put(body, h("p", { class: "note", text: "The first frame can take a few seconds. macOS may ask whether this program may use the camera." }));
}

function buildConnected(body, device) {
  const row = (label, id) => [h("dt", { text: label }), h("dd", { id })];
  put(body, h("dl", { class: "kv" },
    row("Unit", "dev-unit"), row("Camera", "dev-camera"), row("Serial port", "dev-port"),
    row("Light delay", "dev-latency"), row("Replies resent", "dev-loss"), row("Last check", "dev-check")));
  if (device.scenes.length) {
    put(body, h("div", { class: "field" }, h("span", { class: "field-label", text: "In front of the simulated camera" }),
      h("div", { class: "segment", id: "dev-scenes" }, device.scenes.map((name) =>
        h("button", { type: "button", data: { scene: name }, text: name === "white" ? "White board" : "Sample",
          on: { click: () => act("scene", "The scene was not changed", async () => applyState(await post("/api/scene", { name }))) } })))));
  }
  put(body, h("p", { class: "note warn", id: "dev-lock-note" }));
  put(body, h("div", { class: "row-actions" },
    h("button", { class: "button", type: "button", id: "dev-check-go", text: "Check", title: "Blink LED 6, measure the delay and the black level", on: { click: () => startJob("check", {}) } }),
    h("button", { class: "button", type: "button", id: "dev-tune-go", text: "Tune", title: "With the white board in view: measure the timing, search the PWM of every channel, record a white reference and build a calibration", on: { click: () => startJob("tune", scanOptions(false)) } }),
    h("button", { class: "button ghost", type: "button", id: "dev-disconnect", text: "Disconnect", on: { click: disconnect } })));
}

function updateConnected(device) {
  const set = (id, text, kind = "") => { const node = $(id); if (node) { node.textContent = text; node.className = kind; node.title = text; } };
  set("dev-unit", device.unit_id);
  set("dev-camera", device.camera ? device.camera.name : "–");
  set("dev-port", device.serial ? device.serial.port : "–");
  set("dev-latency", device.latency_ms ? `${fixed(device.latency_ms, 0)} ms` : "not measured");
  const loss = device.ack && device.ack.loss_rate;
  set("dev-loss", device.ack && device.ack.commands ? `${fixed(100 * loss, 1)} % of ${device.ack.commands}` : "–", loss > 0.15 ? "warn" : "");
  const check = device.check;
  if (check) {
    const good = check.paired && check.black_level.every((level) => level === check.expected_black_level);
    set("dev-check", `${check.paired ? "paired" : "not paired"} · black ${check.black_level.join(", ")}`, good ? "good" : "warn");
  } else {
    set("dev-check", "not run");
  }
  const note = $("dev-lock-note");
  if (note) note.textContent = device.locked ? "" : lockNote(device.lock_error);
  for (const button of document.querySelectorAll("#dev-scenes button")) {
    button.classList.toggle("on", button.dataset.scene === device.scene);
    button.disabled = jobRunning();
  }
  if ($("dev-check-go")) $("dev-check-go").disabled = jobRunning();
  if ($("dev-tune-go")) $("dev-tune-go").disabled = jobRunning();
}

async function findDevices() {
  await act("devices", "Devices could not be listed", async () => { S.devices = await get("/api/devices"); });
}

async function connect() {
  const value = (id) => ($(id) ? $(id).value : "");
  const body = {};
  if ($("connect-simulate") && $("connect-simulate").checked) body.simulate = true;
  if (value("connect-port")) body.port = value("connect-port");
  if (value("connect-camera")) body.camera = value("connect-camera");
  if (value("connect-size")) body.size = value("connect-size").split("x").map(Number);
  if (value("connect-fps")) body.fps = Number(value("connect-fps"));
  if (value("connect-source")) body.source = value("connect-source");
  await act("connect", "Could not connect", async () => {
    applyState(await post("/api/connect", body));
    toast("good", "Connected", S.server.device.simulated ? "The simulated device." : `Camera ${S.server.device.camera.name}.`);
  });
}

async function disconnect() {
  await act("disconnect", "Disconnecting failed", async () => applyState(await post("/api/disconnect")));
}

// --- LEDs ------------------------------------------------------------------------------

function buildLeds() {
  const list = clear($("led-list"));
  for (const led of S.server.leds) {
    const colour = wavelengthColour(led.nm);
    const swatch = h("span", { class: "led-swatch" });
    swatch.style.background = colour;
    swatch.style.color = colour;
    const tag = led.hazard ? h("span", { class: "led-tag uv", text: led.hazard, title: `${led.hazard}: injures eyes and skin` })
      : led.nm > 750 ? h("span", { class: "led-tag nir", text: "NIR", title: "Near infrared: invisible" }) : null;
    const pwm = h("input", { class: "input small led-pwm", type: "number", min: 1, max: 1023, step: 1, title: "PWM, 1-1023",
      on: { change: () => changePwm(led.led_id, pwm) } });
    const keep = h("button", { class: "led-keep", type: "button", text: "S", on: { click: () => keepPwm(led.led_id) } });
    const toggle = h("input", { type: "checkbox", on: { change: () => switchLed(led.led_id, toggle.checked) } });
    put(list, h("div", { class: "led-row", id: `led-${led.led_id}` },
      h("span", { class: "led-name" }, swatch, h("span", { class: "led-id", text: led.led_id }),
        h("span", { class: "led-nm", text: `${led.nm} nm` }), tag),
      pwm, keep, h("label", { class: "switch", title: `LED ${led.led_id}` }, toggle, h("span"))));
  }
}

function renderLeds() {
  if (!S.server) return;
  if (!$("led-list").children.length) buildLeds();
  const free = connected() && !jobRunning();
  let lit = 0;
  for (const led of S.server.leds) {
    const row = $(`led-${led.led_id}`);
    const [pwm, keep, label] = [row.children[1], row.children[2], row.children[3]];
    const toggle = label.firstChild;
    const on = led.state === "on";
    if (on) lit += 1;
    row.classList.toggle("lit", on);
    row.classList.toggle("unknown", led.state === "unknown");
    row.classList.toggle("locked", !led.allowed);
    row.title = !led.allowed ? "Ultraviolet: start the viewer with --allow-uv to permit this LED"
      : led.state === "unknown" ? "The device did not confirm this LED's state" : "";
    if (document.activeElement !== pwm) {
      const shown = S.manualPwm[led.led_id] ?? (on ? led.pwm : led.scan_pwm);
      if (Number(pwm.value) !== shown) pwm.value = shown;
    }
    const value = Number(pwm.value);
    pwm.disabled = !free || !led.allowed;
    toggle.checked = on;
    toggle.disabled = !free || !led.allowed || S.busy.has(`led-${led.led_id}`);
    keep.disabled = !free || !led.allowed || value === led.scan_pwm;
    keep.classList.toggle("differs", value !== led.scan_pwm);
    keep.title = value === led.scan_pwm
      ? `Scans use PWM ${led.scan_pwm} for this LED${led.scan_pwm !== led.default_pwm ? ` (the model's value is ${led.default_pwm})` : ""}`
      : `Use PWM ${value} for scans (now ${led.scan_pwm}). White references made before no longer match.`;
  }
  $("led-note").textContent = jobRunning() ? "busy" : lit ? `${lit} lit` : "one at a time";
}

async function switchLed(id, on) {
  const row = $(`led-${id}`);
  const pwm = Number(row.children[1].value);
  await act(`led-${id}`, `LED ${id}`, async () => applyState(await post("/api/led", on ? { led_id: id, on, pwm } : { led_id: id, on })));
  render();
}

async function changePwm(id, input) {
  const value = Math.round(Number(input.value));
  if (!(value >= 1 && value <= 1023)) { delete S.manualPwm[id]; render(); return; }
  S.manualPwm[id] = value;
  const led = ledOf(id);
  if (led && led.state === "on") {
    await act(`led-${id}`, `LED ${id}`, async () => applyState(await post("/api/led", { led_id: id, on: true, pwm: value })));
  }
  render();
}

async function keepPwm(id) {
  const value = Number($(`led-${id}`).children[1].value);
  await act(`keep-${id}`, `LED ${id}`, async () => {
    applyState(await post("/api/leds/scan-pwm", { led_id: id, pwm: value }));
    delete S.manualPwm[id];
    toast("info", `LED ${id} scans at PWM ${value}`, "Record a new white reference: earlier ones were made with another PWM.");
  });
}

// --- camera ----------------------------------------------------------------------------

async function loadCamera() {
  try { S.camera = await get("/api/camera"); }
  catch (error) { S.camera = { failed: error instanceof ApiError ? error.message : "The viewer program does not answer." }; }
  renderCamera(true);
}

function renderCamera(rebuild = false) {
  const body = $("camera-body");
  if (!connected()) {
    S.camera = null;
    S.cameraFor = null;
    put(clear(body), h("p", { class: "note", text: "Connect a device to see its camera parameters." }));
    return;
  }
  const key = S.server.device.unit_id + String(S.server.device.simulated);
  if (S.cameraFor !== key) { S.cameraFor = key; S.camera = null; loadCamera(); }
  const camera = S.camera;
  if (!camera) { if (!body.children.length || rebuild) put(clear(body), h("p", { class: "note", text: "Reading…" })); return; }
  if (camera.failed) {
    if (rebuild) {
      put(clear(body), h("p", { class: "note bad", text: `The camera parameters could not be read: ${camera.failed}` }),
        h("div", { class: "row-actions" }, h("button", { class: "button small", type: "button", text: "Try again", on: { click: loadCamera } })));
    }
    return;
  }
  if (rebuild || !$("camera-exposure")) {
    clear(body);
    if (!camera.available) {
      put(body, h("p", { class: "note warn", text: "Camera parameters cannot be set on this computer. The live picture works; scans need “Without a parameter lock”." }));
      return;
    }
    const exposure = camera.values["exposure-time-abs"];
    const gain = camera.values.gain;
    put(body, h("div", { class: "field-row" },
      h("label", { class: "field" }, h("span", { class: "field-label", text: "Exposure (ms)" }),
        h("input", { class: "input", id: "camera-exposure", type: "number", step: 0.1,
          min: exposure && exposure.minimum !== null ? exposure.minimum / 10 : 0.1,
          max: exposure && exposure.maximum !== null ? exposure.maximum / 10 : 500,
          value: exposure ? exposure.current / 10 : "",
          on: { change: (event) => setControl("exposure-time-abs", Math.round(Number(event.target.value) * 10)) } })),
      h("label", { class: "field" }, h("span", { class: "field-label", text: "Gain" }),
        h("input", { class: "input", id: "camera-gain", type: "number", step: gain && gain.step ? gain.step : 1,
          min: gain && gain.minimum !== null ? gain.minimum : 0, max: gain && gain.maximum !== null ? gain.maximum : 100,
          value: gain ? gain.current : "",
          on: { change: (event) => setControl("gain", Math.round(Number(event.target.value))) } }))),
      h("dl", { class: "kv" },
        h("dt", { text: "White balance" }), h("dd", { id: "camera-awb" }),
        h("dt", { text: "Parameters" }), h("dd", { id: "camera-locked" })),
      h("p", { class: "note", text: "Changing exposure or gain makes earlier white references unusable for new scans." }),
      h("div", { class: "row-actions" }, h("button", { class: "button small", id: "camera-lock", type: "button", text: "Lock again",
        on: { click: () => act("camera", "The parameters were not locked", async () => { S.camera = await post("/api/camera/lock"); renderCamera(true); }) } })));
  }
  const free = !jobRunning() && !S.busy.has("camera");
  $("camera-exposure").disabled = !free;
  $("camera-gain").disabled = !free;
  $("camera-lock").disabled = !free;
  const awb = camera.values["auto-white-balance-temp"];
  const temperature = camera.values["white-balance-temp"];
  $("camera-awb").textContent = awb && awb.current === 0 ? `fixed${temperature ? ` at ${temperature.current} K` : ""}` : "automatic";
  $("camera-awb").className = awb && awb.current === 0 ? "good" : "warn";
  $("camera-locked").textContent = camera.locked ? "locked" : "not locked";
  $("camera-locked").className = camera.locked ? "good" : "warn";
}

async function setControl(name, value) {
  await act("camera", "The camera parameter was not changed", async () => {
    try {
      S.camera = await post("/api/camera", { name, value });
      toast("info", "Camera parameter changed", "Record a new white reference before the next calibrated scan.");
    } finally {
      renderCamera(true);
    }
  });
  loadCamera();
}

// --- the stage: zoom, pan, overlay -------------------------------------------------------

const stage = $("stage");
const inner = $("stage-inner");
const picture = $("picture");
const overlay = $("overlay");
let drag = null;

function stageSize() {
  return [stage.clientWidth, stage.clientHeight];
}

function fitView() {
  const view = S.view;
  const [width, height] = stageSize();
  if (!view.width || !width) return;
  view.scale = Math.min(width / view.width, height / view.height);
  view.x = (width - view.width * view.scale) / 2;
  view.y = (height - view.height * view.scale) / 2;
  view.fit = true;
  applyView();
}

function zoomAt(factor, cx, cy) {
  const view = S.view;
  if (!view.width) return;
  const [width, height] = stageSize();
  const least = Math.min(width / view.width, height / view.height) * 0.5;
  const scale = Math.min(32, Math.max(least, view.scale * factor));
  const px = cx ?? width / 2, py = cy ?? height / 2;
  view.x = px - ((px - view.x) / view.scale) * scale;
  view.y = py - ((py - view.y) / view.scale) * scale;
  view.scale = scale;
  view.fit = false;
  applyView();
}

function applyView() {
  const view = S.view;
  inner.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  stage.classList.toggle("crisp", view.scale >= 2);
  $("status-zoom").textContent = view.width ? `${Math.round(view.scale * 100)} %` : "";
  $("status-size").textContent = view.width ? `${view.width}×${view.height}` : "";
  drawOverlay();
}

function setPictureSize(width, height) {
  const view = S.view;
  if (view.width === width && view.height === height) return;
  view.width = width;
  view.height = height;
  picture.width = width;
  picture.height = height;
  fitView();
}

picture.addEventListener("load", () => {
  if (picture.naturalWidth > 1) setPictureSize(picture.naturalWidth, picture.naturalHeight);
});
picture.addEventListener("error", () => {
  if (S.tab === "live" && S.streaming) { S.streaming = false; setTimeout(syncStream, 1000); }
});

function toPicture(event) {
  const box = stage.getBoundingClientRect();
  const view = S.view;
  return [Math.floor((event.clientX - box.left - view.x) / view.scale), Math.floor((event.clientY - box.top - view.y) / view.scale)];
}

const inside = (x, y) => x >= 0 && y >= 0 && x < S.view.width && y < S.view.height;

function drawOverlay() {
  const ratio = window.devicePixelRatio || 1;
  const [width, height] = stageSize();
  if (overlay.width !== Math.round(width * ratio) || overlay.height !== Math.round(height * ratio)) {
    overlay.width = Math.round(width * ratio);
    overlay.height = Math.round(height * ratio);
  }
  const context = overlay.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const view = S.view;
  if (!view.width) return;
  const sx = (x) => view.x + x * view.scale;
  const sy = (y) => view.y + y * view.scale;
  const shape = (geometry, colour, label, dashed) => {
    context.lineWidth = 1.5;
    context.strokeStyle = colour;
    context.fillStyle = colour;
    context.setLineDash(dashed ? [5, 4] : []);
    let lx, ly;
    if (geometry.rect) {
      const [x, y, w, hh] = geometry.rect;
      context.strokeRect(sx(x), sy(y), w * view.scale, hh * view.scale);
      lx = sx(x); ly = sy(y) - 4;
    } else {
      const [x, y] = geometry.point;
      const cx = sx(x + 0.5), cy = sy(y + 0.5);
      context.beginPath();
      context.moveTo(cx - 9, cy); context.lineTo(cx - 3, cy);
      context.moveTo(cx + 3, cy); context.lineTo(cx + 9, cy);
      context.moveTo(cx, cy - 9); context.lineTo(cx, cy - 3);
      context.moveTo(cx, cy + 3); context.lineTo(cx, cy + 9);
      context.stroke();
      lx = cx + 8; ly = cy - 8;
    }
    context.setLineDash([]);
    if (label) {
      context.font = "600 11px -apple-system, Segoe UI, sans-serif";
      context.lineWidth = 3;
      context.strokeStyle = "rgba(0, 0, 0, 0.7)";
      context.strokeText(label, lx, ly);
      context.fillText(label, lx, ly);
    }
  };
  if (S.tab === "live") {
    if (S.probe) shape(S.probe.w > 1 || S.probe.h > 1 ? { rect: [S.probe.x, S.probe.y, S.probe.w, S.probe.h] } : { point: [S.probe.x, S.probe.y] }, "#ffd166", "", false);
  } else {
    for (const region of S.regions) shape(region.geometry, region.colour, region.name, !region.pinned);
  }
  if (drag && drag.kind === "select" && drag.moved) {
    const [x0, y0, x1, y1] = [Math.min(drag.start[0], drag.now[0]), Math.min(drag.start[1], drag.now[1]),
      Math.max(drag.start[0], drag.now[0]), Math.max(drag.start[1], drag.now[1])];
    shape({ rect: [x0, y0, x1 - x0 + 1, y1 - y0 + 1] }, "#ffffff", "", true);
  }
}

stage.addEventListener("wheel", (event) => {
  event.preventDefault();
  const box = stage.getBoundingClientRect();
  zoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - box.left, event.clientY - box.top);
}, { passive: false });

stage.addEventListener("mousedown", (event) => {
  if (event.target.closest(".stage-tools")) return;
  if (!S.view.width) return;
  if (event.button === 1 || event.button === 2 || event.altKey || event.metaKey) {
    drag = { kind: "pan", from: [event.clientX, event.clientY], origin: [S.view.x, S.view.y] };
    stage.classList.add("panning");
  } else if (event.button === 0) {
    drag = { kind: "select", start: toPicture(event), now: toPicture(event), moved: false, shift: event.shiftKey };
  }
  event.preventDefault();
});

window.addEventListener("mousemove", (event) => {
  if (stage.contains(event.target) || drag) {
    const [x, y] = toPicture(event);
    $("status-cursor").textContent = inside(x, y) ? `x ${x}  y ${y}` : "";
  }
  if (!drag) return;
  if (drag.kind === "pan") {
    S.view.x = drag.origin[0] + event.clientX - drag.from[0];
    S.view.y = drag.origin[1] + event.clientY - drag.from[1];
    S.view.fit = false;
    applyView();
  } else {
    drag.now = toPicture(event);
    if (Math.abs(drag.now[0] - drag.start[0]) * S.view.scale > 4 || Math.abs(drag.now[1] - drag.start[1]) * S.view.scale > 4) drag.moved = true;
    drawOverlay();
  }
});

window.addEventListener("mouseup", () => {
  if (!drag) return;
  const done = drag;
  drag = null;
  stage.classList.remove("panning");
  if (done.kind !== "select") return;
  const clamp = (value, top) => Math.max(0, Math.min(top - 1, value));
  if (done.moved) {
    const x0 = clamp(Math.min(done.start[0], done.now[0]), S.view.width), x1 = clamp(Math.max(done.start[0], done.now[0]), S.view.width);
    const y0 = clamp(Math.min(done.start[1], done.now[1]), S.view.height), y1 = clamp(Math.max(done.start[1], done.now[1]), S.view.height);
    picked({ rect: [x0, y0, x1 - x0 + 1, y1 - y0 + 1] }, true);
  } else if (inside(done.start[0], done.start[1])) {
    picked({ point: done.start }, done.shift);
  }
  drawOverlay();
});

stage.addEventListener("contextmenu", (event) => event.preventDefault());
stage.addEventListener("dblclick", (event) => { if (!event.target.closest(".stage-tools")) fitView(); });
stage.addEventListener("mouseleave", () => { $("status-cursor").textContent = ""; });
new ResizeObserver(() => { if (S.view.fit) fitView(); else drawOverlay(); }).observe(stage);

$("zoom-in").addEventListener("click", () => zoomAt(1.5));
$("zoom-out").addEventListener("click", () => zoomAt(1 / 1.5));
$("zoom-fit").addEventListener("click", fitView);
$("zoom-one").addEventListener("click", () => zoomAt(1 / S.view.scale));

function picked(geometry, pin) {
  if (S.tab === "live") {
    S.probe = geometry.rect ? { x: geometry.rect[0], y: geometry.rect[1], w: geometry.rect[2], h: geometry.rect[3] }
      : { x: geometry.point[0], y: geometry.point[1], w: 1, h: 1 };
    pollLive();
    renderOptionsText();
    render();
  } else if (S.capture && S.capture.summary.mode === "scan") {
    addRegion(geometry, pin);
  }
}

// --- live ------------------------------------------------------------------------------

async function syncStream() {
  const wanted = S.tab === "live" && connected();
  if (wanted && !S.streaming) {
    S.streaming = true;
    try {
      const { ticket } = await post("/api/preview/ticket");
      if (S.tab === "live" && connected()) showPicture(`/api/preview.mjpg?ticket=${ticket}`);
      else S.streaming = false;
    } catch {
      S.streaming = false;
    }
  } else if (!wanted && S.streaming) {
    S.streaming = false;
    if (S.tab === "live") showPicture(BLANK);
  }
}

async function pollLive() {
  if (S.stopped || S.tab !== "live" || !connected()) return;
  try {
    const probe = S.probe;
    S.live = await get(`/api/live${probe ? query(probe) : ""}`);
    if (S.live.size) setPictureSize(S.live.size[0], S.live.size[1]);
  } catch (error) {
    if (error instanceof ApiError && error.code === "CONFIG_REJECTED") S.probe = null;
  }
  renderLive();
}

function stat(name, value, kind = "") {
  return h("div", { class: `stat ${kind}` }, h("div", { class: "stat-name", text: name }), h("div", { class: "stat-value", text: value }));
}

function renderLive() {
  const live = S.live;
  const canvas = $("histogram");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 320, height = canvas.clientHeight || 120;
  if (canvas.width !== Math.round(width * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const stats = clear($("live-stats"));
  if (!live || !live.size || !connected()) {
    $("histogram-note").textContent = "";
    return;
  }
  context.strokeStyle = "#1e232b";
  context.lineWidth = 1;
  for (const mark of [0.25, 0.5, 0.75]) {
    context.beginPath(); context.moveTo(mark * width + 0.5, 0); context.lineTo(mark * width + 0.5, height); context.stroke();
  }
  const top = Math.max(1, ...live.histogram.flat());
  const colours = ["rgba(255, 99, 88, 0.85)", "rgba(74, 222, 128, 0.85)", "rgba(96, 165, 250, 0.85)"];
  context.globalCompositeOperation = "lighter";
  live.histogram.forEach((bins, channel) => {
    context.beginPath();
    context.moveTo(0, height);
    bins.forEach((count, index) => {
      const x = (index / (bins.length - 1)) * width;
      context.lineTo(x, height - Math.sqrt(count / top) * (height - 4));
    });
    context.lineTo(width, height);
    context.closePath();
    context.fillStyle = colours[channel].replace("0.85", "0.28");
    context.fill();
    context.strokeStyle = colours[channel];
    context.lineWidth = 1.2;
    context.stroke();
  });
  context.globalCompositeOperation = "source-over";
  $("histogram-note").textContent = "code values 0–255";
  const clipped = live.saturated * 100;
  put(stats, 
    stat("Mean R", fixed(live.mean[0]), "r"), stat("Mean G", fixed(live.mean[1]), "g"), stat("Mean B", fixed(live.mean[2]), "b"),
    stat("Max", String(Math.max(...live.max)), Math.max(...live.max) >= 255 ? "warn" : ""),
    stat("Clipped", `${fixed(clipped, clipped < 1 ? 2 : 1)} %`, `span-2 ${clipped > 1 ? "bad" : clipped > 0 ? "warn" : ""}`),
    stat("Sharpness", fixed(live.sharpness, 2), "span-2"));
  stats.lastChild.title = "Mean local contrast of the picture centre: turn the lens until it is highest";
  if (live.probe) {
    const p = live.probe;
    put(stats, stat(p.pixels > 1 ? `Region ${p.w}×${p.h} at ${p.x}, ${p.y}` : `Pixel ${p.x}, ${p.y}`,
      `R ${fixed(p.rgb[0])}   G ${fixed(p.rgb[1])}   B ${fixed(p.rgb[2])}   max ${Math.max(...p.max)}`, "span-4"));
  }
}

// --- captures --------------------------------------------------------------------------

async function loadLibrary() {
  try {
    const [captures, calibrations] = await Promise.all([get("/api/captures"), get("/api/calibrations")]);
    S.captures = captures.captures;
    S.calibrations = calibrations.calibrations;
    S.revision = captures.revision;
  } catch (error) {
    fail(error, "The data folder could not be read");
  }
  renderLibrary();
  renderToolbar();
}

function renderLibrary() {
  const strip = $("library-strip");
  $("library-count").textContent = S.captures.length ? `${S.captures.length}` : "";
  const wanted = S.captures.map((entry) => `${entry.id}:${entry.status}:${entry.finished_at_utc}`).join("|");
  if (strip.dataset.shown !== wanted) {
    strip.dataset.shown = wanted;
    clear(strip);
    if (!S.captures.length) put(strip, h("div", { class: "library-empty", text: "Nothing recorded yet. Connect a device and press Scan." }));
    for (const entry of S.captures) {
      const tags = [];
      if (entry.role === "white") tags.push(h("span", { class: "tag white", text: "white" }));
      if (entry.mode === "dark" || entry.role === "dark") tags.push(h("span", { class: "tag dark", text: "dark" }));
      else if (entry.mode === "single") tags.push(h("span", { class: "tag single", text: "frame" }));
      if (entry.status === "damaged") tags.push(h("span", { class: "tag bad", text: "damaged" }));
      else if (entry.status !== "complete") tags.push(h("span", { class: "tag warn", text: entry.failure ? "failed" : "partial" }));
      if (entry.simulated) tags.push(h("span", { class: "tag sim", text: "sim" }));
      const holder = h("div", { class: "shot-missing", text: entry.status === "damaged" ? "unreadable" : "…" });
      const card = h("button", { class: "shot", type: "button", data: { id: entry.id }, title: entry.id, on: { click: () => openCapture(entry.id) } },
        holder, h("div", { class: "shot-tags" }, tags),
        h("div", { class: "shot-label" }, h("span", { class: "shot-name", text: entry.id }), h("span", { class: "shot-time", text: localTime(entry.started_at_utc) })));
      put(strip, card);
      if (entry.status !== "damaged" && (entry.outputs || entry.mode !== "scan")) {
        pictureUrl(`/api/captures/${encodeURIComponent(entry.id)}/thumbnail.png`).then((url) => {
          const release = () => URL.revokeObjectURL(url);
          holder.replaceWith(h("img", { src: url, alt: "", on: { load: release, error: release } }));
        }).catch(() => { holder.textContent = "no picture"; });
      } else {
        holder.textContent = entry.status === "damaged" ? "unreadable" : "no complete scan";
      }
    }
  }
  for (const card of strip.querySelectorAll(".shot")) {
    card.classList.toggle("on", Boolean(S.capture) && S.tab === "capture" && card.dataset.id === S.capture.id);
  }
}

const DEFAULT_VIEW = { mode: "rgb", band: null, palette: "auto", range: "standard", low: "", high: "", gamma: 1, overlay: false, force: false };

async function openCapture(id) {
  try {
    const summary = await get(`/api/captures/${encodeURIComponent(id)}`);
    const previous = S.capture;
    const capture = { id, summary, layer: null, ...DEFAULT_VIEW, calibration: null, error: null };
    if (summary.mode === "scan") {
      capture.mode = summary.views.rgb ? "rgb" : "band";
      capture.band = summary.bands.length ? summary.bands[0].band_id : null;
      if (previous && previous.summary.mode === "scan") {
        if (previous.mode !== "band" ? summary.views[previous.mode] !== false : true) capture.mode = previous.mode;
        if (summary.bands.some((band) => band.band_id === previous.band)) capture.band = previous.band;
        Object.assign(capture, { palette: previous.palette, gamma: previous.gamma, overlay: previous.overlay, range: previous.range, low: previous.low, high: previous.high });
      }
      if (capture.mode === "rgb" && !summary.views.rgb) capture.mode = "band";
      // The calibration used last, else the newest one; dropped quietly if it does not fit.
      const newest = S.calibrations.length ? S.calibrations[0].id : null;
      capture.calibration = summary.role === "white" ? null : (S.preferredCalibration || newest);
    }
    S.capture = capture;
    enterTab("capture");
    await loadLayer(true);
  } catch (error) {
    fail(error, `“${id}” could not be opened`);
  }
  render();
}

// The bands of the capture as it is shown: a calibration that covers fewer bands than
// were scanned leaves the others out.
function shownBands(capture) {
  const all = capture.summary.bands;
  if (!capture.layer || !capture.layer.bands || !capture.layer.bands.length) return all;
  const kept = new Set(capture.layer.bands.map((band) => band.band_id));
  return all.filter((band) => kept.has(band.band_id));
}

const shownViews = (capture) => (capture.layer && capture.layer.views) || capture.summary.views;

// Reads what the capture is once calibrated. A calibration that does not fit is dropped
// (quietly when it was only the preferred one), and the raw data is shown instead.
async function loadLayer(quiet = false) {
  const capture = S.capture;
  if (!capture) return;
  capture.error = null;
  const path = `/api/captures/${encodeURIComponent(capture.id)}/layer`;
  try {
    capture.layer = await get(path + query({ calibration: capture.calibration, force: capture.force }));
  } catch (error) {
    if (capture.calibration && error instanceof ApiError) {
      const refused = capture.calibration;
      capture.calibration = null;
      capture.force = false;
      if (!quiet) {
        toast("error", "The calibration does not fit this capture", error.message, {
          code: error.code,
          hint: error.details.differing ? `Different: ${error.details.differing.join(", ")}` : error.hint,
          actions: error.code === "CALIBRATION_MISMATCH" ? [{ label: "Apply anyway (not comparable)", run: () => {
            capture.calibration = refused; capture.force = true; loadLayer().then(render);
          } }] : null,
        });
      }
      try { capture.layer = await get(path); } catch (inner) { capture.error = inner; }
    } else {
      capture.error = error;
    }
  }
  const calibrated = capture.layer && capture.layer.layer === "L2";
  const possible = shownViews(capture);
  if ((["ndvi", "ndwi", "pca", "heat"].includes(capture.mode) && !calibrated) || possible[capture.mode] === false) {
    capture.mode = possible.rgb ? "rgb" : "band";
  }
  const bands = shownBands(capture);
  if (bands.length && !bands.some((band) => band.band_id === capture.band)) capture.band = bands[0].band_id;
  refreshRegions();
  await showCapture();
}

let pictureTurn = 0;

async function showCapture() {
  const capture = S.capture;
  if (!capture || S.tab !== "capture") return;
  const turn = ++pictureTurn;
  renderStageText();
  if (capture.error) { showPicture(BLANK); return; }
  const mode = capture.mode === "band" ? `band:${capture.band}` : capture.mode;
  const path = `/api/captures/${encodeURIComponent(capture.id)}/view.png` + query({
    mode, calibration: capture.calibration, force: capture.force,
    ...displayRange(capture),
    gamma: capture.gamma !== 1 ? capture.gamma : null,
    palette: capture.palette !== "auto" ? capture.palette : null, overlay: capture.overlay,
  });
  capture.loading = true;
  renderStageText();
  loadScale(capture, mode, turn);
  try {
    const url = await pictureUrl(path);
    if (turn !== pictureTurn) { URL.revokeObjectURL(url); return; }
    showPicture(url);
    capture.viewError = null;
  } catch (error) {
    if (turn !== pictureTurn) return;
    capture.viewError = error;
    showPicture(BLANK);
  }
  capture.loading = false;
  renderStageText();
}

// The colour scale of a view drawn through a colour table: what its two ends stand for.
async function loadScale(capture, mode, turn) {
  capture.scale = null;
  if (capture.summary.mode !== "scan" || ["rgb", "cir", "uv", "pca"].includes(capture.mode)) { renderScale(); return; }
  try {
    const scale = await get(`/api/captures/${encodeURIComponent(capture.id)}/scale` + query({
      mode, calibration: capture.calibration, force: capture.force,
      ...displayRange(capture),
      palette: capture.palette !== "auto" ? capture.palette : null,
    }));
    if (turn !== pictureTurn) return;
    capture.scale = scale;
    if (scale) capture.scaleBar = await paletteUrl(scale.palette);
  } catch {
    capture.scale = null;
  }
  if (turn === pictureTurn) renderScale();
}

function renderScale() {
  const capture = S.capture;
  const scale = S.tab === "capture" && capture && !capture.error && !capture.viewError ? capture.scale : null;
  $("stage-scale").hidden = !scale;
  if (!scale) return;
  const digits = scale.unit === "image_code_value" ? 1 : 3;
  $("scale-bar").src = capture.scaleBar;
  $("scale-low").textContent = fixed(scale.low, digits);
  $("scale-high").textContent = fixed(scale.high, digits);
  const unit = { index: "index", relative_to_white: "relative to white", image_code_value: "code value" }[scale.unit] || scale.unit;
  $("scale-unit").textContent = capture.gamma !== 1 ? `${unit} · γ ${fixed(capture.gamma, 2)}` : unit;
}

function renderStageText() {
  renderScale();
  const badge = clear($("stage-badge"));
  const message = clear($("stage-message"));
  const hint = $("status-hint");
  if (S.tab === "live") {
    hint.textContent = "Drag: region · click: pixel · wheel: zoom · right-drag: move · double-click: fit";
    if (!connected()) {
      put(message, h("strong", { text: S.offline ? "The viewer program does not answer" : "No device connected" }),
        h("span", { text: S.offline ? "Start vispek-hc-viewer again and reload this page." : "Connect a device on the left to see its live picture." }));
      return;
    }
    put(badge, h("span", { class: "badge live", text: "● LIVE" }));
    if (S.server.device.simulated) put(badge, h("span", { class: "badge", text: "SIMULATED" }));
    if (S.server.preview.clipping) put(badge, h("span", { class: "badge", text: "CLIPPING IN MAGENTA" }));
    if (S.server.device.stale) put(message, h("strong", { class: "bad", text: "No frames are arriving" }), h("span", { text: "Is the camera cable in? Is another program showing the camera?" }));
    return;
  }
  hint.textContent = "Click: spectrum of a pixel · drag: region · shift-click: keep the pixel · ← →: bands";
  const capture = S.capture;
  if (!capture) return;
  const failure = capture.error || capture.viewError;
  if (failure) {
    put(message, h("strong", { class: "bad", text: failure.message || "This capture cannot be shown" }),
      failure.hint ? h("span", { text: failure.hint }) : null);
    return;
  }
  if (capture.loading) put(message, h("span", { text: "Rendering…" }));
  if (capture.summary.mode !== "scan") {
    put(badge, h("span", { class: "badge", text: capture.summary.mode === "dark" || capture.summary.role === "dark" ? "DARK FRAME" : "SINGLE FRAME" }));
  } else if (capture.layer) {
    put(badge, capture.layer.layer === "L2"
      ? h("span", { class: "badge calibrated", text: "CALIBRATED · relative to white" })
      : h("span", { class: "badge raw", text: "RAW · code values, not comparable" }));
    if (capture.layer.forced) put(badge, h("span", { class: "badge forced", text: "FORCED · settings differ" }));
  }
  if (capture.summary.simulated) put(badge, h("span", { class: "badge", text: "SIMULATED" }));
}

// --- toolbar ---------------------------------------------------------------------------

const VIEW_BUTTONS = [
  ["rgb", "RGB", "630 / 560 / 455 nm"], ["cir", "CIR", "850 / 667 / 560 nm"], ["uv", "UV", "400 / 370 / 275 nm"],
  ["band", "Band", "One band"], ["ndvi", "NDVI", "(804 − 667) / (804 + 667)"], ["ndwi", "NDWI", "(850 − 973) / (850 + 973)"],
  ["pca", "PCA", "First three principal components"], ["heat", "Mean", "Mean of the valid bands"],
];

let toolbarShape = "";

function renderToolbar() {
  const bar = $("toolbar");
  $("tab-live").classList.toggle("on", S.tab === "live");
  $("tab-capture").classList.toggle("on", S.tab === "capture");
  $("tab-capture").disabled = !S.capture;
  $("tab-capture").textContent = S.capture ? S.capture.id : "Capture";
  const capture = S.capture;
  const shape = S.tab === "live"
    ? JSON.stringify(["live", connected(), Boolean(S.probe), S.server && S.server.preview.clipping])
    : JSON.stringify(["capture", capture && [capture.id, capture.mode, capture.band, capture.calibration, capture.force, capture.palette,
      capture.range, capture.overlay, capture.layer && capture.layer.layer], S.calibrations.map((entry) => entry.id), S.busy.has("export")]);
  if (shape === toolbarShape) return;
  toolbarShape = shape;
  clear(bar);
  if (S.tab === "live") {
    const clipping = h("input", { type: "checkbox", checked: Boolean(S.server && S.server.preview.clipping), disabled: !connected(),
      on: { change: () => act("clipping", "The option was not changed", async () => applyState(await post("/api/preview/options", { clipping: clipping.checked }))) } });
    put(bar, h("label", { class: "check", title: "Draw every pixel that is at 255 in magenta" }, clipping, "Show clipping"),
      h("span", { class: "spacer" }),
      S.probe ? h("button", { class: "button ghost small", type: "button", text: "Clear the region", on: { click: () => { S.probe = null; drawOverlay(); renderOptionsText(); pollLive(); toolbarShape = ""; renderToolbar(); } } }) : null);
    return;
  }
  if (!capture) return;
  if (capture.summary.mode !== "scan") {
    put(bar, h("span", { class: "tool-label", text: "A single frame as the camera delivered it." }));
    return;
  }
  const calibrated = capture.layer && capture.layer.layer === "L2";
  const views = shownViews(capture);
  const scanned = capture.summary.views;
  const modes = h("div", { class: "segment" }, VIEW_BUTTONS.map(([mode, label, title]) => {
    let why = "";
    if (mode in views && !views[mode] && scanned[mode]) why = `The chosen calibration has no white reference for a band this view needs (${title}). Choose “None (raw)” as calibration to see it.`;
    else if (mode in views && !views[mode]) why = `This capture was scanned without a band this view needs (${title}). Choose those LEDs in “Channels & options” and scan again.`;
    else if (["ndvi", "ndwi", "pca", "heat"].includes(mode) && !calibrated) why = "Needs a calibration: on raw data it would only show how hard each LED was driven";
    else if (["pca", "heat"].includes(mode) && shownBands(capture).length < 3) why = "Needs at least three bands";
    return h("button", { type: "button", class: capture.mode === mode ? "on" : "", text: label, title: why || title, disabled: Boolean(why),
      on: { click: () => changeView({ mode }) } });
  }));
  put(bar, modes);
  if (capture.mode === "band") {
    put(bar, h("div", { class: "tool-group" },
      h("button", { class: "icon-button", type: "button", text: "◀", title: "Previous band (←)", on: { click: () => stepBand(-1) } }),
      h("select", { class: "input", on: { change: (event) => changeView({ band: event.target.value }) } },
        shownBands(capture).map((band) => option(band.band_id, `LED ${band.led_id} · ${band.nm} nm`, band.band_id === capture.band))),
      h("button", { class: "icon-button", type: "button", text: "▶", title: "Next band (→)", on: { click: () => stepBand(1) } })));
  }
  put(bar, h("div", { class: "tool-group" }, h("span", { class: "tool-label", text: "Calibration" }),
    h("select", { class: "input", title: "The white reference the values are divided by",
      on: { change: (event) => { capture.calibration = event.target.value || null; capture.force = false; S.preferredCalibration = capture.calibration; loadLayer().then(render); } } },
      option("", "None (raw)", !capture.calibration),
      S.calibrations.map((entry) => option(entry.id, entry.id, entry.id === capture.calibration)))));
  if (!["rgb", "cir", "uv", "pca"].includes(capture.mode)) {
    put(bar, h("div", { class: "tool-group" }, h("span", { class: "tool-label", text: "Colours" }),
      h("select", { class: "input", on: { change: (event) => changeView({ palette: event.target.value }) } },
        [["auto", "Automatic"], ["gray", "Grey"], ["viridis", "Viridis"], ["spectral", "Spectral"]].map(([value, label]) => option(value, label, value === capture.palette)))));
  }
  put(bar, h("div", { class: "tool-group" }, h("span", { class: "tool-label", text: "Range" }),
    h("select", { class: "input", title: "Standard: 0 to 1 for calibrated pictures (white is white, and captures can be compared); for raw data the 1st to 99th percentile. Auto: always the 1st to 99th percentile.",
      on: { change: (event) => changeView({ range: event.target.value }) } },
      [["standard", "Standard"], ["auto", "Auto (1–99 %)"], ["manual", "Manual"]].map(([value, label]) => option(value, label, value === capture.range))),
    capture.range !== "manual" ? null : h("input", { class: "input", type: "number", step: "any", placeholder: "low", value: capture.low, title: "Value shown as black",
      on: { change: (event) => changeView({ low: event.target.value }) } }),
    capture.range !== "manual" ? null : h("input", { class: "input", type: "number", step: "any", placeholder: "high", value: capture.high, title: "Value shown as white",
      on: { change: (event) => changeView({ high: event.target.value }) } })));
  const gammaLabel = h("span", { class: "tool-label num", text: `γ ${fixed(capture.gamma, 2)}` });
  put(bar, h("div", { class: "tool-group" }, gammaLabel,
    h("input", { type: "range", min: 0.3, max: 3, step: 0.05, value: capture.gamma, title: "Gamma (double-click: 1.0)",
      on: { input: (event) => { gammaLabel.textContent = `γ ${fixed(Number(event.target.value), 2)}`; },
        change: (event) => changeView({ gamma: Number(event.target.value) }),
        dblclick: (event) => { event.target.value = 1; changeView({ gamma: 1 }); toolbarShape = ""; renderToolbar(); } } })));
  const marks = h("input", { type: "checkbox", checked: capture.overlay, on: { change: () => changeView({ overlay: marks.checked }) } });
  put(bar, h("label", { class: "check", title: "Mark clipped and invalid pixels of the bands this view uses" }, marks, "Mark bad pixels"));
  put(bar, h("span", { class: "spacer" }),
    h("select", { class: "input", disabled: S.busy.has("export"), title: "Write this capture into the exports folder of the data folder",
      on: { change: (event) => { const kind = event.target.value; event.target.value = ""; if (kind) exportCapture(kind); } } },
      option("", "Export…", true), option("envi", "ENVI (data.bsq + header)"), option("tiff", "TIFF, one page per band"), option("npz", "cube.npz for numpy")));
}

// The values shown as black and white. "standard" is 0 to 1 for calibrated pictures and
// single bands, where 1 is the white reference; indices keep their own fixed scale and
// raw data is stretched from its 1st to its 99th percentile.
function displayRange(capture) {
  if (capture.range === "manual") return { low: capture.low, high: capture.high };
  const calibrated = capture.layer && capture.layer.layer === "L2";
  if (capture.range === "standard" && calibrated && ["rgb", "cir", "uv", "band", "heat"].includes(capture.mode)) return { low: 0, high: 1 };
  return { low: null, high: null };
}

function changeView(change) {
  Object.assign(S.capture, change);
  render();
  showCapture();
}

function stepBand(step) {
  const capture = S.capture;
  if (!capture || capture.summary.mode !== "scan" || !capture.summary.bands.length) return;
  const bands = shownBands(capture);
  const index = bands.findIndex((band) => band.band_id === capture.band);
  changeView({ mode: "band", band: bands[(Math.max(0, index) + step + bands.length) % bands.length].band_id });
}

async function exportCapture(kind) {
  const capture = S.capture;
  await act("export", "The export failed", async () => {
    const made = await post(`/api/captures/${encodeURIComponent(capture.id)}/export`, { format: kind, calibration: capture.calibration, force: capture.force });
    toast("good", "Exported", `${made.files.length} file${made.files.length === 1 ? "" : "s"} in ${made.directory} of the data folder (layer ${made.layer}).`);
  });
}

// --- regions and spectra ---------------------------------------------------------------

function addRegion(geometry, pin) {
  const pinned = S.regions.filter((region) => region.pinned);
  if (pin && pinned.length >= 8) { toast("warning", "Eight regions are kept at most", "Remove one first."); return; }
  S.regions = S.regions.filter((region) => region.pinned);
  S.regionCount += pin ? 1 : 0;
  const used = new Set(S.regions.map((region) => region.colour));
  const colour = pin ? (REGION_COLOURS.find((candidate) => !used.has(candidate)) || REGION_COLOURS[0]) : "#ffffff";
  const region = { id: `${Date.now()}-${Math.random()}`, name: pin ? `R${S.regionCount}` : "cursor", colour, geometry, pinned: pin, data: null };
  S.regions.push(region);
  loadSpectrum(region);
  render();
}

async function loadSpectrum(region) {
  const capture = S.capture;
  if (!capture || capture.summary.mode !== "scan" || capture.error) return;
  const g = region.geometry;
  const where = g.rect ? { x: g.rect[0], y: g.rect[1], w: g.rect[2], h: g.rect[3] } : { x: g.point[0], y: g.point[1] };
  try {
    const data = await get(`/api/captures/${encodeURIComponent(capture.id)}/spectrum` + query({ ...where, name: region.name, calibration: capture.calibration, force: capture.force }));
    if (S.capture !== capture) return;
    region.data = data;
    region.error = null;
  } catch (error) {
    region.data = null;
    region.error = error.message;
  }
  renderSpectrum();
}

function refreshRegions() {
  for (const region of S.regions) { region.data = null; loadSpectrum(region); }
}

function niceStep(span, target) {
  const rough = span / target;
  const power = Math.pow(10, Math.floor(Math.log10(rough)));
  const unit = rough / power;
  return (unit < 1.5 ? 1 : unit < 3.5 ? 2 : unit < 7.5 ? 5 : 10) * power;
}

function renderSpectrum() {
  const holder = $("spectrum-chart");
  const list = clear($("region-list"));
  const actions = clear($("region-actions"));
  const capture = S.capture;
  const drawn = S.regions.filter((region) => region.data);
  $("spectrum-note").textContent = capture && capture.layer && capture.layer.layer === "L2" ? "relative to white" : "code values";
  clear(holder);
  if (!capture || capture.summary.mode !== "scan") {
    put(holder, h("div", { class: "chart-empty", text: "Spectra need a scan." }));
    return;
  }
  if (!S.regions.length) {
    put(holder, h("div", { class: "chart-empty", text: "Click the picture for the spectrum of a pixel. Drag for a region." }));
    return;
  }
  const W = 320, H = 200, left = 38, right = 8, top = 10, bottom = 24;
  const chart = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  const bands = drawn.length ? drawn[0].data.bands : [];
  const wavelengths = bands.map((band) => band.center_nm);
  const low = wavelengths.length ? Math.min(...wavelengths) : 400, high = wavelengths.length ? Math.max(...wavelengths) : 1050;
  const padded = Math.max(20, (high - low) * 0.04);
  const x = (nm) => left + ((nm - (low - padded)) / (high - low + 2 * padded)) * (W - left - right);
  let peak = 0;
  for (const region of drawn) for (const band of region.data.bands) if (band.mean !== null) peak = Math.max(peak, band.mean + (band.std || 0));
  const calibrated = capture.layer && capture.layer.layer === "L2";
  const ceiling = calibrated ? Math.max(1.05, peak * 1.08) : Math.max(32, Math.min(255, peak * 1.15));
  const y = (value) => top + (1 - Math.max(0, Math.min(ceiling, value)) / ceiling) * (H - top - bottom);
  const stepY = niceStep(ceiling, 4);
  for (let value = 0; value <= ceiling + 1e-9; value += stepY) {
    chart.append(s("line", { x1: left, x2: W - right, y1: y(value), y2: y(value), stroke: "#1e232b", "stroke-width": 1 }),
      s("text", { x: left - 5, y: y(value) + 3.5, fill: "#8e98a8", "font-size": 9.5, "text-anchor": "end", "font-family": "ui-monospace, Menlo, monospace", text: calibrated ? value.toFixed(stepY < 0.1 ? 2 : 1) : String(Math.round(value)) }));
  }
  if (calibrated) chart.append(s("line", { x1: left, x2: W - right, y1: y(1), y2: y(1), stroke: "#3a4250", "stroke-width": 1, "stroke-dasharray": "3 3" }));
  for (let nm = Math.ceil((low - padded) / 100) * 100; nm <= high + padded; nm += 100) {
    chart.append(s("text", { x: x(nm), y: H - 7, fill: "#8e98a8", "font-size": 9.5, "text-anchor": "middle", "font-family": "ui-monospace, Menlo, monospace", text: String(nm) }));
  }
  for (const nm of wavelengths) {
    chart.append(s("line", { x1: x(nm), x2: x(nm), y1: H - bottom, y2: H - bottom + 4, stroke: wavelengthColour(nm), "stroke-width": 2 }));
  }
  chart.append(s("line", { x1: left, x2: W - right, y1: H - bottom, y2: H - bottom, stroke: "#3a4250", "stroke-width": 1 }));
  for (const region of drawn) {
    const points = region.data.bands.filter((band) => band.mean !== null);
    if (!points.length) continue;
    if (region.geometry.rect) {
      const upper = points.map((band) => `${x(band.center_nm)},${y(band.mean + band.std)}`);
      const lower = points.map((band) => `${x(band.center_nm)},${y(band.mean - band.std)}`).reverse();
      chart.append(s("polygon", { points: [...upper, ...lower].join(" "), fill: region.colour, "fill-opacity": 0.14 }));
    }
    chart.append(s("polyline", { points: points.map((band) => `${x(band.center_nm)},${y(band.mean)}`).join(" "), fill: "none", stroke: region.colour,
      "stroke-width": 1.6, "stroke-linejoin": "round", "stroke-dasharray": region.pinned ? null : "4 3" }));
    for (const band of points) chart.append(s("circle", { cx: x(band.center_nm), cy: y(band.mean), r: 2.3, fill: region.colour }));
  }
  const cursor = s("line", { y1: top, y2: H - bottom, stroke: "#5f6877", "stroke-width": 1, visibility: "hidden" });
  chart.append(cursor);
  put(holder, chart);
  const tip = h("div", { class: "chart-tip", hidden: true });
  put(holder, tip);
  chart.addEventListener("mousemove", (event) => {
    if (!bands.length) return;
    const box = chart.getBoundingClientRect();
    const at = ((event.clientX - box.left) / box.width) * W;
    let nearest = 0;
    bands.forEach((band, index) => { if (Math.abs(x(band.center_nm) - at) < Math.abs(x(bands[nearest].center_nm) - at)) nearest = index; });
    const band = bands[nearest];
    cursor.setAttribute("x1", x(band.center_nm));
    cursor.setAttribute("x2", x(band.center_nm));
    cursor.setAttribute("visibility", "visible");
    put(clear(tip), h("div", { text: `LED ${Number(band.band_id.slice(4))} · ${band.center_nm} nm` }),
      drawn.map((region) => {
        const value = region.data.bands[nearest];
        const line = h("div", { text: `${region.name}  ${value.mean === null ? "no valid pixel" : fixed(value.mean, calibrated ? 3 : 1)}${region.geometry.rect && value.std !== null ? ` ± ${fixed(value.std, calibrated ? 3 : 1)}` : ""}` });
        line.style.color = region.colour;
        return line;
      }));
    tip.hidden = false;
    const tx = (x(band.center_nm) / W) * box.width;
    tip.style.left = `${Math.min(box.width - 150, Math.max(0, tx + 10))}px`;
    tip.style.top = "6px";
  });
  chart.addEventListener("mouseleave", () => { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); });

  for (const region of S.regions) {
    const dot = h("span", { class: "region-dot" });
    dot.style.background = region.colour;
    const g = region.geometry;
    put(list, h("div", { class: "region-row" }, dot,
      h("span", { class: "region-name", text: region.error ? `${region.name}: ${region.error}` : region.name }),
      h("span", { class: "region-where", text: g.rect ? `${g.rect[2]}×${g.rect[3]} at ${g.rect[0]}, ${g.rect[1]}` : `${g.point[0]}, ${g.point[1]}` }),
      h("button", { class: "region-remove", type: "button", title: "Remove", text: "×", on: { click: () => { S.regions = S.regions.filter((other) => other !== region); render(); } } })));
  }
  const cursorRegion = S.regions.find((region) => !region.pinned);
  put(actions, 
    cursorRegion ? h("button", { class: "button small", type: "button", text: "Keep the cursor", title: "Keep this spectrum while you pick others (shift-click does the same)",
      on: { click: () => { S.regions = S.regions.filter((region) => region !== cursorRegion); addRegion(cursorRegion.geometry, true); } } }) : null,
    h("button", { class: "button small", type: "button", text: "Save as CSV", disabled: !drawn.length, on: { click: saveCsv } }),
    h("button", { class: "button ghost small", type: "button", text: "Clear", on: { click: () => { S.regions = []; S.regionCount = 0; render(); } } }));
}

async function saveCsv() {
  const capture = S.capture;
  await act("csv", "The spectra were not saved", async () => {
    const blob = await post(`/api/captures/${encodeURIComponent(capture.id)}/spectra.csv`, {
      calibration: capture.calibration, force: capture.force,
      regions: S.regions.map((region) => ({ name: region.name, ...region.geometry })),
    });
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: `${capture.id}-spectra.csv` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
}

// --- bands and capture facts -----------------------------------------------------------

function renderBands() {
  const body = clear($("bands-body"));
  const capture = S.capture;
  if (!capture || capture.summary.mode !== "scan" || !capture.summary.bands.length) {
    put(body, h("p", { class: "note", text: "No bands." }));
    return;
  }
  const kept = new Set(shownBands(capture).map((band) => band.band_id));
  const rows = capture.summary.bands.map((band) => {
    const swatch = h("span", { class: "led-swatch" });
    swatch.style.background = wavelengthColour(band.nm);
    const clipped = band.saturated_fraction === null || band.saturated_fraction === undefined ? null : band.saturated_fraction * 100;
    const shown = kept.has(band.band_id);
    return h("tr", { class: shown ? `pick ${capture.mode === "band" && capture.band === band.band_id ? "on" : ""}` : "left-out",
      title: shown ? "Show this band" : "The chosen calibration has no white reference for this band. Choose “None (raw)” to see it.",
      on: shown ? { click: () => changeView({ mode: "band", band: band.band_id }) } : {} },
      h("td", null, swatch, `${band.led_id} · ${band.nm} nm`), h("td", { text: band.pwm ?? "–" }),
      h("td", { text: fixed(band.response_dn, 1), class: band.response_dn !== null && band.response_dn < 10 ? "warn" : "" }),
      h("td", { text: clipped === null ? "–" : `${fixed(clipped, clipped && clipped < 0.1 ? 3 : 1)}`, class: clipped ? "warn" : "" }));
  });
  put(body, h("table", { class: "band-table" },
    h("thead", null, h("tr", null, h("th", { text: "Band" }), h("th", { text: "PWM" }),
      h("th", { text: "Signal", title: "How far the LED's own camera channel rose above the dark frame" }),
      h("th", { text: "Clipped %", title: "Share of pixels at 255 in any colour channel" }))),
    h("tbody", null, rows)));
}

function renderInfo() {
  const body = clear($("info-body"));
  const capture = S.capture;
  if (!capture) { put(body, h("p", { class: "note", text: "Pick a capture below." })); return; }
  const c = capture.summary;
  const rows = [];
  const add = (label, value, kind) => { if (value !== null && value !== undefined && value !== "") rows.push(h("dt", { text: label }), h("dd", { text: String(value), class: kind || "", title: String(value) })); };
  add("Name", c.id);
  add("Recorded", localTime(c.started_at_utc));
  add("Kind", c.role === "white" ? "white reference" : c.mode === "scan" ? "scan" : c.role === "dark" ? "dark frame" : c.mode);
  add("Status", c.status, c.status === "complete" ? "good" : "warn");
  add("Picture", c.size ? `${c.size[0]}×${c.size[1]}` : null);
  add("Exposure", c.exposure !== null && c.exposure !== undefined ? `${fixed(c.exposure / 10, 1)} ms` : null);
  add("Gain", c.gain);
  add("Parameters", c.controls_locked === true ? "locked: fixed while it was recorded" : c.controls_locked === false ? "not locked: the camera may have adjusted itself" : null, c.controls_locked ? "good" : "warn");
  add("Scans averaged", c.average > 1 ? c.average : null);
  add("Value per band", c.reduction);
  add("Unit", c.unit_id);
  add("Setup", c.setup_label);
  add("Enclosure", c.enclosure && c.enclosure !== "unknown" ? c.enclosure : null);
  put(body, h("dl", { class: "kv" }, rows));
  if (c.failure) put(body, h("p", { class: "note bad", text: `${c.failure.code}: ${c.failure.message}` }));
  const warnings = capture.layer ? capture.layer.warnings : [];
  for (const warning of warnings) put(body, h("p", { class: "note warn", text: warning }));
  const clippedLeds = (c.clipped && c.clipped.led_ids) || [];
  if (clippedLeds.length) {
    put(body, h("p", { class: "note warn", text: `Clipped: LED ${clippedLeds.join(", ")} (${clippedLeds.length} of ${c.bands.length} bands have more than ${c.clipped.limit * 100} % of their pixels at the top of the range). `
      + (c.role === "white" ? "No calibration can be built from it: record it again with “Search the PWM of every channel first” (Channels & options)."
        : "Those pixels have no value in these bands. Record the white reference with “Search the PWM of every channel first” (Channels & options) and scan again.") }));
  }
  if (c.role === "white" && c.status === "complete" && !clippedLeds.length && !S.calibrations.some((entry) => entry.inputs.includes(c.id))) {
    put(body, h("div", { class: "row-actions" }, h("button", { class: "button small", type: "button", text: "Build a calibration from it", disabled: S.busy.has("calibration"),
      on: { click: () => act("calibration", "No calibration was built", async () => {
        const made = await post("/api/calibrations", { whites: [c.id], name: c.id });
        S.preferredCalibration = made.id;
        await loadLibrary();
        toast("good", "Calibration built", `“${made.id}” is now used for scans that fit it.`);
      }) } })));
  }
}

// --- recording -------------------------------------------------------------------------

const jobName = (kind) => ({ scan: "Scan", white: "White reference", tune: "Tuning", single: "Frame", dark: "Dark frame", check: "Check" }[kind] || kind);

function scanOptions(white) {
  const options = S.options;
  const body = { average: Number(options.average) || 1, reduction: options.reduction, enclosure: options.enclosure };
  if (options.channels) body.channels = options.channels;
  if (options.label) body.setup_label = options.label;
  if (options.unlocked) body.unlocked = true;
  if (white) {
    body.auto_pwm = Boolean(options.autoPwm);
    body.calibrate = Boolean(options.calibrate);
    const roi = whiteRoi();
    if (options.whiteRoi && roi) body.white_roi = roi;
  }
  return body;
}

function whiteRoi() {
  const probe = S.probe;
  const live = S.live;
  if (!probe || !live || !live.size || (probe.w < 2 && probe.h < 2)) return null;
  const [width, height] = live.size;
  const round = (value) => Math.round(value * 1e6) / 1e6;
  return [round(probe.x / width), round(probe.y / height), round(probe.w / width), round(probe.h / height)];
}

async function startJob(kind, body) {
  const name = $("capture-name").value.trim();
  const sent = { kind, ...body };
  if (name && kind !== "check") sent.name = name;
  await act("job", `${jobName(kind)} was not started`, async () => {
    const job = await post("/api/jobs", sent);
    $("capture-name").value = "";
    S.server.job = job;
    S.seenJob = `${job.id}:running`;
    if (kind !== "check" && S.tab !== "live") setTab("live");
  });
  tick(true);
}

function renderAcquire() {
  const free = connected() && !jobRunning() && !S.busy.has("job");
  for (const id of ["do-scan", "do-white", "do-single", "do-dark"]) $(id).disabled = !free;
  $("capture-name").disabled = !connected();
  const job = S.server && S.server.job;
  const progress = $("progress");
  if (job && job.state === "running") {
    progress.hidden = false;
    const bar = $("progress-bar");
    const known = job.bands > 0 && job.phase === "Scanning";
    bar.classList.toggle("indeterminate", !known);
    if (known) bar.style.width = `${Math.round((100 * job.band) / job.bands)}%`;
    $("progress-text").textContent = job.stopping ? "Stopping…"
      : `${jobName(job.kind)} · ${job.phase || "starting"}${known ? ` · ${job.band} of ${job.bands} bands done` : ""}`;
    $("do-stop").disabled = job.stopping;
  } else {
    progress.hidden = true;
  }
}

function jobChanged(job) {
  if (job.state === "running") return;
  const title = jobName(job.kind);
  const warnings = job.warnings.join(". ");
  if (job.state === "done") {
    if (job.kind === "check") {
      const check = S.server.device && S.server.device.check;
      toast(warnings ? "warning" : "good", "Check finished", warnings || (check ? `Paired; light shows after ${fixed(check.latency_ms, 0)} ms; black level ${check.black_level.join(", ")}.` : ""));
    } else {
      toast(warnings ? "warning" : "good", `${title} finished`, warnings || (job.calibration ? `Saved as “${job.capture}”; calibration “${job.calibration}” built.` : `Saved as “${job.capture}”.`));
    }
  } else if (job.state === "cancelled") {
    toast("warning", `${title} stopped`, job.capture ? "What was recorded so far is kept." : "");
  } else {
    const error = job.error || {};
    if (job.kind === "white" && error.code === "CALIBRATION_QUALITY") {
      const bands = (error.details && error.details.bands) || [];
      const failing = bands.filter((band) => !band.pass);
      const bright = failing.filter((band) => band.saturated_fraction > 0);
      const searched = Boolean(S.options.autoPwm);
      toast("error", `${title}: no calibration was built`,
        bright.length ? `${bright.length} of ${bands.length} bands are too bright: the LEDs are driven too hard for this board and distance.`
          : `${failing.length} of ${bands.length} bands are too dark or too uneven to divide by.`,
        { code: error.code, sticky: true,
          hint: searched ? "The PWM was searched already. Move the board, light it more evenly, or judge only a region of it (Channels & options)."
            : "Searching the PWM finds a drive level for every channel, then records the white board again. The scan made now is kept as it is.",
          actions: searched ? null : [{ label: "Search the PWM and record again", run: () => startJob("white", { ...scanOptions(true), auto_pwm: true, calibrate: true }) }] });
    } else {
      toast("error", `${title} failed`, error.message || "", { code: error.code, hint: [error.hint, warnings].filter(Boolean).join(" ") });
    }
  }
  if (job.calibration) S.preferredCalibration = job.calibration;
  loadLibrary().then(() => {
    if (job.capture && job.state !== "failed" && S.captures.some((entry) => entry.id === job.capture && (entry.outputs || entry.mode !== "scan"))) openCapture(job.capture);
  });
  if (job.kind === "white" || job.kind === "tune") loadCamera();
}

// --- options dialog --------------------------------------------------------------------

function openOptions() {
  const grid = clear($("channel-grid"));
  const chosen = new Set(S.options.channels || (S.server ? S.server.leds.filter((led) => !led.hazard).map((led) => led.led_id) : []));
  for (const led of S.server ? S.server.leds : []) {
    const box = h("input", { type: "checkbox", checked: chosen.has(led.led_id) && led.allowed, disabled: !led.allowed, data: { led: led.led_id } });
    put(grid, h("label", { class: `channel ${led.allowed ? "" : "locked"}`, title: led.allowed ? `LED ${led.led_id}` : "Ultraviolet: start the viewer with --allow-uv" }, box, `${led.nm}`));
  }
  $("option-average").value = S.options.average;
  $("option-reduction").value = S.options.reduction;
  $("option-enclosure").value = S.options.enclosure;
  $("option-label").value = S.options.label;
  $("option-auto-pwm").checked = S.options.autoPwm;
  $("option-calibrate").checked = S.options.calibrate;
  $("option-white-roi").checked = S.options.whiteRoi;
  $("option-unlocked").checked = S.options.unlocked;
  renderOptionsText();
  $("options-dialog").showModal();
}

function renderOptionsText() {
  const roi = whiteRoi();
  $("white-roi-text").textContent = roi ? `(${S.probe.w}×${S.probe.h} at ${S.probe.x}, ${S.probe.y})` : "(drag a rectangle on the live picture first)";
}

function setChannels(pick) {
  for (const box of $("channel-grid").querySelectorAll("input")) if (!box.disabled) box.checked = pick(Number(box.dataset.led));
}

function readOptions() {
  const boxes = [...$("channel-grid").querySelectorAll("input")];
  const picked = boxes.filter((box) => box.checked).map((box) => Number(box.dataset.led));
  const standard = boxes.filter((box) => !box.disabled && Number(box.dataset.led) >= 5).map((box) => Number(box.dataset.led));
  const same = picked.length === standard.length && picked.every((id, index) => id === standard[index]);
  S.options = {
    channels: same || !picked.length ? null : picked,
    average: Math.max(1, Math.min(64, Math.round(Number($("option-average").value) || 1))),
    reduction: $("option-reduction").value,
    enclosure: $("option-enclosure").value,
    label: $("option-label").value.trim(),
    autoPwm: $("option-auto-pwm").checked,
    calibrate: $("option-calibrate").checked,
    whiteRoi: $("option-white-roi").checked,
    unlocked: $("option-unlocked").checked,
  };
  saveOptions();
  render();
}

// --- everything together ---------------------------------------------------------------

function render() {
  renderTopbar();
  renderDevice();
  renderLeds();
  renderCamera();
  renderToolbar();
  renderStageText();
  renderAcquire();
  renderLibrary();
  const live = S.tab === "live";
  $("live-card").hidden = !live;
  $("spectrum-card").hidden = live;
  $("bands-card").hidden = live;
  $("info-card").hidden = live;
  if (live) renderLive();
  else { renderSpectrum(); renderBands(); renderInfo(); }
  const options = S.options;
  const changed = options.channels || options.average > 1 || options.label || options.autoPwm || options.whiteRoi || options.unlocked || options.reduction !== "dominant";
  $("options-toggle").textContent = changed ? "Channels & options •" : "Channels & options";
  drawOverlay();
}

// Leaves the picture of the other tab behind: the stage starts empty and fits anew.
function enterTab(tab) {
  if (S.tab !== tab || tab === "live") {
    S.view.width = 0;
    S.view.height = 0;
    showPicture(BLANK);
  }
  S.tab = tab;
  S.streaming = false;
  toolbarShape = "";
}

function setTab(tab) {
  if (tab === "capture" && !S.capture) return;
  enterTab(tab);
  render();
  if (tab === "live") { syncStream(); pollLive(); } else { showCapture(); }
}

function applyState(server) {
  const before = S.server;
  S.server = server;
  S.offline = false;
  if (!before || before.connected !== server.connected) {
    if (!server.connected) { S.live = null; S.probe = null; S.manualPwm = {}; }
    if (S.tab === "live") { S.view.width = 0; S.view.height = 0; if (!server.connected) showPicture(BLANK); S.streaming = false; }
    toolbarShape = "";
  }
  // The camera card shows what was read once: read it again when the lock changed.
  if (before && before.device && server.device && before.device.locked !== server.device.locked) loadCamera();
  const job = server.job;
  const seen = job ? `${job.id}:${job.state}` : null;
  if (S.seenJob === null && before === null) S.seenJob = seen;        // what was there when the page opened
  else if (seen !== S.seenJob) { S.seenJob = seen; if (job) jobChanged(job); }
  const newest = server.notices.length ? server.notices[server.notices.length - 1].id : 0;
  if (S.seenNotice === null) S.seenNotice = newest;
  for (const notice of server.notices) {
    if (notice.id > S.seenNotice) toast(notice.level === "error" ? "error" : "warning", notice.level === "error" ? "Attention" : "Notice", notice.text, { sticky: notice.level === "error" });
  }
  S.seenNotice = Math.max(S.seenNotice, newest);
  if (server.library_revision !== S.revision) { S.revision = server.library_revision; loadLibrary(); }
  render();
  syncStream();
}

let timer = null;
async function tick(now = false) {
  clearTimeout(timer);
  if (S.stopped) return;
  try {
    applyState(await get("/api/state"));
  } catch (error) {
    if (!(error instanceof ApiError)) { S.offline = true; S.streaming = false; if (S.server) S.server.connected = false; render(); }
  }
  if (!S.stopped) timer = setTimeout(tick, jobRunning() || (S.server && S.server.connecting) ? 300 : 1000);
}

function start() {
  if (!token) {
    curtain("Open the address that vispek-hc-viewer printed when it started: it carries the token this page needs.");
    return;
  }
  $("tab-live").addEventListener("click", () => setTab("live"));
  $("tab-capture").addEventListener("click", () => setTab("capture"));
  $("uv-chip").addEventListener("click", () => {
    if (!S.server) return;
    if (S.server.allow_uv) toast("warning", "Ultraviolet is enabled", "LEDs 1–4 (255–370 nm) can be lit. UV-C injures eyes and skin: keep the box closed.", { hint: "To lock them again, end the viewer program and start it without --allow-uv." });
    else toast("info", "Ultraviolet is locked", "LEDs 1–4 (255–370 nm) can be lit only when the viewer program was started with --allow-uv. This page has no switch for it, on purpose: UV-C injures eyes and skin.", { hint: "End the program (Ctrl-C in its terminal) and start it again:  vispek-hc-viewer --allow-uv" });
  });
  $("all-off").addEventListener("click", () => act("all-off", "Not every LED could be switched off", async () => applyState(await post("/api/leds/off"))));
  $("do-scan").addEventListener("click", () => startJob("scan", scanOptions(false)));
  $("do-white").addEventListener("click", () => startJob("white", scanOptions(true)));
  $("do-single").addEventListener("click", () => startJob("single", S.options.label ? { setup_label: S.options.label, enclosure: S.options.enclosure } : { enclosure: S.options.enclosure }));
  $("do-dark").addEventListener("click", () => startJob("dark", S.options.label ? { setup_label: S.options.label, enclosure: S.options.enclosure } : { enclosure: S.options.enclosure }));
  $("do-stop").addEventListener("click", () => act("stop", "The job was not stopped", async () => applyState(await post("/api/jobs/stop"))));
  $("library-refresh").addEventListener("click", loadLibrary);
  $("options-toggle").addEventListener("click", openOptions);
  $("options-dialog").addEventListener("close", readOptions);
  $("channels-default").addEventListener("click", () => setChannels((id) => id >= 5));
  $("channels-visible").addEventListener("click", () => setChannels((id) => id >= 5 && id <= 11));
  $("channels-nir").addEventListener("click", () => setChannels((id) => id >= 12));
  window.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement && document.activeElement.tagName);
    if (typing || $("options-dialog").open) return;
    if (S.tab === "capture" && event.key === "ArrowRight") { stepBand(1); event.preventDefault(); }
    else if (S.tab === "capture" && event.key === "ArrowLeft") { stepBand(-1); event.preventDefault(); }
    else if (event.key === "f") fitView();
    else if (event.key === "Escape" && S.tab === "live" && S.probe) { S.probe = null; toolbarShape = ""; render(); }
  });
  // A hidden page keeps asking (browsers slow it down): an LED lit by hand stays lit
  // only while some page is in contact.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(true); });
  setInterval(pollLive, 400);
  showPicture(BLANK);
  loadLibrary();
  tick(true).then(() => { if (S.server && !S.server.connected && !S.server.simulate_only) findDevices(); });
}

start();
