# bench-psu

> **Experimental. Use at your own risk.** This software drives a laboratory power supply that
> can deliver 60 V or 20 A, enough to start fires, destroy equipment and injure people.
> **Read the code yourself** before you point it at real hardware. A software bug, a wrong
> setting, an agent misreading a tool, or a firmware quirk can energise the output or change
> its voltage or current. **You are entirely responsible for any damage, injury, data loss or
> downtime that results from using it.** The authors and contributors accept no liability.

Headless control of a **Kiprim DC620S** bench power supply (0–60 V, 0–20 A, 400 W) from a
small Linux host over the supply's rear USB port. The supply speaks the OWON SPE/SPS SCPI
dialect over a CH340 USB-serial bridge; the host puts it on the network.

- **Driver library** (`kiprim`): standard-library Python only (`termios`, no pyserial), an
  exclusive `flock` + `TIOCEXCL` on the tty, `\n` framing, `ERR` as an exception, a "lost" state
  after repeated timeouts, tolerant parsing, and **every setter verified by readback**.
- **Daemon** (`bench-psud`): the only process that owns the port. Polls at 4 Hz (two queries per
  poll), serves a JSON API, a status page and an **MCP endpoint**, logs every state change and a
  periodic measurement sample as JSON lines, and keeps Wh / Ah counters.
- **CLI** (`kiprim`): talks to the daemon; `--direct` opens the tty when the daemon is stopped.
- **Simulator** and tests: a fake supply modelled on what the real unit answered.

## Disclaimer

**Kiprim** and **OWON** are trademarks of their respective owners. This project is an
independent effort and is **not affiliated with, endorsed by, or connected with** Kiprim,
Shenzhen Kepurui Business Co., OWON or Lilliput. The names are used only to identify the
hardware this software works with.

- **No warranty.** Provided **AS IS** under the Apache License 2.0 (see [LICENSE](LICENSE));
  liability is disclaimed to the fullest extent permitted by law.
- **Tested on one unit.** DC620S firmware V2.3.0. Other firmware and other models in the family
  may answer differently; run `kiprim probe` first.
- **Assume write access is dangerous.** Anyone or anything holding the token (including an AI
  agent connected over MCP) can switch the output on and change the setpoints within the host
  ceilings.

## Safety rules

These hold for the software and for the people using it.

1. **The daemon never changes the output by itself.** Starting, stopping, restarting or
   reconnecting sends queries only. A crash leaves the supply as it was. The single exception is
   `output_off_on_exit = true` in the config (default **off**), which switches the output off when
   the daemon stops cleanly. An unexpected error in the worker is logged and the port reopened; if a
   thread dies anyway the process exits non-zero and systemd restarts it (queries only again).
2. **Identity first.** Until `*IDN?` matches `expect_idn_prefix` (default `KIPRIM,DC620S`) and, if
   set, `expect_serial`, nothing else is sent to the port.
3. **Host ceilings** (`max_voltage`, `max_current`, default 15 V / 10 A) are checked before
   anything is sent. The output is not switched on while a setpoint (perhaps set from the front
   panel) is above a ceiling.
4. **Protection limits keep a margin.** The voltage setpoint must stay at least `ovp_margin`
   (0.1 V) below OVP and the current setpoint at least `ocp_margin` (0.1 A) below OCP. A request
   that would break that is refused, not reordered. Switching the output on is refused too while
   front-panel settings break it.
5. **Current and limit changes cycle the output.** With the output on, a change to the current
   setpoint, OVP or OCP goes output OFF → write → output ON. Why: the two drivers that run this
   firmware family on a bench ([lab_Kiprim](https://github.com/Wajalot/lab_Kiprim),
   [kibarra-esp32](https://github.com/Wajalot/kibarra-esp32)) report that OCP and the `CURR:LIM`
   value are one register and that writing it with the output on and current flowing near the new
   value tripped OCP. On a DC620S (V2.3.0) `CURR` and `CURR:LIM` read back separately and a live
   `CURR` write with open terminals left `CURR:LIM` alone, but nothing has been tested with current
   flowing, so the cycle stays the default. `--live` / `"live": true` skips it on explicit request.
   If a write fails mid-cycle, the output is **left off**. Voltage setpoint changes apply live.
6. **One process owns the port** (`flock` + `TIOCEXCL`); the CLI's `--direct` mode fails while the
   daemon runs.
7. **Writes need a bearer token**, generated on the host by the installer and readable only by
   root and the service. Without a token file, writes are disabled. Reads are open.
8. **Never connect a battery to the supply directly.** The manual says the supply is not designed
   for direct battery charging: it has no reverse-current blocking and no remote sense, and its
   protections act on the supply, not the cells. Battery work needs a blocking element, a fuse, an
   independent meter and a dead-man disconnect, none of which this project provides.
9. Never raise OVP to silence an OVP trip. `*RST` is never sent by this software.
10. **The probe cleans up.** If a probe fails part-way, it still sends `SYST:LOCK OFF`, `SYST:LOC`
    and `OUTP OFF` for whatever it had turned on.
11. Test setters with **open terminals or a resistive load**, never a battery.

## Install (Debian 12/13 host)

Needs `python3` ≥ 3.11 and nothing else. From a checkout on the host, as root:

```sh
install/install.sh install            # detects the CH340 tty, writes the udev rule
install/install.sh install --tty /dev/ttyUSB0      # if more than one CH340 is present
install/install.sh install --allow-from 10.0.0.0/16  # also open the port in nftables (see below)
install/install.sh upgrade            # same as install; keeps config, token and udev rule
install/install.sh uninstall [--purge]
```

Or from a workstation, deploying the committed `HEAD` over SSH:

```sh
scripts/deploy.sh <ssh-host> install
```

What it installs:

| Path | What |
|---|---|
| `/usr/local/lib/bench-psu/kiprim/` | the package |
| `/usr/local/bin/kiprim`, `/usr/local/bin/bench-psud` | launchers (`python3 -I`) |
| `/etc/bench-psu/config.toml` | config, `root:bench-psu 0640`, installed once, never overwritten |
| `/etc/bench-psu/token` | write token, `root:bench-psu 0640`, generated once, never printed |
| `/etc/udev/rules.d/70-kiprim.rules` | `/dev/kiprim` from the USB port path (`ID_PATH`), `ID_MM_DEVICE_IGNORE=1` |
| `/etc/sysusers.d/bench-psu.conf` | system user `bench-psu`, member of `dialout` |
| `/etc/nftables.d/bench-psu.nft` | only with `--allow-from` |
| `/etc/systemd/system/bench-psud.service` | the unit, enabled, sandboxed (`ProtectSystem=strict`, `DevicePolicy=closed` + `char-ttyUSB`) |

CH340 bridges carry no USB serial number, so the supply is identified by the port it is plugged
into: move it to another USB port and re-run `install.sh install --tty /dev/ttyUSBn`.

On Debian, remove `brltty` if it is installed (it grabs CH340 devices).

`--allow-from <CIDR>` writes `/etc/nftables.d/bench-psu.nft` (`add rule inet filter input … tcp
dport <port> accept`) and restarts nftables after a syntax check. It assumes an `nftables.conf`
with an `inet filter` table, an `input` chain and `include "/etc/nftables.d/*.nft"`; on other
firewalls, open the port yourself.

To try the hardware before installing anything, `scripts/bundle.py` prints a single-file copy
of the CLI that runs from memory:
`python3 scripts/bundle.py | ssh <host> python3 - --direct --device /dev/ttyUSB0 probe`.

## Config

`/etc/bench-psu/config.toml`, see [install/config.toml](install/config.toml). Defaults:

| Key | Default | Meaning |
|---|---|---|
| `[serial] device` | `/dev/kiprim` | stable symlink from the udev rule |
| `[serial] expect_idn_prefix` / `expect_serial` | `KIPRIM,DC620S` / `""` | identity required before anything else is sent |
| `[poll] interval` | `0.25` | 4 Hz measurement poll (`MEAS:ALL:INFO?` + `OUTP?`), fixed rate |
| `[poll] stale_after` | `3.0` | no successful poll for this long: link `stale`, `/api/health` 503 |
| `[poll] sample_interval_on` / `_off` | `2.0` / `60.0` | `sample` journal lines while the output is on / off (0 = never) |
| `[poll] setpoint_interval` | `5.0` | full refresh of setpoints and limits |
| `[http] listen` / `port` | `0.0.0.0` / `8100` | API, status page, MCP |
| `[http] write_allow_from` | `[]` | optional CIDR allow-list for writes, on top of the token |
| `[http] allowed_hosts` | `[]` | extra `Host` names accepted (IP literals, `localhost`, the host's own name and FQDN always are) |
| `[http] request_timeout` | `10.0` | an idle or stalled connection is dropped after this |
| `[http] mcp` | `true` | MCP endpoint at `/mcp` |
| `[limits] max_voltage` / `max_current` | `15.0` / `10.0` | host ceilings for setpoints |
| `[limits] ovp_margin` / `ocp_margin` | `0.1` / `0.1` | V / A the protection limits must stay above the setpoints |
| `[behaviour] output_off_on_exit` | `false` | switch the output off when the daemon stops |

The plain HTTP API carries the token in the clear on the LAN. Put a TLS proxy in front, or use
`write_allow_from`, if that matters on your network. Every request, read or write, must carry a
`Host` header that is an IP literal or one of the host's names, and a browser `Origin` must match
it: that stops a web page from reaching the daemon through DNS rebinding.

## HTTP API

Reads are open. Writes are `POST` with a JSON body and `Authorization: Bearer <token>`.
Every write response carries `ok`, `result` (the read-back values) and the fresh `status`.

| Method and path | Body | Does |
|---|---|---|
| `GET /` | | status page (refreshes every 2 s) |
| `GET /api/status` | | the state document below |
| `GET /api/health` | | 200 while the link is up, 503 otherwise |
| `POST /api/set` | `{"volts": 13.8, "amps": 4, "live": false}` | both, at most one output cycle |
| `POST /api/voltage` | `{"volts": 13.8}` | voltage setpoint (live) |
| `POST /api/current` | `{"amps": 4, "live": false}` | current limit (cycles the output if on) |
| `POST /api/output` | `{"on": true}` | output on/off |
| `POST /api/ovp` | `{"volts": 15, "live": false}` | over-voltage protection limit (cycles the output if on) |
| `POST /api/ocp` | `{"amps": 5, "live": false}` | over-current protection limit (cycles the output if on) |
| `POST /api/lock` | `{"locked": true}` | front-panel lock |
| `POST /api/probe` | `{"level": "queries"}` | conformance probe (see below) |
| `POST /api/counters/reset` | `{}` | reset the Wh / Ah counters (sends nothing to the supply) |

Errors: 401/403 auth, 409 conflict with present setpoints (or probe refused), 422 above a
ceiling or the hardware range, 502 the supply answered `ERR` or read back a different value,
503 supply not connected or the wrong device, 504 timeout. On 401/403/422 nothing is sent to the
supply; on 409 the daemon has read the present values (queries) but sent no setter.

`/api/status`:

```json
{
  "time": "2026-11-11T20:00:00.000+00:00",
  "identity": {"manufacturer": "KIPRIM", "model": "DC620S", "serial": "…", "firmware": "V2.3.0", "raw": "…"},
  "output": true, "mode": "cv", "mode_code": 1,
  "setpoints": {"voltage": 13.8, "current": 4.0},
  "protection": {"ovp_limit": 63.0, "ocp_limit": 21.0, "ovp_tripped": false, "ocp_tripped": false, "otp_tripped": false},
  "measured": {"voltage": 13.8, "current": 0.0, "power": 0.0},
  "energy": {"wh": 0.0, "ah": 0.0, "output_on_s": 0.0, "since": "…"},
  "front_panel_lock": null,
  "link": {"state": "ok", "ok": true, "device": "/dev/kiprim", "age_s": 0.1, "poll_hz": 4.0,
           "reconnects": 0, "worker_alive": true, "…": "…"},
  "ceilings": {"max_voltage": 15.0, "max_current": 10.0, "ovp_margin": 0.1, "ocp_margin": 0.1},
  "warnings": [],
  "daemon": {"version": "0.2.0", "started": "…", "output_off_on_exit": false, "writes_enabled": true}
}
```

`mode` is `standby`, `cv`, `cc` or `fault` (the `MEAS:ALL:INFO?` mode code 0–3). The document is
shaped for a Home Assistant RESTful sensor (`value_template: "{{ value_json.measured.voltage }}"`
and so on).

## MCP

`POST /mcp` is a Model Context Protocol server (Streamable HTTP transport, JSON responses, no SSE
stream), built into the daemon so the one-owner rule still holds. Without the token it offers
only `get_status`; with `Authorization: Bearer <token>` it adds `set_voltage`, `set_current`,
`set_voltage_current`, `set_output`, `set_ovp`, `set_ocp`, `set_lock`, `reset_counters` and `probe`, with the same
ceilings and rules as the HTTP API. A wrong token is refused (401/403), not quietly treated as
read-only. Its server instructions repeat the safety rules for the agent. Tested with the official
MCP Python SDK client.

```sh
claude mcp add --transport http bench-psu http://<host>:8100/mcp     # read-only
```

For read-write, add `--header "Authorization: Bearer <token>"`, taking the token from
`/etc/bench-psu/token` on the host without echoing it into shared logs.

## CLI

```text
kiprim status                 full state
kiprim idn                    identity string
kiprim set 13.8 4 [--live]    voltage and current limit
kiprim volt 12.5              voltage only
kiprim curr 2 [--live]        current limit only
kiprim on | off               output
kiprim ovp 15 | ocp 5 [--live]  protection limits
kiprim reset-counters         zero the daemon's Wh / Ah counters
kiprim lock | unlock          front panel
kiprim watch [-i 1]           measurements until Ctrl-C
kiprim probe [--level L] [--markdown]
kiprim --direct status        bypass the daemon (it must be stopped)
kiprim --json status          JSON output
```

On the host as root the CLI reads the token file itself, so `ssh <host> kiprim set 13.8 4` works.
Elsewhere set `KIPRIM_URL=http://<host>:8100` and `KIPRIM_TOKEN`.

## Probe levels

`kiprim probe` runs the protocol table and prints a conformance table. Each level includes the
previous one.

| Level | Sends | Needs |
|---|---|---|
| `queries` | every query; changes nothing | nothing |
| `setters` | each setpoint and limit written back with its present value, `SYST:LOCK ON/OFF` (read back with `SYST:LOCK?` if it exists), `SYST:REM`/`SYST:LOC` | output off |
| `output` | `OUTP ON`, mode read, `OUTP OFF` | output off; open terminals or a resistive load |
| `ocp_quirk` | `OUTP ON`, live `CURR` and `CURR:LIM` writes of the unchanged values, checking for an OCP trip after each, `OUTP OFF`; only meaningful with current flowing | as above |

## Protocol (DC620S, firmware V2.3.0)

115200 8N1, commands end in `\n`, one-line replies. Setters answer nothing on success and `ERR`
on a rejected command.

| Command | Reply on V2.3.0 |
|---|---|
| `*IDN?` | `KIPRIM,DC620S,<serial>,FV:V2.3.0` |
| `OUTP?` | `ON` / `OFF` |
| `VOLT?` / `CURR?` | `13.800` / `4.000` |
| `VOLT:LIM?` / `CURR:LIM?` | `63.000` / `21.000` (factory) |
| `MEAS:VOLT?` / `MEAS:CURR?` / `MEAS:POW?` | `13.800` / `0.000` / `0.000` |
| `MEAS:ALL?` | `V,I` (two fields; some family firmware sends three, space-separated) |
| `MEAS:ALL:INFO?` | `V,I,P,<ovp>,<ocp>,<otp>,<mode>`, flags `ON`/`OFF`, mode 0 standby, 1 CV, 2 CC, 3 fault |
| `SYST:STAT?` | `ERR` |
| `VOLT`, `CURR`, `VOLT:LIM`, `CURR:LIM`, `OUTP ON\|OFF`, `SYST:LOCK ON\|OFF`, `SYST:REM`, `SYST:LOC` | silent on success, read back exactly (lock: no query exists, `SYST:LOCK?` → `ERR`) |
| `VOLT:LIM 63` | accepted (the manual says OVP 0–61 V) |

## Development

```sh
uv sync
uv run pytest
uv run bench-psud --simulate --listen 127.0.0.1 --config install/config.toml   # simulated supply
```

CI (GitHub Actions) runs the tests on Python 3.11–3.13 and `shellcheck` on the shell scripts.

The simulator (`kiprim.simulator`) models the verified behaviour, with switches for the
unverified quirks (`ocp_quirk`, three-field `MEAS:ALL?`, digit `OUTP?`, missing
`MEAS:ALL:INFO?`, `SYST:LOCK?`, dropped replies, readback offsets) and a resistive load.

## References

- OWON SP/SPE/SPS SCPI programming manual (the protocol family)
- [robbederks/owon-psu-control](https://github.com/robbederks/owon-psu-control) (MIT)
- [tiwut/Kiprim-DC310S-Power-Studio](https://github.com/tiwut/Kiprim-DC310S-Power-Studio) (MIT)
- [Wajalot/lab_Kiprim](https://github.com/Wajalot/lab_Kiprim) (the OCP-register quirk report)

No code was copied from these projects; they informed the command table and the quirk list.

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).

```
Copyright 2026 TechBlueprints

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
