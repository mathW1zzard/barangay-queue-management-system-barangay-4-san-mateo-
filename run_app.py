import threading
import uvicorn
import webview

def start_serve():
    uvicorn.run("main:app", host="127.0.0.1", port= 8000, log_level="warning")
    
if __name__ == "__main__":
    server_thread = threading.Thread(target= start_serve, daemon=True)
    server_thread.start()
    
    webview.create_window(
        "Barangay Queue & Service Management",
        "http://127.0.0.1:8000",
        width=1200,
        height=800
    )
    webview.start()