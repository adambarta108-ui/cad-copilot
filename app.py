"""CAD Copilot desktop app: a chat window in front of the agent in agent.py.

    python app.py

The window is HTML (ui/index.html) shown by pywebview. JavaScript calls the
methods on Api; the agent runs on one background thread (SolidWorks' COM
interface must be used from the thread that created it) and streams progress
back to the page through window.onEvent(...).
"""
import json
import os
import queue
import sys
import threading
import webbrowser

import anthropic
import keyring
import webview

import agent
from sw_bridge import MockBridge, SolidWorksBridge, SolidWorksError, parts_folder, solidworks_installed

APP_NAME = "CAD Copilot"
KEYRING_SERVICE = "CAD Copilot"
KEYRING_USER = "anthropic-api-key"
SETTINGS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME, "settings.json")


def resource(path):
    """Path to a bundled file, both when run from source and inside the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, path)


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def get_api_key():
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or os.environ.get("ANTHROPIC_API_KEY")
    except keyring.errors.KeyringError:
        return os.environ.get("ANTHROPIC_API_KEY")


def friendly_api_error(e):
    if isinstance(e, anthropic.AuthenticationError):
        return "Your API key was rejected. Open Settings and paste a valid key."
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Check your internet connection and try again."
    message = getattr(e, "message", str(e))
    if "credit balance" in message:
        return "Your Anthropic account is out of credits. Add credits at console.anthropic.com (Billing), then try again."
    if isinstance(e, anthropic.RateLimitError):
        return "Too many requests right now. Wait a moment and try again."
    return f"The API returned an error: {message}"


class Worker:
    """Owns the conversation, the CAD connection and the agent loop, all on one thread."""

    def __init__(self, emit):
        self.emit = emit
        self.jobs = queue.Queue()
        self.stop_requested = threading.Event()
        self.messages = []
        self.bridge = None
        self.bridge_mode = None
        self.session_cost = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, job, *args):
        self.jobs.put((job, args))

    def _loop(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass
        while True:
            job, args = self.jobs.get()
            try:
                getattr(self, f"_{job}")(*args)
            except Exception as e:  # never let one bad job kill the worker thread
                self.emit("error", text=f"Unexpected error: {type(e).__name__}: {e}")
                self.emit("done", session=self.session_cost)

    def _reset(self):
        self.messages = []
        self.session_cost = 0.0
        self.emit("cost", turn=0.0, session=0.0)

    def _get_bridge(self, mode):
        if self.bridge is None or self.bridge_mode != mode:
            if mode == "solidworks":
                self.emit("status", text="Connecting to SolidWorks (it may take a minute to start)...")
                self.bridge = SolidWorksBridge()
            else:
                self.bridge = MockBridge()
            self.bridge_mode = mode
        return self.bridge

    def _send(self, text, mode, model):
        self.stop_requested.clear()
        key = get_api_key()
        if not key:
            self.emit("error", text="Add your Anthropic API key in Settings first.", open_settings=True)
            self.emit("done", session=self.session_cost)
            return
        try:
            bridge = self._get_bridge(mode)
        except SolidWorksError as e:
            self.emit("error", text=f"{e} You can switch to Demo mode to try the app without it.")
            self.emit("done", session=self.session_cost)
            return
        self.emit("status", text="")

        start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        before = self.session_cost

        def emit(kind, **data):
            if kind == "cost":
                self.session_cost = before + data["turn"]
                data["session"] = self.session_cost
            self.emit(kind, **data)

        try:
            client = anthropic.Anthropic(api_key=key)
            agent.run_turn(client, bridge, self.messages, emit=emit,
                           should_stop=self.stop_requested.is_set, model=model)
        except anthropic.APIError as e:
            # Drop the half-finished turn so the history stays valid. Features already built stay in the CAD model.
            del self.messages[start:]
            self.emit("error", text=friendly_api_error(e))
        self.emit("done", session=self.session_cost)


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>(...)."""

    def __init__(self):
        self._window = None
        self._settings = load_settings()
        self._sw_installed = solidworks_installed()
        self._worker = Worker(self._emit)

    def _emit(self, kind, **data):
        if self._window is not None:
            self._window.evaluate_js(f"window.onEvent({json.dumps({'kind': kind, **data})})")

    def _mode(self):
        mode = self._settings.get("mode") or ("solidworks" if self._sw_installed else "demo")
        return "demo" if mode == "solidworks" and not self._sw_installed else mode

    def _model(self):
        model = self._settings.get("model", agent.MODEL)
        return model if model in agent.MODELS else agent.MODEL

    def get_state(self):
        return {
            "has_key": bool(get_api_key()),
            "sw_installed": self._sw_installed,
            "mode": self._mode(),
            "model": self._model(),
            "models": agent.MODELS,
            "session_cost": self._worker.session_cost,
            "parts_folder": parts_folder(create=False),
        }

    def save_api_key(self, key):
        key = (key or "").strip()
        if not key.startswith("sk-ant-"):
            return {"ok": False, "error": "That doesn't look like an Anthropic API key. It should start with sk-ant-."}
        try:
            anthropic.Anthropic(api_key=key).models.retrieve(self._model())  # free check that the key works
        except anthropic.AuthenticationError:
            return {"ok": False, "error": "Anthropic rejected that key. Check you copied all of it."}
        except anthropic.APIError as e:
            return {"ok": False, "error": friendly_api_error(e)}
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        return {"ok": True}

    def remove_api_key(self):
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass
        return self.get_state()

    def set_mode(self, mode):
        if mode in ("solidworks", "demo") and mode != self._mode():
            self._settings["mode"] = mode
            save_settings(self._settings)
            self._worker.submit("reset")
        return self.get_state()

    def set_model(self, model):
        if model in agent.MODELS:
            self._settings["model"] = model
            save_settings(self._settings)
        return self.get_state()

    def send(self, text):
        text = (text or "").strip()
        if text:
            self._worker.submit("send", text, self._mode(), self._model())

    def stop(self):
        self._worker.stop_requested.set()

    def new_chat(self):
        self._worker.submit("reset")

    def open_url(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)

    def open_parts_folder(self):
        os.startfile(parts_folder())


def main():
    api = Api()
    api._window = webview.create_window(
        APP_NAME, resource(os.path.join("ui", "index.html")), js_api=api,
        width=1100, height=800, min_size=(720, 560),
    )
    webview.start()


if __name__ == "__main__":
    main()
