"""单条推文读取：查询约束、逐请求来源、失败归因与补读次数。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from engines_base import pop_failure_note
from engines_builders_intl import _build_twitter_syndication_engine, extract_tweet_id
import twitter_single_read as single

ID = '1585841080431321088'
PRIMARY = {'__typename': 'Tweet', 'id_str': ID, 'text': 'the bird is freed',
           'user': {'screen_name': 'elonmusk'}, 'mediaDetails': [{}, {}]}
FX = {'code': 200, 'tweet': {'id': ID, 'text': 'the bird is freed', 'author': {'screen_name': 'elonmusk'}}}


@pytest.mark.parametrize('query,expected', [
    (ID, ID), ('123456789', None), ('https://x.com/a/status/20', '20'),
    ('https://twitter.com/a/statuses/20?s=1', '20'), ('https://x.com/i/web/status/20', '20'),
    ('https://x.com/i/web/status?tweet_id=20', '20'), ('https://example.org/?id=20', None),
    ('https://notx.com/a/status/20', None), ('https://x.com/a/status/123abc', None),
])
def test_extract(query, expected):
    assert extract_tweet_id(query) == expected


@pytest.fixture
def run(monkeypatch):
    def invoke(*payloads, query=ID):
        calls = []
        def get(url, *, headers, timeout, max_retries):
            assert max_retries == 0 and 0 < timeout <= 8
            payload = payloads[len(calls)]
            calls.append(url)
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, int):
                raise single.FxTwitterError('HTTP failure', code=payload)
            return (payload if isinstance(payload, bytes) else json.dumps(payload).encode()), 200
        monkeypatch.setattr(single, 'http_get', get)
        pop_failure_note(single.ENGINE)
        rows = _build_twitter_syndication_engine({})(query)
        return rows, calls, pop_failure_note(single.ENGINE)
    return invoke


def test_primary(run):
    rows, calls, note = run(PRIMARY)
    assert len(calls) == 1 and '&token=argo' in calls[0] and note is None
    assert rows[0]['url'] == f'https://x.com/elonmusk/status/{ID}'
    assert rows[0]['source'] == single.ENGINE and '2 项媒体' in rows[0]['snippet']


def test_keyword_does_not_dial(run):
    assert run(query='not a tweet') == ([], [], None)


@pytest.mark.parametrize('primary', [408, 429, 503, {}, b'', b'{'])
def test_temporary_recovers_once(run, primary):
    rows, calls, note = run(primary, FX)
    assert len(calls) == 2 and calls[1] == f'https://api.fxtwitter.com/status/{ID}'
    assert note is None and rows[0]['source'] == 'twitter_syndication/fxtwitter'
    assert rows[0]['url'].endswith('/' + ID) and 'the bird is freed' in rows[0]['snippet']


@pytest.mark.parametrize('primary', [401, 403, 404, 410, {'__typename': 'TweetTombstone'},
                                     {'errors': [{'message': 'not authorized'}]},
                                     b'<html>Sign in</html>', {**PRIMARY, 'id_str': '21'},
                                     {**PRIMARY, 'user': {'protected': True}}])
def test_terminal_does_not_fallback(run, primary):
    rows, calls, note = run(primary)
    assert rows == [] and len(calls) == 1 and note['reason'] == 'tweet-read-failed'


@pytest.mark.parametrize('fx', [503, 401, 404, {}, b'bad json', {'code': 404},
                                {'tweet': {'id': '21', 'text': 'wrong'}},
                                {'tweet': {'text': 'no id'}}, {'tweet': {'id': ID}}])
def test_fallback_failure_is_not_empty_success(run, fx):
    rows, calls, note = run(503, fx)
    assert rows == [] and len(calls) == 2
    assert 'HTTP 503' in note['detail'] and single.FX_SOURCE in note['detail']


def test_network_failure_vs_policy_refusal(run):
    failed = single.FxTwitterError('network')
    failed.__cause__ = TimeoutError()
    assert run(failed, FX)[0][0]['source'] == single.FX_SOURCE
    rows, calls, note = run(single.FxTwitterError('policy'))
    assert not rows and len(calls) == 1 and 'refused' in note['detail']
