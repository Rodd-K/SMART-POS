cat > desktop_windows.py <<'PY'
"""Windows desktop launcher for the packaged SmartPOS application."""

import os
import sys
import time
import socket
import subprocess
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"


def application_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def prepare_environment():
    app_data = Path(os.environ.get(
        "LOCALAPPDATA",
        Path.home() / "AppData" / "Local"
    )) / "SmartPOS"

    app_data.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SMARTPOS_DB"] = str(app_data / "smartpos.db")
    env["SMARTPOS_CONFIG_DIR"] = str(app_data)
    env.setdefault("SMARTPOS_DEMO", "0")

    storage = app_data / "webview"
    storage.mkdir(parents=True, exist_ok=True)

    logs = app_data / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    return env, storage, logs


def port_in_use():
    with socket.socket() as sock:
        return sock.connect_ex((HOST, PORT)) == 0


def wait_for_server(process):
    for _ in range(80):
        if process.poll() is not None:
            raise RuntimeError("SmartPOS backend stopped unexpectedly.")

        try:
            with urllib.request.urlopen(URL, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)

    raise RuntimeError("SmartPOS backend did not start within 20 seconds.")


def main():
    if port_in_use():
        raise RuntimeError(
            "Port 8000 is already in use. Close the other SmartPOS instance."
        )

    app_dir = application_directory()
    server_exe = app_dir / "SmartPOS_Server.exe"

    if not server_exe.is_file():
        raise FileNotFoundError(f"Backend executable not found: {server_exe}")

    env, storage, logs = prepare_environment()
    server = None
    log_file = None

    try:
        log_file = open(logs / "server.log", "a", encoding="utf-8")

        server = subprocess.Popen(
            [str(server_exe)],
            cwd=str(app_dir),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        wait_for_server(server)

        import webview

        webview.create_window(
            "SmartPOS",
            URL,
            width=1280,
            height=800,
            min_size=(1024, 650),
        )

        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(storage),
        )

    finally:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
PY