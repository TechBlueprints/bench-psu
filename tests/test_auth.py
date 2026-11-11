# SPDX-License-Identifier: Apache-2.0
from http import HTTPStatus

from conftest import TOKEN

from kiprim.config import Config
from kiprim.daemon import check_write


def cfg(tmp_path, token=TOKEN, allow=()):
    f = tmp_path / "token"
    if token is not None:
        f.write_text(token)
    return Config(token_file=str(f), write_allow_from=list(allow))


def test_good_token(tmp_path):
    assert check_write(cfg(tmp_path), f"Bearer {TOKEN}", "10.0.0.5")[0]


def test_missing_and_wrong_token(tmp_path):
    c = cfg(tmp_path)
    assert check_write(c, None, "10.0.0.5")[1] == HTTPStatus.UNAUTHORIZED
    assert check_write(c, "Basic abc", "10.0.0.5")[1] == HTTPStatus.UNAUTHORIZED
    assert check_write(c, "Bearer nope", "10.0.0.5")[1] == HTTPStatus.FORBIDDEN


def test_no_token_file_fails_closed(tmp_path):
    c = cfg(tmp_path, token=None)
    ok, status, _ = check_write(c, f"Bearer {TOKEN}", "127.0.0.1")
    assert not ok and status == HTTPStatus.SERVICE_UNAVAILABLE
    c2 = cfg(tmp_path, token="  \n")
    assert not check_write(c2, "Bearer ", "127.0.0.1")[0]


def test_allow_list(tmp_path):
    c = cfg(tmp_path, allow=["10.0.0.0/24", "127.0.0.1"])
    assert check_write(c, f"Bearer {TOKEN}", "10.0.0.9")[0]
    assert check_write(c, f"Bearer {TOKEN}", "192.168.1.1")[1] == HTTPStatus.FORBIDDEN


def test_http_refuses_without_token(make_daemon):
    r = make_daemon()
    for token, code in ((None, 401), ("wrong", 403)):
        status, body = r.req("POST", "/api/output", {"on": True}, token=token)
        assert status == code and body["ok"] is False
    assert r.supply.setters_received() == []


def test_http_refuses_cross_origin(make_daemon):
    r = make_daemon()
    status, _ = r.req("POST", "/api/output", {"on": True}, token=TOKEN,
                      headers={"Origin": "http://evil.example"})
    assert status == 403
    assert r.supply.setters_received() == []


def test_reads_are_open(make_daemon):
    r = make_daemon(token=None)
    assert r.req("GET", "/api/status")[0] == 200
    assert r.req("POST", "/api/output", {"on": True}, token=TOKEN)[0] == 503
