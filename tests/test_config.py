# SPDX-License-Identifier: Apache-2.0
import pytest

from kiprim.config import Config, load


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return str(p)


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr("kiprim.config.DEFAULT_PATH", str(tmp_path / "absent.toml"))
    cfg = load()
    assert cfg == Config()
    assert (cfg.ceilings.max_voltage, cfg.ceilings.max_current) == (15.0, 10.0)
    assert cfg.output_off_on_exit is False
    assert cfg.expect_idn_prefix == "KIPRIM,DC620S"


def test_explicit_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(str(tmp_path / "nope.toml"))


def test_shipped_template_parses():
    from pathlib import Path
    cfg = load(str(Path(__file__).parent.parent / "install" / "config.toml"))
    assert cfg.port == 8100 and cfg.device == "/dev/kiprim"
    assert cfg.ceilings.ovp_margin == 0.1


def test_full_file(tmp_path):
    cfg = load(write(tmp_path, """
[serial]
device = "/dev/x"
expect_serial = "123"
[poll]
interval = 0.5
stale_after = 4
sample_interval_on = 0
[http]
port = 9000
write_allow_from = ["10.0.0.0/16"]
allowed_hosts = ["bench.lan"]
[limits]
max_voltage = 30
max_current = 5
ocp_margin = 0.2
[behaviour]
output_off_on_exit = true
"""))
    assert (cfg.device, cfg.expect_serial, cfg.poll_interval, cfg.stale_after) == ("/dev/x", "123", 0.5, 4.0)
    assert cfg.sample_interval_on == 0.0 and cfg.port == 9000
    assert cfg.allowed_hosts == ["bench.lan"] and len(cfg.write_networks()) == 1
    assert (cfg.ceilings.max_voltage, cfg.ceilings.max_current, cfg.ceilings.ocp_margin) == (30.0, 5.0, 0.2)
    assert cfg.output_off_on_exit is True


@pytest.mark.parametrize("text,match", [
    ("[nope]\n", "unknown section"),
    ("[http]\nprot = 1\n", "unknown key"),
    ("[http]\nport = \"8100\"\n", "must be int"),
    ("[behaviour]\noutput_off_on_exit = 1\n", "must be bool"),
    ("[limits]\nmax_voltage = true\n", "must be float"),
    ("[http]\nallowed_hosts = [1]\n", "list of strings"),
    ("[http]\nwrite_allow_from = [\"not-a-net\"]\n", "does not appear"),
    ("[limits]\nmax_voltage = 61\n", "max_voltage"),
    ("[limits]\nmax_current = 0\n", "max_current"),
    ("[poll]\ninterval = 0.01\n", "starve"),
    ("[poll]\nstale_after = 0.1\n", "stale_after"),
    ("[http]\nport = 70000\n", "port"),
])
def test_bad_values(tmp_path, text, match):
    with pytest.raises(ValueError, match=match):
        load(write(tmp_path, text))
