# SPDX-License-Identifier: Apache-2.0
from conftest import TOKEN

from kiprim import cli
from kiprim.simulator import SimulatedSupply


def run(r, capsys, *argv, token=TOKEN):
    args = cli.build_parser().parse_args(list(argv))
    code = cli.run(args, cli.DaemonBackend(r.base, token))
    return code, capsys.readouterr().out


def test_status_and_idn(make_daemon, capsys):
    r = make_daemon(SimulatedSupply(output=True))
    code, out = run(r, capsys, "status")
    assert code == 0 and "output   ON" in out and "DC620S" in out
    code, out = run(r, capsys, "idn")
    assert out.strip() == "KIPRIM,DC620S,SIM00001,FV:V2.3.0"


def test_set_on_off(make_daemon, capsys):
    s = SimulatedSupply()
    r = make_daemon(s)
    assert run(r, capsys, "set", "13.8", "4")[0] == 0
    assert run(r, capsys, "on")[0] == 0 and s.output
    assert run(r, capsys, "off")[0] == 0 and not s.output
    assert (s.voltage, s.current) == (13.8, 4.0)


def test_write_without_token_is_refused(make_daemon, capsys):
    s = SimulatedSupply()
    r = make_daemon(s)
    args = cli.build_parser().parse_args(["on"])
    try:
        cli.run(args, cli.DaemonBackend(r.base, None))
    except cli.CliError as exc:
        assert "no token" in str(exc)
    else:
        raise AssertionError("expected CliError")
    assert not s.output


def test_probe_table(make_daemon, capsys):
    r = make_daemon()
    code, out = run(r, capsys, "probe", "--markdown")
    assert code == 0 and "| `*IDN?` | query |" in out


def test_main_unreachable(capsys):
    assert cli.main(["--url", "http://127.0.0.1:9", "status"]) == cli.EXIT_REFUSED
