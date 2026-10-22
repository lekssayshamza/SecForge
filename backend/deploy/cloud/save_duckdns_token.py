"""Save a newly rotated DuckDNS token using a private local prompt."""
import argparse
import getpass
import os
from pathlib import Path
import re
import stat


def private_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Token directory must be owned by you, private, and not a symlink")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-rotated", action="store_true", required=True)
    args = parser.parse_args()
    if not args.confirm_rotated:
        parser.error("Rotate the previously exposed token first")
    directory = Path.home() / ".config/secforge-cloud"
    private_directory(directory)
    token = getpass.getpass("Newly rotated DuckDNS token (hidden): ").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", token):
        raise SystemExit("Invalid token format; nothing saved")
    descriptor = os.open(directory / "duckdns.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(token + "\n")
        output.flush()
        os.fsync(output.fileno())
    print("Rotated token saved privately. No token value was printed.")


if __name__ == "__main__":
    try:
        main()
    except FileExistsError:
        raise SystemExit("A token file already exists; do not overwrite it without checking ownership") from None
