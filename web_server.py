import argparse
import socket
from pathlib import Path

import uvicorn

from app import FileServer


def get_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    try:
        host = socket.gethostname()
        _, _, host_ips = socket.gethostbyname_ex(host)
        for ip in host_ips:
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    return sorted(ips) if ips else ["127.0.0.1"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Web file server (HTTP)")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host, default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080, help="Listen port, default: 8080")
    parser.add_argument(
        "--root",
        default=str((Path.cwd() / "data").resolve()),
        help="Storage root directory",
    )
    parser.add_argument("--token", default="", help="Optional access token")
    args = parser.parse_args()

    root_dir = Path(args.root).resolve()
    fs = FileServer(root_dir=root_dir, token=args.token)

    print(f"[WebServer] Root: {root_dir}")
    print(f"[WebServer] Local: http://127.0.0.1:{args.port}")
    print("[WebServer] LAN (all):")
    for ip in get_lan_ips():
        print(f"  http://{ip}:{args.port}")
    print(f"[WebServer] Public hint: http://<public-ip-or-domain>:{args.port}")
    print("[WebServer] Web UI: open / in browser")

    uvicorn.run(fs.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
