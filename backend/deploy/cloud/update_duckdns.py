"""One scoped DuckDNS update, reading the rotated token privately."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import urllib.parse
import urllib.request


def read_private(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Input must be an owned private regular file")
        return source.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address-file", type=Path, required=True)
    args = parser.parse_args()
    token = read_private(Path.home() / ".config/secforge-cloud/duckdns.token").strip()
    address = json.loads(read_private(args.address_file))["public_ip"]
    if not ipaddress.ip_address(address).is_global:
        raise ValueError("Application address must be public")
    query = urllib.parse.urlencode(dict(domains="secforgeacademy", token=token, ip=address))
    try:
        with urllib.request.urlopen("https://www.duckdns.org/update?" + query, timeout=20) as response:
            result = response.read(16).decode().strip()
    except Exception:
        raise SystemExit("DuckDNS update failed; token and request URL were not logged") from None
    if result != "OK":
        raise SystemExit("DuckDNS rejected the update; check the rotated token privately")
    print("secforgeacademy.duckdns.org updated to the dedicated application address")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError):
        raise SystemExit("Private address/token input unavailable or unsafe; nothing updated") from None
