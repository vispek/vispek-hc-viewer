# Provenance

The code in this repository is an independent implementation. It was written from the
functional specification of the `vispek-hc` project (which describes a viewer in one
paragraph: a local server, a token, the routes, a three-column page), from the public
interface of the `vispek-hc` SDK, and from public technical references. No source code,
screenshots or documentation of any earlier or third-party camera software were consulted
while writing it, and none is included here. The layout, the wording and every name in
the page were chosen for this program.

Public references:

- Python standard library documentation (`http.server`, `secrets`, `hmac`, `threading`):
  <https://docs.python.org/3/>
- NumPy and Pillow documentation.
- MDN Web Docs for the web platform (Fetch, Canvas, SVG, `<dialog>`, Content Security
  Policy, `multipart/x-mixed-replace` in `<img>`): <https://developer.mozilla.org/>
- OWASP guidance on DNS rebinding and cross-site request forgery.

`tests/test_provenance.py` fails the build if identifiers known to belong to an unrelated
earlier implementation appear in the sources or documents.
