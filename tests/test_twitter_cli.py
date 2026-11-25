import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import engines_twitter_cli as x

SPEC = yaml.safe_load((ROOT / 'engines/specs/twitter_cli.yaml').read_text(encoding='utf-8'))
POST = {'id': '1585841080431321088', 'text': 'the bird is freed', 'author': {'screenName': 'elonmusk'},
        'createdAtISO': '2022-10-28T03:49:11Z'}


def response(*items):
    return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'schema_version': '1', 'data': items}).encode())


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.setattr(x.shutil, 'which', lambda _: sys.executable)
    monkeypatch.setattr(x, 'build_child_env', lambda *a, extra_env: {**extra_env, 'TWITTER_AUTH_TOKEN': 'fixture-auth', 'TWITTER_CT0': 'fixture-csrf'})
    def setup(*responses):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            assert args[:3] == [sys.executable, 'search', '--json']
            assert kwargs['stderr'] == subprocess.DEVNULL
            assert Path(kwargs['env']['USERPROFILE']).is_file()
            assert kwargs['env']['PYTHONDONTWRITEBYTECODE'] == '1'
            assert 'fixture-auth' not in str(args) and 'fixture-csrf' not in str(args)
            assert len(calls) <= len(responses)
            return responses[len(calls)-1]
        monkeypatch.setattr(x.subprocess, 'run', run)
        return x._build_twitter_cli_engine(SPEC), calls
    return setup


def test_registered_and_explicit():
    import engines
    assert engines._BUILDERS[SPEC['type']] is x._build_twitter_cli_engine
    assert SPEC['explicit_only'] is True


def test_exact_window_with_same_coarse_candidates(invoke):
    fn, calls = invoke(response(POST), response(POST))
    query = 'the bird is freed from:ElonMusk'
    yes = fn(query, 5, since='2022-10-28T11:49:11+08:00', until='2022-10-28T11:49:12+08:00')
    no = fn(query, 5, since='2022-10-28T11:49:10+08:00', until='2022-10-28T11:49:11+08:00')
    assert [r['url'] for r in yes] == ['https://x.com/elonmusk/status/1585841080431321088']
    assert yes[0]['published_at'] == POST['createdAtISO'] and no == []
    assert calls[0][0] == calls[1][0]
    assert calls[0][0][7:] == ['--from', 'ElonMusk', '--since', '2022-10-27', '--until', '2022-10-30', '--', 'the bird is freed']


@pytest.mark.parametrize('query', ['(from:a OR from:b)', 'from:a OR outside', 'from:a -from:b', 'from:a from:b',
                                    '"from:shadow"', 'foo lang:en from:a', '--output', '-o', 'from:a since:2022-01-01 until:2022-02-01 OR now'])
def test_complex_queries_are_opaque(query, invoke):
    c = x.parse_x_constraints(query)
    assert c.complex_query and not c.strict and c.query == query
    c = x.parse_x_constraints(query, until='2022-10-28')
    assert c.strict and c.author is None and not x.matches_x_constraints(c, 'a', POST['createdAtISO'])
    fn, calls = invoke(response(POST))
    assert fn(query)[0]['tweet_id'] == POST['id']
    assert calls[0][0][-2:] == ['--', query]


def test_bound_intersection_and_utc_midnight():
    c = x.parse_x_constraints('since:2022-10-28 until:2022-10-29 from:@ElonMusk', since='2022-10-28T11:00:00+08:00')
    assert c.since.isoformat() == '2022-10-28T03:00:00+00:00'
    assert c.until.isoformat() == '2022-10-29T00:00:00+00:00'
    assert not x.matches_x_constraints(c, 'someone', POST['createdAtISO'])
    assert not x.matches_x_constraints(c, None, POST['createdAtISO'])
    for bogus in [None, '', '2022-10-28', 'yesterday', '2022-10-28T04:00:00']:
        assert not x.matches_x_constraints(c, 'elonmusk', bogus)
    assert x.matches_x_constraints(c, 'elonmusk', POST['createdAtISO'])
    for bad in ['昨天', '7d', '2022-02-31', '2022-10-28T03:01:02']:
        with pytest.raises(ValueError):
            x.parse_x_constraints('from:a', since=bad)
    with pytest.raises(ValueError):
        x.parse_x_constraints('from:a', since='2022-01-02', until='2022-01-02')


def test_expands_full_strict_batch_once_deduplicating_within_wall_clock_budget(invoke, monkeypatch):
    wrong = {**POST, 'author': {'screenName': 'other'}}
    fn, calls = invoke(response(POST, wrong, POST), response(POST, {**POST, 'id': '42'}, {**POST, 'id': '43'}))
    ticks = iter([100, 101, 105]); monkeypatch.setattr(x.time, 'monotonic', lambda: next(ticks))
    assert [r['tweet_id'] for r in fn('from:elonmusk', 3, 8)] == [POST['id'], '42', '43']
    assert [c[0][6] for c in calls] == ['3', '10']
    assert [c[1]['timeout'] for c in calls] == [7, 3]


def test_no_replay_for_exhausted_or_unconstrained(invoke):
    fn, calls = invoke(response(POST), response(POST, POST))
    assert fn('from:other', 2) == []
    assert len(fn('keyword', 2)) == 1
    assert len(calls) == 2


def test_valid_empty_and_malformed_or_failed_are_distinct(invoke):
    for stdout, rc in [(b'{}', 0), (b'[]', 0), (b'{"ok": false}', 0), (b'sensitive upstream message', 1),
                       (b'invalid', 0), (response({}).stdout, 0)]:
        fn, _ = invoke(SimpleNamespace(stdout=stdout, returncode=rc))
        err = fn('test')[0]
        assert err['error_kind'] == 'error' and 'sensitive' not in str(err)
    fn, _ = invoke(response())
    assert fn('test') == []


def test_missing_binary_or_credentials_skips_spawn(monkeypatch):
    monkeypatch.setattr(x.subprocess, 'run', lambda *a, **kw: pytest.fail('must not spawn'))
    monkeypatch.setattr(x, 'build_child_env', lambda *a, **kw: {})
    for path in (None, sys.executable):
        monkeypatch.setattr(x.shutil, 'which', lambda _: path)
        assert x._build_twitter_cli_engine(SPEC)('test')[0]['error_kind'] == 'unavailable'
