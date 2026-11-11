#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Install, upgrade or uninstall bench-psu on a Debian host. Run as root from
# a checkout (or an unpacked `git archive`). Idempotent.
#
#   install.sh install [--id-path <ID_PATH>] [--tty /dev/ttyUSB0] [--allow-from <CIDR>]
#   install.sh upgrade            same as install; keeps config, token, udev rule, firewall file
#                                 (--tty or --id-path rewrites the udev rule, e.g. after moving
#                                 the supply to another USB port)
#
# --allow-from writes /etc/nftables.d/bench-psu.nft opening the daemon's TCP
# port to that network, for hosts whose nftables.conf includes /etc/nftables.d/*.nft
# with an `inet filter` table and an `input` chain.
#   install.sh uninstall [--purge]
#
# The supply's output is never touched: the service is (re)started, which
# sends queries only. Uses only the standard library, so no package is needed
# beyond python3 (>= 3.11).
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
LIB=/usr/local/lib/bench-psu
BIN=/usr/local/bin
DOC=/usr/local/share/doc/bench-psu
ETC=/etc/bench-psu
UNIT=/etc/systemd/system/bench-psud.service
RULE=/etc/udev/rules.d/70-kiprim.rules
SYSUSERS=/etc/sysusers.d/bench-psu.conf
NFT=/etc/nftables.d/bench-psu.nft

say() { printf 'bench-psu: %s\n' "$*"; }
die() { say "error: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

detect_id_path() {
    tty=$1
    if [ -z "$tty" ]; then
        set --
        for dev in /dev/ttyUSB*; do
            [ -e "$dev" ] || continue
            props=$(udevadm info -q property -n "$dev")
            echo "$props" | grep -qx 'ID_VENDOR_ID=1a86' || continue
            echo "$props" | grep -qx 'ID_MODEL_ID=7523' || continue
            set -- "$@" "$dev"
        done
        [ $# -eq 1 ] || die "found $# CH340 ttys; pass --tty or --id-path"
        tty=$1
    fi
    udevadm info -q property -n "$tty" | sed -n 's/^ID_PATH=//p'
}

write_launcher() {
    target=$1 module=$2
    cat > "$target.new" <<EOF
#!/usr/bin/python3 -I
# Installed by bench-psu install.sh
import sys
sys.path.insert(0, "$LIB")
from $module import main
sys.exit(main())
EOF
    chmod 0755 "$target.new"
    mv "$target.new" "$target"
}

do_install() {
    id_path="" tty="" allow_from=""
    while [ $# -gt 0 ]; do
        case $1 in
            --id-path) id_path=$2; shift 2 ;;
            --tty) tty=$2; shift 2 ;;
            --allow-from) allow_from=$2; shift 2 ;;
            *) die "unknown option $1" ;;
        esac
    done
    python3 -c 'import sys; assert sys.version_info >= (3, 11)' || die "python3 >= 3.11 needed"

    # service user (in dialout)
    install -D -m 0644 "$SRC/install/bench-psu.sysusers" "$SYSUSERS"
    systemd-sysusers "$SYSUSERS"

    # code
    rm -rf "$LIB.new"
    mkdir -p "$LIB.new"
    cp -R "$SRC/src/kiprim" "$LIB.new/kiprim"
    find "$LIB.new" -name '__pycache__' -prune -exec rm -rf {} +
    python3 -m compileall -q "$LIB.new" >/dev/null
    rm -rf "$LIB.old"
    [ -d "$LIB" ] && mv "$LIB" "$LIB.old"
    mv "$LIB.new" "$LIB"
    rm -rf "$LIB.old"
    write_launcher "$BIN/kiprim" kiprim.cli
    write_launcher "$BIN/bench-psud" kiprim.daemon
    install -D -m 0644 "$SRC/README.md" "$DOC/README.md"
    install -D -m 0644 "$SRC/install/config.toml" "$DOC/config.toml.example"

    # config: once, never overwritten
    install -d -m 0750 -o root -g bench-psu "$ETC"
    if [ ! -e "$ETC/config.toml" ]; then
        install -m 0640 -o root -g bench-psu "$SRC/install/config.toml" "$ETC/config.toml"
        say "installed default config $ETC/config.toml"
    fi
    # token: generated here, readable by root and the service only, never printed
    if [ ! -s "$ETC/token" ]; then
        (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$ETC/token.new")
        chown root:bench-psu "$ETC/token.new"
        chmod 0640 "$ETC/token.new"
        mv "$ETC/token.new" "$ETC/token"
        say "generated write token in $ETC/token (root and bench-psu only)"
    fi

    # udev rule: keep an existing one unless --id-path or --tty asks for a new one
    if [ -n "$id_path" ] || [ -n "$tty" ] || [ ! -e "$RULE" ]; then
        [ -n "$id_path" ] || id_path=$(detect_id_path "$tty")
        [ -n "$id_path" ] || die "could not determine ID_PATH; pass --id-path"
        sed "s|@ID_PATH@|$id_path|" "$SRC/install/70-kiprim.rules.in" > "$RULE.new"
        mv "$RULE.new" "$RULE"
        say "udev rule for ID_PATH=$id_path"
        udevadm control --reload
        udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyUSB*'
        udevadm settle --timeout=10 || true
    fi

    # firewall drop-in (optional)
    if [ -n "$allow_from" ]; then
        port=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("http", {}).get("port", 8100))' "$ETC/config.toml")
        mkdir -p "$(dirname "$NFT")"
        case $allow_from in *:*) fam=ip6 ;; *) fam=ip ;; esac
        printf '# bench-psu: API, status page and MCP (installed by bench-psu install.sh)\nadd rule inet filter input %s saddr %s tcp dport %s accept\n' \
            "$fam" "$allow_from" "$port" > "$NFT.new"
        mv "$NFT.new" "$NFT"
        say "firewall: tcp/$port from $allow_from in $NFT"
        if systemctl is-active --quiet nftables.service; then
            nft -c -f /etc/nftables.conf || die "nftables config check failed; $NFT left in place, nftables not restarted"
            systemctl restart nftables.service
            say "restarted nftables"
        fi
    fi

    # service
    install -m 0644 "$SRC/install/bench-psud.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable bench-psud.service >/dev/null
    if systemctl is-active --quiet bench-psud.service; then
        systemctl restart bench-psud.service
        say "restarted bench-psud (output untouched)"
    else
        systemctl start bench-psud.service
        say "started bench-psud"
    fi
    if [ -e /dev/kiprim ]; then
        say "/dev/kiprim -> $(readlink -f /dev/kiprim)"
    else
        say "warning: /dev/kiprim does not exist yet (supply unplugged?)"
    fi
}

do_uninstall() {
    purge=0
    [ "${1:-}" = "--purge" ] && purge=1
    if systemctl list-unit-files bench-psud.service >/dev/null 2>&1; then
        systemctl disable --now bench-psud.service 2>/dev/null || true
    fi
    rm -f "$UNIT" "$BIN/kiprim" "$BIN/bench-psud"
    systemctl daemon-reload
    rm -rf "$LIB" "$DOC"
    if [ -e "$NFT" ]; then
        rm -f "$NFT"
        systemctl is-active --quiet nftables.service && systemctl restart nftables.service
    fi
    if [ -e "$RULE" ]; then
        rm -f "$RULE"
        udevadm control --reload
        udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyUSB*'
    fi
    if [ $purge = 1 ]; then
        rm -rf "$ETC"
        rm -f "$SYSUSERS"
        if id bench-psu >/dev/null 2>&1; then
            userdel bench-psu 2>/dev/null || true
            groupdel bench-psu 2>/dev/null || true
        fi
        say "uninstalled and purged config, token and user"
    else
        say "uninstalled; kept $ETC and the bench-psu user (use --purge to remove)"
    fi
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case $cmd in
    install|upgrade) do_install "$@" ;;
    uninstall) do_uninstall "$@" ;;
    *) die "usage: install.sh install|upgrade [--id-path P] [--tty DEV] [--allow-from CIDR] | uninstall [--purge]" ;;
esac
