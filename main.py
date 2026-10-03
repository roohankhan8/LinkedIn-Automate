"""Launch the LinkedIn Apply desktop dashboard in a pywebview window."""

import threading

import webview
from app import app

PORT = 5050


def run_flask():
    app.run(host="127.0.0.1", port=PORT, debug=True, use_reloader=False)


def main():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    webview.create_window(
        "LinkedIn Apply",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
