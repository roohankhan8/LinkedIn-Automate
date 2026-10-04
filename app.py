"""Flask backend for the LinkedIn Apply desktop dashboard."""

import json
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request, render_template

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
RESUME_PROFILE_PATH = BASE_DIR / "resume_profile.json"
QA_CACHE_PATH = BASE_DIR / "qa_cache.json"
STATE_PATH = BASE_DIR / "linkedin_state.json"
LOGIN_DONE_FLAG = BASE_DIR / "login_done.flag"
ERROR_LOG_PATH = BASE_DIR / "error.log"

app = Flask(__name__)


class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self.process = None
        self.current = None
        self.logs = []
        self._last_returncode = None
        self._error = False

    def start(self, task, cmd):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return False
            self.current = task
            self.logs = []
            self._last_returncode = None
            self._error = False

        def run():
            info_log_path = BASE_DIR / "info.log"
            with (
                open(info_log_path, "w", encoding="utf-8") as info_log,
                open(ERROR_LOG_PATH, "w", encoding="utf-8") as error_log,
            ):
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(BASE_DIR),
                )
                with self._lock:
                    self.process = process

                def collect(stream, output_file):
                    for line in stream:
                        output_file.write(line)
                        output_file.flush()
                        with self._lock:
                            self.logs.append(line.rstrip())

                stdout_thread = threading.Thread(
                    target=collect, args=(process.stdout, info_log), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=collect, args=(process.stderr, error_log), daemon=True
                )
                stdout_thread.start()
                stderr_thread.start()
                process.wait()
                stdout_thread.join()
                stderr_thread.join()
                with self._lock:
                    self.process = None
                    self._last_returncode = process.returncode
                    self._error = process.returncode != 0
                    self.current = None

        threading.Thread(target=run, daemon=True).start()
        return True

    def status(self):
        with self._lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "task": self.current,
                "running": running,
                "logs": self.logs[-200:],
                "returncode": self._last_returncode,
                "error": self._error,
            }


runner = Runner()


def _read_text(path):
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login/status")
def login_status():
    return jsonify({"logged_in": STATE_PATH.exists()})


@app.route("/api/config", methods=["GET", "POST"])
def config_route():
    if request.method == "POST":
        data = request.get_json()
        _write_json(CONFIG_PATH, data)
        return jsonify({"ok": True})
    return jsonify(_read_json(CONFIG_PATH))


@app.route("/api/env", methods=["GET", "POST"])
def env_route():
    if request.method == "POST":
        _write_text(ENV_PATH, request.get_json().get("value", ""))
        return jsonify({"ok": True})
    return jsonify({"value": _read_text(ENV_PATH)})


@app.route("/api/resume_profile", methods=["GET", "POST"])
def resume_profile_route():
    if request.method == "POST":
        data = request.get_json()
        _write_json(RESUME_PROFILE_PATH, data)
        return jsonify({"ok": True})
    return jsonify(_read_json(RESUME_PROFILE_PATH))


@app.route("/api/qa_cache", methods=["GET", "POST"])
def qa_cache_route():
    if request.method == "POST":
        data = request.get_json()
        _write_json(QA_CACHE_PATH, data)
        return jsonify({"ok": True})
    return jsonify(_read_json(QA_CACHE_PATH))


@app.route("/api/status")
def status():
    return jsonify(runner.status())


@app.route("/api/login", methods=["POST"])
def start_login():
    if LOGIN_DONE_FLAG.exists():
        LOGIN_DONE_FLAG.unlink()
    python = sys.executable
    ok = runner.start("login", [python, "linkedin_login.py"])
    return jsonify({"started": ok, "running": not ok})


@app.route("/api/login/confirm", methods=["POST"])
def confirm_login():
    LOGIN_DONE_FLAG.touch(exist_ok=True)
    return jsonify({"ok": True})


@app.route("/api/search", methods=["POST"])
def start_search():
    python = sys.executable
    ok = runner.start("search", [python, "linkedin_search.py"])
    return jsonify({"started": ok, "running": not ok})


@app.route("/api/ollama/logs", methods=["GET"])
def ollama_logs():
    try:
        result = subprocess.run(
            ["docker", "logs", "ollama", "--tail", "100"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return jsonify({"logs": (result.stdout or "") + (result.stderr or "")})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.stdout or "") + (e.stderr or "")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
