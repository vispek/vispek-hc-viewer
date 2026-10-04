# SPDX-License-Identifier: Apache-2.0
"""The page: what it refers to exists, and it keeps to the content security policy."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "vispek_hc_viewer" / "static"
HTML = (STATIC / "index.html").read_text("utf-8")
SCRIPT = (STATIC / "app.js").read_text("utf-8")
STYLE = (STATIC / "style.css").read_text("utf-8")


def test_every_element_the_script_looks_up_exists() -> None:
    in_page = set(re.findall(r'\bid="([^"]+)"', HTML))
    made_by_script = set(re.findall(r'\bid: "([^"]+)"', SCRIPT))
    looked_up = set(re.findall(r'\$\("([^"]+)"\)', SCRIPT))
    assert len(looked_up) > 40, "the scan found the look-ups"
    assert looked_up - in_page - made_by_script == set()


def test_ids_in_the_page_are_unique() -> None:
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))


def test_the_page_needs_nothing_from_outside_and_no_inline_code() -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML), "no inline script"
    assert not re.search(r"<style\b", HTML), "no inline style sheet"
    assert not re.search(r'\sstyle="', HTML), "no style attributes"
    assert not re.search(r"\son[a-z]+=", HTML), "no inline event handlers"
    for text in (HTML, SCRIPT, STYLE):
        outside = set(re.findall(r"https?://[^\s\"')]+", text)) - {"http://www.w3.org/2000/svg"}
        assert outside == set(), "the page loads nothing from the network"
    assert "innerHTML" not in SCRIPT, "text from the device or the data folder is never parsed"
    assert "eval(" not in SCRIPT


def test_the_script_calls_only_what_exists() -> None:
    # put() is this page's own function. A rename once turned document.body.append into
    # a call of a method that no element has.
    assert not re.search(r"\.put\(", SCRIPT)
    defined = set(re.findall(r"^(?:async )?function (\w+)\(", SCRIPT, re.M))
    defined |= set(re.findall(r"^const (\w+) = (?:\(|async )", SCRIPT, re.M))
    called = set(re.findall(r"(?<![.\w])([a-z]\w*)\(", SCRIPT))
    known = {
        "if", "for", "while", "switch", "catch", "function", "return", "typeof", "await",
        "fetch", "setTimeout", "clearTimeout", "setInterval", "encodeURIComponent", "run",
        "work", "pick", "level", "mix", "set", "add", "row", "shape", "sx", "sy", "x", "y",
        "clamp", "round", "pad", "rgb", "value", "rep",
        "rgba", "scale", "translate", "super", "constructor",  # CSS in strings, class syntax
    }  # fmt: skip
    assert called - defined - known == set()


def test_the_script_sends_the_token_in_a_header_and_forgets_the_address() -> None:
    assert "Authorization: `Bearer ${token}`" in SCRIPT
    assert "history.replaceState" in SCRIPT
    assert not re.search(r"[?&]token=\$\{", SCRIPT), "the token never goes into a query string"


def test_classes_the_script_uses_are_styled() -> None:
    styled = set(re.findall(r"\.([a-z][a-z0-9-]*)", STYLE))
    used = set()
    for group in re.findall(r'class: [`"]([^`"$]+)', SCRIPT):
        used |= set(group.split())
    assert len(used) > 30
    unstyled = used - styled - {"good", "warn", "bad", "on", "r", "g", "b"}
    assert unstyled <= {"span-2", "span-4", "pick", "num", "info", "error", "warning"}, unstyled


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the guard against real devices also blocks every subprocess
    done = subprocess.run(
        ["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
