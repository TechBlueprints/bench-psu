# SPDX-License-Identifier: Apache-2.0
import pytest

from kiprim import parse


@pytest.mark.parametrize("reply,expected", [
    ("0.001,0.000", (0.001, 0.0, None)),          # the real DC620S V2.3.0
    ("13.800 4.000", (13.8, 4.0, None)),
    ("13.800,1.000,13.800", (13.8, 1.0, 13.8)),    # OWON three-field form
    ("13.800 1.000 13.800", (13.8, 1.0, 13.8)),
    (" 1.0 , 2.0 \r", (1.0, 2.0, None)),
])
def test_meas_all_tolerant(reply, expected):
    m = parse.parse_meas_all(reply)
    assert (m.voltage, m.current, m.power) == expected


@pytest.mark.parametrize("reply", ["", "1.0", "1,2,3,4", "a,b"])
def test_meas_all_rejects(reply):
    with pytest.raises(parse.ParseError):
        parse.parse_meas_all(reply)


@pytest.mark.parametrize("reply,value", [("ON", True), ("OFF", False), ("1", True), ("0", False),
                                         (" on\r", True)])
def test_outp_both_forms(reply, value):
    assert parse.parse_bool(reply) is value


def test_bool_rejects():
    with pytest.raises(parse.ParseError):
        parse.parse_bool("MAYBE")


def test_info_real_reply():
    i = parse.parse_meas_info("13.800,0.000,0.000,OFF,OFF,OFF,1")
    assert (i.voltage, i.current, i.power) == (13.8, 0.0, 0.0)
    assert not (i.ovp_tripped or i.ocp_tripped or i.otp_tripped)
    assert i.mode_code == 1 and i.mode == "cv"


def test_info_space_and_digits():
    i = parse.parse_meas_info("5.000 2.000 10.000 0 1 0 2")
    assert i.ocp_tripped and i.mode == "cc"


def test_info_wrong_count():
    with pytest.raises(parse.ParseError):
        parse.parse_meas_info("1,2,3")


def test_idn_real_reply():
    ident = parse.parse_idn("KIPRIM,DC620S,12345678,FV:V2.3.0")
    assert ident.as_dict() == {"manufacturer": "KIPRIM", "model": "DC620S", "serial": "12345678",
                               "firmware": "V2.3.0", "raw": "KIPRIM,DC620S,12345678,FV:V2.3.0"}


def test_float_with_unit():
    assert parse.parse_float("13.800V") == 13.8
    with pytest.raises(parse.ParseError):
        parse.parse_float("ERRx")
