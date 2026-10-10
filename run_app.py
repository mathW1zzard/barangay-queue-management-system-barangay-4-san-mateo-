import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn
import webview

from app_logging import setup_logging, log
setup_logging()   # first, so even an error while importing main is recorded
import main  # imported directly (not "main:app") so PyInstaller bundles it

from paths import resource_path


def find_free_port() -> int:
    """Ask Windows for an unused port, so a program already on 8000 can't clash with the app."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int) -> None:
    # log_config=None: a windowed (--noconsole) app has no stdout/stderr, and uvicorn's
    # default logging crashes without them.
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_config=None)


def wait_until_ready(url: str, timeout: float = 30.0) -> bool:
    """Wait for the server to answer, so the window never opens on a blank error page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # the server answered (even with an error page), so it is up
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


if __name__ == "__main__":
    main.log_app_start()
    
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    log.info("Server address: %s", url) #Ready to remove for testing
    
    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()
    wait_until_ready(url)
    
    if not wait_until_ready(url):
        log.error("The server did not start within 30 seconds")    

# Let the app window download files (the attendance CSV). Very old pywebview versions have no settings.
    if hasattr(webview, "settings"):
        webview.settings["ALLOW_DOWNLOADS"] = True


    webview.create_window(
        "Barangay Queue & Service Management",
        url,
        width=1200,
        height=800,
    )
    webview.start(icon=resource_path("app_icon.ico"))
    main.log_app_closed()
    log.info("Application window closed")