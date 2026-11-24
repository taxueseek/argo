"""Exercise the production DOM probe with an offline, disposable Chrome profile."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import zhihu_answer_body as reader

URL = 'https://www.zhihu.com/question/100/answer/200'
OTHER = 'https://www.zhihu.com/question/100/answer/201'
BODY = '<p>第一段</p><p>最后一段</p>'


def card(url=URL, body=BODY, section='', extra='', button='收起', style=''):
    return f'''<article class="AnswerItem"><meta itemprop="url" content="{url}">
        <div class="RichContent {section}"><div class="RichContent-inner" style="{style}">
        <span itemprop="text" class="RichText ztext">{body}</span></div>{extra}
        <button class="ContentItem-rightButton">{button}</button></div></article>'''


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    from contextlib import ExitStack

    # Only rendered-DOM cases need a runnable browser. Keep assertion and
    # evaluation failures outside the optional dependency check.
    with ExitStack() as stack:
        try:
            cdp = stack.enter_context(reader.ChromeCDP(user_data_dir=str(tmp_path_factory.mktemp('chrome'))))
        except FileNotFoundError:
            pytest.skip('Chrome/Edge is not installed')
        except RuntimeError as exc:
            if not str(exc).startswith('Chrome CDP failed to start on port '):
                raise
            pytest.skip('headless Chrome did not expose a local CDP endpoint')
        yield cdp


@pytest.mark.parametrize(('markup', 'route', 'expected'), [
    (card(OTHER, '他人正文') + card(), URL, '第一段\n\n最后一段'),
    (card(OTHER), URL, ''),
    (card(), OTHER, ''),
    (card() + card(), URL, ''),
    (card(section='is-collapsed') + card(OTHER), URL, ''),
    (card(button='阅读全文'), URL, ''),
    (card(extra='<div class="PaidMask">试读</div>'), URL, ''),
    (card(extra='<button>购买后继续阅读</button>'), URL, ''),
    (card(style='height:10px;overflow:hidden'), URL, ''),
    (card() + '<div role="dialog">登录验证</div>', URL, ''),
])
def test_rendered_answer(browser, markup, route, expected):
    browser.evaluate(f'document.body.innerHTML = {json.dumps(markup)}; true')
    actual = browser.evaluate(f'({reader._INSPECT})({json.dumps(URL)}, {json.dumps(route)})')
    assert actual == expected


@pytest.mark.parametrize('phase', ['startup', 'evaluate', 'shutdown'])
def test_timeout_keeps_ownership_until_cleanup(monkeypatch, phase):
    from threading import Event
    from time import monotonic

    release, entered = Event(), Event()
    starts = []

    def block(name):
        if name == phase:
            entered.set()
            release.wait(3)

    class SlowBrowser:
        def __init__(self, **kwargs):
            starts.append(kwargs)
            block('startup')

        def __enter__(self):
            return self

        def navigate(self, *args, **kwargs):
            pass

        def evaluate(self, _):
            block('evaluate')
            return 'body'

        def __exit__(self, *args):
            block('shutdown')

    monkeypatch.setattr(reader, 'ChromeCDP', SlowBrowser)
    monkeypatch.setattr(reader, 'auth_profile_for_fetch', lambda _: 'fixture')
    start = monotonic()
    try:
        assert reader.read_body('100', '200', 0.1) == ''
        assert monotonic() - start < 1
        assert entered.is_set()
        assert reader.read_body('100', '200', 1) == ''
        assert len(starts) == 1
    finally:
        release.set()
        assert reader._PROFILE_BUSY.acquire(timeout=3)
        reader._PROFILE_BUSY.release()
    assert reader.read_body('100', '200', 1) == 'body'


def test_missing_profile(monkeypatch):
    monkeypatch.setattr(reader, 'auth_profile_for_fetch', lambda _: None)
    monkeypatch.setattr(reader, 'ChromeCDP', lambda **_: pytest.fail('unexpected browser launch'))
    assert reader.read_body('100', '200', 1) == ''


@pytest.mark.parametrize(('question', 'answer', 'timeout'), [
    ('100', '201/../200', 2), ('１２', '200', 2), ('100', '200', 0), ('100', '200', float('nan')),
])
def test_invalid_input(monkeypatch, question, answer, timeout):
    monkeypatch.setattr(reader, 'auth_profile_for_fetch', lambda _: pytest.fail('unexpected lookup'))
    assert reader.read_body(question, answer, timeout) == ''
