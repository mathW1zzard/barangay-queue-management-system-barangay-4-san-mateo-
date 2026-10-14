import ctypes
import socket
import sys
import threading
import time

import uvicorn
import webview

from app_logging import setup_logging, log
setup_logging()   # first, so even an error while importing main is recorded

from paths import resource_path

STARTUP_TIMEOUT = 30.0


def start_server():
    """Create the listening socket here and hand it to uvicorn, so no other program can grab
    the port between 'find a free port' and 'start the server'."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))                       # port 0 = the OS picks a free one
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_config=None))   # no console in a windowed exe
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    return server, thread, port


def wait_until_started(server, thread, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return True
        if not thread.is_alive():                     # the server crashed while starting
            return False
        time.sleep(0.1)
    return False

_ERROR_ALREADY_EXISTS = 183
_instance_handle = None          # keep the handle alive for the life of the process

def acquire_single_instance() -> bool:
    """Windows named mutex: True if we are the first copy running in this Windows session."""
    global _instance_handle
    if sys.platform != "win32":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _instance_handle = k32.CreateMutexW(None, False, "Local\\BarangayQueueManagement_SingleInstance")
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

if not acquire_single_instance():
    log.warning("A second copy was started and closed itself")
    ctypes.windll.user32.MessageBoxW(None, "The Barangay Queuing and Attendance Management System is already running.",
                                     "Barangay Queuing and Attendance Management", 0x40)
    sys.exit(0)

import main 

if __name__ == "__main__":
    main.log_app_start()

    server, thread, port = start_server()
    url = f"http://127.0.0.1:{port}"

    if not wait_until_started(server, thread):
        log.error("The server did not start (see the errors above in app.log)")
        webview.create_window("Barangay Queuing and Attendance Management",
                              html="<h2 style='font-family:sans-serif'>The application could not start.</h2>"
                                   "<p style='font-family:sans-serif'>Please contact the system administrator. "
                                   "Details are in app.log.</p>", width=600, height=300)
        webview.start(icon=resource_path("app_icon.ico"))
        sys.exit(1)

    log.info("Server address: %s", url)

    # Let the app window download files (the attendance CSV). Very old pywebview versions have no settings.
    if hasattr(webview, "settings"):
        webview.settings["ALLOW_DOWNLOADS"] = True

    webview.create_window("Barangay Queuing and Attendance Management", url, width=1200, height=800)
    webview.start(icon=resource_path("app_icon.ico"))

    server.should_exit = True        # ask uvicorn to finish in-flight requests
    thread.join(timeout=5)
    main.log_app_closed()
    log.info("Application window closed")