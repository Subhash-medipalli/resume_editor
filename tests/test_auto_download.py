"""The page saves the verified Word file itself, once, and only for a run that passed.

There is no browser in the test suite, so these read the page source. They pin the
three ways this feature goes wrong: offering a file for a failed run, navigating
away from the result view, and downloading from somewhere other than a fresh run.
"""

import re

from resume_tailor import server

HTML = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
SCRIPT = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
RUN = SCRIPT[SCRIPT.index("async function run()"):]
HISTORY = SCRIPT[SCRIPT.index("function loadHistory"):SCRIPT.index("function selectSaved")]


def _guard():
    guard = re.search(r"const (\w+) = data\.ok && DOWNLOAD_RE\.test\(data\.download\);", RUN)
    assert guard, "guard must be: data.ok && DOWNLOAD_RE.test(data.download)"
    return guard[1]


def _saved():
    saved = re.search(rf"const (\w+) = {_guard()} && typeof data\.saved_to === \"string\" && data\.saved_to;", RUN)
    assert saved, "a server-saved copy only counts for a run that passed the same guard"
    return saved[1]


def _auto_block():
    block = re.search(rf"if \({_guard()} && !{_saved()}\) \{{([^{{}}]*)\}}", RUN)
    assert block, "the automatic download must sit directly under the ok + route guard, unless the server saved a copy"
    return block


def test_download_route_pattern_is_the_strict_one_shared_with_history():
    assert r"const DOWNLOAD_RE = /^\/api\/download\/[0-9a-f]{32}$/;" in SCRIPT
    assert "DOWNLOAD_RE.test(run.download)" in HISTORY


def test_automatic_download_needs_an_ok_run_and_a_download_route():
    block = _auto_block()
    assert ".click()" in block[1]
    assert RUN.count(".click()") == 1  # never a second path in run()
    assert RUN.index(block[0]) > RUN.index('show("result")')  # after the result is painted


def test_nothing_but_the_guarded_block_in_run_ever_downloads():
    assert ".click()" not in HISTORY
    # Cut the one guarded download out; what is left (page load, history, saved-resume
    # selection, handlers) may only click the file picker and never builds a download link.
    rest = SCRIPT.replace(_auto_block()[0], "")
    assert rest.count(".click()") == 1 and '$("file").click()' in rest
    assert 'createElement("a")' not in rest.replace('const link = document.createElement("a");', "")  # a visible history link


def test_automatic_download_uses_an_anchor_and_cannot_navigate_the_page():
    block = _auto_block()[1]
    assert 'createElement("a")' in block and re.search(r"\.download\s*=", block)
    assert "a.href = data.download" in block  # saves this run's file, not some other URL
    assert ".remove()" in block  # the temporary link does not linger
    assert not re.search(r"\blocation\b|window\.open|iframe", SCRIPT, re.I)


def test_status_line_is_hidden_for_failed_runs_and_reset_for_new_ones():
    assert re.search(r'<p[^>]*id="dlnote"[^>]*\bhidden\b', HTML)
    assert RUN.index('$("dlnote").hidden = true') < RUN.index('await fetch("/api/tailor"')
    assert f'$("dlnote").hidden = !{_guard()};' in RUN


def test_fallback_button_is_shown_and_pointed_at_the_download():
    assert 'id="dl"' in HTML
    assert '$("dl").hidden = !data.download' in RUN
    assert '$("dl").href = data.download' in RUN


def test_a_server_saved_copy_skips_the_browser_download_and_says_where_it_went():
    # The browser's save dialog is what a script or an AI driving the page cannot answer.
    assert RUN.count("saved_to") == 3  # the guard, the guard's value, the note
    assert '"Saved to " + data.saved_to' in RUN
    assert RUN.index('$("dlnote").textContent') < RUN.index('$("dlnote").hidden = !')
