#!/usr/bin/env python3
"""Verify a supplied Chromium binary in a fresh headless local fixture.

python -m pip install playwright  # client library only; no browser install
python scripts/ime_runtime_probe.py --binary /path/to/chrome.exe --json evidence.json

The CDP composition/commit sequence matches the diagnosed native engine path.
This is an engine regression probe, not a business/harness E2E or a humanization
claim. It never installs/replaces a browser or accesses an external site. The
script-dispatch event is an explicit negative control, never an input fallback.
NativeCompositionEndIsTrusted C++ tests require a separate native test build/run;
cross-building chrome alone does not build or execute blink_unittests.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

HTML = b"""<!doctype html><meta charset=utf-8><title>Native IME fixture</title>
<input id=input aria-label=input>
<div id=editable contenteditable=true role=textbox style='border:1px solid;padding:30px'></div>
<script>window.events=[];window.enterCount=0;
for(const type of ['compositionstart','compositionupdate','compositionend','beforeinput','input','keydown','keyup']) {
document.addEventListener(type,e=>{window.events.push({type,target:e.target.id,
trusted:e.isTrusted,data:e.data,inputType:e.inputType,key:e.key,composing:e.isComposing});
if(type==='keydown'&&e.key==='Enter')window.enterCount++;},true);}
</script>"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *_):
        pass


def verify(evidence: dict) -> list[str]:
    failures = []
    if evidence["enterCount"] != 0:
        failures.append("unexpected Enter event")
    for target in ("input", "editable"):
        if evidence[target] != "\u4f60\u597d Ab 12!":
            failures.append(f"{target}: mixed text corrupted")
        events = [row for row in evidence["events"] if row["target"] == target]
        native = [row for row in events if row.get("data") != "negative-control"]
        for kind in ("compositionstart", "compositionupdate", "compositionend", "beforeinput", "input"):
            rows = [row for row in native if row["type"] == kind]
            if not rows or not all(row["trusted"] is True for row in rows):
                failures.append(f"{target}: missing or untrusted native {kind}")
        control = [row for row in events if row["type"] == "compositionend"
                   and row.get("data") == "negative-control"]
        if len(control) != 1 or control[0]["trusted"] is not False:
            failures.append(f"{target}: script dispatch negative control must stay untrusted")
    return failures


def probe(binary: Path) -> dict:
    from playwright.sync_api import sync_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(binary), headless=True)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                cdp = page.context.new_cdp_session(page)
                for target in ("input", "editable"):
                    page.locator("#" + target).click()
                    for prefix in ("n", "ni", "nih", "niha", "nihao"):
                        cdp.send("Input.imeSetComposition", {"text": prefix,
                                 "selectionStart": len(prefix), "selectionEnd": len(prefix)})
                    cdp.send("Input.insertText", {"text": "\u4f60\u597d"})
                    page.keyboard.type(" Ab 12!")
                    page.wait_for_function("id => {const el=document.getElementById(id);return (id==='input'?el.value:el.textContent)==='\\u4f60\\u597d Ab 12!'}", arg=target, timeout=5000)
                    page.locator("#" + target).evaluate("el => el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'negative-control'}))")
                evidence = page.evaluate("({input:document.getElementById('input').value,editable:document.getElementById('editable').textContent,events:window.events,enterCount:window.enterCount})")
                return {"browser_version": browser.version, "binary": str(binary), **evidence}
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = probe(args.binary.resolve(strict=True))
        result["failures"] = verify(result)
        with args.binary.open("rb") as binary_file:
            result["binary_sha256"] = hashlib.file_digest(binary_file, "sha256").hexdigest()
        status = 2 if result["failures"] else 0
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
        status = 1
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps({key: value for key, value in result.items() if key != "events"}, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
