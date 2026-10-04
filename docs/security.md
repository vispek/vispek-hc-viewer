# Security

The viewer drives hardware that can emit UV-C, from a web page. This is what stands
between that hardware and anything else that runs on, or reaches, the computer.

## What it defends against

| Threat | Defence |
|---|---|
| Another computer on the network | The server binds 127.0.0.1. No option changes that. |
| Another user or program on this computer that does not know the token | Every `/api/` request needs `Authorization: Bearer <token>`. The token is 32 random bytes, made at start, compared in constant time. |
| A web page in your browser that sends requests to `127.0.0.1` | It cannot set the `Authorization` header across origins, and it does not know the token. Requests carrying another site's `Origin` are refused; no CORS header is ever sent, so no preflight succeeds. |
| A web page that makes its own host name resolve to 127.0.0.1 (DNS rebinding) | Requests whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` are refused. |
| The token leaking through history, logs or the `Referer` | It is delivered in the fragment of the address, which browsers do not send; the page removes it from the address bar at once and keeps it in the tab's session storage. It is never accepted in a query string and never logged. `Referrer-Policy: no-referrer` is set. |
| The live stream, which an `<img>` must open without a header | A ticket that works once and for 30 s; it is not logged. |
| Text from a capture or a device name running as code in the page | `Content-Security-Policy: default-src 'self'` with no inline code; the page builds its content with `textContent`, never `innerHTML`. `X-Content-Type-Options: nosniff`. |
| A request that names a file outside the data folder | Captures and calibrations are addressed by a folder name of letters, digits, `.`, `_`, `-`. No route takes a path. Links are not followed. The SDK refuses frame names that leave a capture folder. |
| Lighting an ultraviolet LED through the API | Refused by the server unless the program was started with `--allow-uv`. The page cannot switch that on. |
| An LED left lit by a closed page | Switched off after 30 s without contact: no request with the token and no frame delivered to an open live stream. A page that is open but hidden keeps contact, more slowly; browsers may slow a page hidden for minutes so far that the LED goes off. |

## What it does not defend against

- **Someone who has the token.** They can do everything the page can. The token is
  printed in the terminal and is in the address of the first load: treat both as secret
  while the viewer runs. A new token is made at every start.
- **Programs running as you.** They can read the terminal, the process memory, the data
  folder, and the serial port itself.
- **A process that is killed.** `kill -9` or a power cut leaves the LEDs as they were.
  UV LEDs lit by hand are switched off by the SDK after 10 s only while the process
  lives. Unplug every cable of the device, the camera cable too, to be sure: the light
  source draws power through the camera cable as well.
- **Plain HTTP.** The connection is not encrypted; it never leaves the computer.

## Reporting

A way to light an LED without permission, to leave one lit, or to reach the API without
the token is a vulnerability. Report it privately to the maintainers, not in a public
issue.
