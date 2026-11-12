#!/usr/bin/env python3
"""Integration checks for the local browser bridge (bin/speak-bridge)."""

import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bin" / "speak-bridge"


def load_bridge_module():
    name = f"_omarchy_tts_bridge_test_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(BRIDGE))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BRIDGE_MODULE = load_bridge_module()

STUB_SPEAK = r'''#!/usr/bin/env python3
import json
import os
import signal
import struct
import subprocess
import sys
import time


def main():
    with open(os.environ["BRIDGE_STUB_CONTROL"], encoding="utf-8") as handle:
        cfg = json.load(handle)
    args = sys.argv[1:]
    calls = os.environ.get("BRIDGE_STUB_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
    if "--info" in args:
        sys.stdout.write(json.dumps(cfg.get("info", {})))
        return 0
    if "--stop" in args:
        pid_file = os.environ.get("BRIDGE_STUB_ESCAPED_PID")
        if pid_file and os.path.exists(pid_file):
            try:
                os.kill(int(open(pid_file, encoding="utf-8").read()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        return 0

    text = sys.stdin.read()
    with open(os.environ["BRIDGE_STUB_STDIN"], "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.environ["BRIDGE_STUB_ENV"], "w", encoding="utf-8") as handle:
        json.dump({"silent": os.environ.get("TTS_SILENT"),
                   "cache": os.environ.get("TTS_CACHE"),
                   "out": os.environ.get("TTS_AUDIO_OUT"),
                   "meta": os.environ.get("TTS_AUDIO_META")}, handle)

    mode = cfg.get("mode", "empty")
    out = os.environ.get("TTS_AUDIO_OUT")
    meta = os.environ.get("TTS_AUDIO_META")

    def write_meta(value):
        with open(meta, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    if mode == "pcm":
        write_meta({"format": "pcm-s16le", "rate": 22050, "channels": 1})
        with open(out, "wb") as handle:
            handle.write(b"\x01\x02" * 100)
    elif mode == "pcm_stereo":
        write_meta({"format": "pcm-s16le", "rate": 44100, "channels": 2})
        with open(out, "wb") as handle:
            handle.write(bytes(range(101)))
    elif mode == "mp3":
        write_meta({"format": "file"})
        with open(out, "wb") as handle:
            handle.write(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 8)
    elif mode == "wav":
        write_meta({"format": "file"})
        data = b"\x00\x00" * 40
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data),
                             b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2,
                             16, b"data", len(data))
        with open(out, "wb") as handle:
            handle.write(header + data)
    elif mode == "ogg":
        write_meta({"format": "file"})
        with open(out, "wb") as handle:
            handle.write(b"OggS" + b"\x00" * 32)
    elif mode == "flac":
        write_meta({"format": "file"})
        with open(out, "wb") as handle:
            handle.write(b"fLaC" + b"\x00" * 32)
    elif mode == "fail":
        sys.stderr.write("boom " + text + "\nsecond line\n")
        return 3
    elif mode == "escape":
        # Mimic bin/speak: the provider runs in its own session and therefore
        # survives a killpg of the CLI that started it.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        with open(os.environ["BRIDGE_STUB_ESCAPED_PID"], "w",
                  encoding="utf-8") as handle:
            handle.write(str(child.pid))
        time.sleep(60)
    return 0


sys.exit(main())
'''

DEFAULT_INFO = {
    "provider": "piper",
    "voice": "en_US-amy-medium",
    "rate": 1.0,
    "maxChars": 0,
    "speaking": False,
    "junk": "not part of the contract",
    "providers": [
        {"name": "piper", "kind": "local", "maxChars": None, "maxBytes": None},
        {"name": "cloud", "kind": "cloud", "maxChars": 123456, "maxBytes": 654321},
    ],
}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.config_home = root / "config"
        self.config_home.mkdir()
        self.tmpdir = root / "tmp"
        self.tmpdir.mkdir()

        env = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "TMPDIR": str(self.tmpdir),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.stub = root / "stub-speak"
        self.stub.write_text(STUB_SPEAK, encoding="utf-8")
        self.stub.chmod(0o755)
        self.control = root / "control.json"
        self.calls = root / "calls.log"
        self.stdin_file = root / "stdin.txt"
        self.env_file = root / "env.json"
        os.environ["BRIDGE_STUB_CONTROL"] = str(self.control)
        os.environ["BRIDGE_STUB_CALLS"] = str(self.calls)
        os.environ["BRIDGE_STUB_STDIN"] = str(self.stdin_file)
        os.environ["BRIDGE_STUB_ENV"] = str(self.env_file)
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in (
            "BRIDGE_STUB_CONTROL", "BRIDGE_STUB_CALLS", "BRIDGE_STUB_STDIN",
            "BRIDGE_STUB_ENV")])
        self.set_control()

        manifest = root / "manifest.json"
        manifest.write_text('{"version":"9.9.9"}', encoding="utf-8")
        BRIDGE_MODULE.SPEAK = self.stub
        BRIDGE_MODULE.PLUGIN_MANIFEST = manifest
        BRIDGE_MODULE.reset_info_cache()

        self.token_path = self.config_home / "omarchy-tts" / "browser-token"
        self.token = BRIDGE_MODULE.ensure_token(self.token_path)

        self.server = BRIDGE_MODULE.build_server(
            "127.0.0.1", 0, token_path=self.token_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- helpers ------------------------------------------------------------

    def set_control(self, mode=None, info=None):
        payload = {"mode": mode, "info": DEFAULT_INFO if info is None else info}
        self.control.write_text(json.dumps(payload), encoding="utf-8")

    def request(self, method, path, body=None, token=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        request = urllib.request.Request(url, data=data, method=method)
        if token is not False:
            request.add_header("Authorization", f"Bearer {token or self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def call_count(self, arg):
        if not self.calls.exists():
            return 0
        return sum(
            1 for line in self.calls.read_text(encoding="utf-8").splitlines()
            if json.loads(line) == [arg]
        )

    # -- token handling -----------------------------------------------------

    def test_token_file_created_0600(self):
        path = self.config_home / "omarchy-tts" / "browser-token"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), self.token)
        self.assertEqual(
            stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_token_file_permissions_are_repaired(self):
        self.token_path.chmod(0o644)
        self.assertEqual(BRIDGE_MODULE.read_token(self.token_path), self.token)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_symlink_token_refused(self):
        target = self.root / "outside-token"
        target.write_text("attacker-token\n", encoding="utf-8")
        link = self.config_home / "omarchy-tts" / "browser-token-link"
        link.symlink_to(target)
        with self.assertRaises(BRIDGE_MODULE.TokenError):
            BRIDGE_MODULE.ensure_token(link)
        with self.assertRaises(BRIDGE_MODULE.TokenError):
            BRIDGE_MODULE.read_token(link)
        self.assertEqual(target.read_text(encoding="utf-8"), "attacker-token\n")

    # -- auth and CORS ------------------------------------------------------

    def test_missing_or_wrong_token_is_401(self):
        status, _headers, _body = self.request("GET", "/v1/health", token=False)
        self.assertEqual(status, 401)
        status, _headers, _body = self.request("GET", "/v1/health", token="wrong")
        self.assertEqual(status, 401)

    def test_web_origin_rejected(self):
        status, _headers, _body = self.request(
            "GET", "/v1/health", headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)

    def test_keep_alive_survives_early_auth_rejection_with_body(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            body = b"ignored-body"
            sock.sendall(
                b"POST /v1/health HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer wrong\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body
            )
            first = self._read_http_response(sock)
            self.assertIn(b" 401 ", first)
            self.assertNotIn(b"Connection: close", first)

            sock.sendall(
                b"GET /v1/health HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer %s\r\n\r\n" % self.token.encode("ascii")
            )
            second = self._read_http_response(sock)
            self.assertIn(b" 200 ", second)

    def test_oversized_body_closes_connection(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            length = BRIDGE_MODULE.MAX_BODY + 1
            sock.sendall(
                b"POST /v1/synthesize HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer %s\r\n"
                b"Content-Length: %d\r\n\r\n"
                % (self.token.encode("ascii"), length)
            )
            sock.sendall(b"x" * 65536)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data and b" 413 " in data:
                    break
            self.assertIn(b" 413 ", data)
            self.assertIn(b"Connection: close", data)

    @staticmethod
    def _read_http_response(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("connection closed before response headers")
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n")[1:]:
            key, _, value = line.partition(b":")
            if key.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(rest) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest

    def test_chrome_extension_origin_allowed(self):
        origin = "chrome-extension://abcdefghijklmnop"
        status, headers, _body = self.request(
            "GET", "/v1/health", headers={"Origin": origin})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertIn("Origin", headers.get("Vary", ""))

    def test_options_preflight(self):
        origin = "chrome-extension://abcdefghijklmnop"
        status, headers, body = self.request(
            "OPTIONS", "/v1/synthesize", token=False,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            })
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertEqual(headers.get("Access-Control-Allow-Private-Network"), "true")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(
            headers.get("Access-Control-Allow-Headers"),
            "authorization, content-type")

    # -- request limits -----------------------------------------------------

    def test_oversized_body_is_413(self):
        body = json.dumps({"text": "hi", "pad": "x" * (256 * 1024)})
        status, _headers, raw = self.request("POST", "/v1/synthesize", body=body)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(raw)["error"], "too_large")

    def test_oversized_text_is_413(self):
        status, _headers, raw = self.request(
            "POST", "/v1/synthesize", body={"text": "a" * 20001})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(raw)["error"], "too_large")

    def test_lone_surrogate_is_400(self):
        status, _headers, raw = self.request(
            "POST", "/v1/synthesize", body='{"text":"\\ud800"}')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "bad_request")

    def test_timeout_stops_the_whole_process_group(self):
        script = self.root / "hang-speak"
        child_pid_file = self.root / "timeout-child.pid"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "with open(os.environ['BRIDGE_TIMEOUT_CHILD_PID'], 'w') as handle:\n"
            "    handle.write(str(child.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        original = BRIDGE_MODULE.SPEAK
        BRIDGE_MODULE.SPEAK = script
        self.addCleanup(setattr, BRIDGE_MODULE, "SPEAK", original)
        env = dict(os.environ)
        env["BRIDGE_TIMEOUT_CHILD_PID"] = str(child_pid_file)

        with self.assertRaises(subprocess.TimeoutExpired):
            BRIDGE_MODULE._run_speak([], timeout=1, env=env)

        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        for _ in range(50):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("provider grandchild survived the synthesis timeout")

    def test_synthesis_timeout_stops_a_provider_that_escaped_setsid(self):
        self.set_control(mode="escape")
        pid_file = self.root / "escaped-provider.pid"
        os.environ["BRIDGE_STUB_ESCAPED_PID"] = str(pid_file)
        self.addCleanup(os.environ.pop, "BRIDGE_STUB_ESCAPED_PID", None)
        try:
            with mock.patch.object(BRIDGE_MODULE, "SYNTH_TIMEOUT", 1):
                status, _headers, raw = self.request(
                    "POST", "/v1/synthesize", body={"text": "hang"})
            self.assertEqual(status, 504)
            self.assertEqual(json.loads(raw)["error"], "timeout")
            self.assertEqual(self.call_count("--stop"), 1)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(50):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)
            self.fail("escaped provider survived the synthesis timeout")
        finally:
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), 9)
            except (OSError, ValueError):
                pass

    # -- synthesis ----------------------------------------------------------

    def test_pcm_sidecar_becomes_wav(self):
        self.set_control(mode="pcm")
        status, headers, body = self.request(
            "POST", "/v1/synthesize", body={"text": "Hello world"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(headers.get("X-Tts-Format"), "wav")
        self.assertEqual(headers.get("X-Tts-Provider"), "piper")
        self.assertEqual(headers.get("X-Tts-Voice"), "en_US-amy-medium")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(body[0:4], b"RIFF")
        self.assertEqual(body[8:12], b"WAVE")
        self.assertEqual(struct.unpack_from("<H", body, 22)[0], 1)
        self.assertEqual(struct.unpack_from("<I", body, 24)[0], 22050)
        self.assertEqual(struct.unpack_from("<I", body, 40)[0], 200)
        self.assertEqual(len(body), 44 + 200)
        self.assertEqual(struct.unpack_from("<I", body, 4)[0], len(body) - 8)
        # speak receives the text on stdin with the silent path disabled.
        self.assertEqual(self.stdin_file.read_text(encoding="utf-8"), "Hello world")
        stub_env = json.loads(self.env_file.read_text(encoding="utf-8"))
        self.assertIsNone(stub_env["silent"])

    def test_pcm_sidecar_trims_to_block_alignment(self):
        self.set_control(mode="pcm_stereo")
        status, _headers, body = self.request(
            "POST", "/v1/synthesize", body={"text": "stereo"})
        self.assertEqual(status, 200)
        self.assertEqual(struct.unpack_from("<H", body, 22)[0], 2)
        self.assertEqual(struct.unpack_from("<I", body, 24)[0], 44100)
        self.assertEqual(struct.unpack_from("<I", body, 40)[0], 100)
        self.assertEqual(len(body), 144)
        self.assertEqual(struct.unpack_from("<I", body, 4)[0], len(body) - 8)

    def test_file_passthrough_mp3(self):
        self.set_control(mode="mp3")
        status, headers, body = self.request(
            "POST", "/v1/synthesize", body={"text": "mp3 please"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(headers.get("X-Tts-Format"), "mp3")
        self.assertTrue(body.startswith(b"ID3"))

    def test_file_passthrough_wav(self):
        self.set_control(mode="wav")
        status, headers, body = self.request(
            "POST", "/v1/synthesize", body={"text": "wav please"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(body[0:4], b"RIFF")

    def test_empty_audio_is_204(self):
        self.set_control(mode="empty")
        status, _headers, body = self.request(
            "POST", "/v1/synthesize", body={"text": "nothing to say"})
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_nonzero_speak_is_502_without_text(self):
        self.set_control(mode="fail")
        secret = "the selected sentence must never leak"
        status, _headers, raw = self.request(
            "POST", "/v1/synthesize", body={"text": secret})
        self.assertEqual(status, 502)
        payload = json.loads(raw)
        self.assertEqual(payload["error"], "synthesis_failed")
        self.assertNotIn(secret, raw.decode("utf-8"))
        self.assertNotIn("the selected sentence", payload["message"])
        self.assertNotIn("\n", payload["message"])
        self.assertLessEqual(len(payload["message"]), 400)

    def test_flac_is_unsupported(self):
        self.set_control(mode="flac")
        status, _headers, raw = self.request(
            "POST", "/v1/synthesize", body={"text": "flac"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(raw)["error"], "unsupported_audio")

    # -- info and health ----------------------------------------------------

    def test_info_trimming(self):
        info = dict(DEFAULT_INFO)
        info["provider"] = "cloud"
        self.set_control(info=info)
        status, _headers, raw = self.request("GET", "/v1/info")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(
            set(payload),
            {"provider", "voice", "rate", "maxChars", "limits", "bridgeVersion"})
        self.assertEqual(payload["provider"], "cloud")
        self.assertEqual(payload["limits"], {"maxChars": 123456, "maxBytes": 654321})
        self.assertEqual(payload["bridgeVersion"], BRIDGE_MODULE.BRIDGE_VERSION)

    def test_info_uses_plugin_caps_without_provider_limits(self):
        status, _headers, raw = self.request("GET", "/v1/info")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["limits"], {"maxChars": 200000, "maxBytes": 1048576})

    def test_info_is_cached(self):
        self.request("GET", "/v1/info")
        self.request("GET", "/v1/info")
        self.assertEqual(self.call_count("--info"), 1)

    def test_health(self):
        status, _headers, raw = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["bridgeVersion"], BRIDGE_MODULE.BRIDGE_VERSION)
        self.assertEqual(payload["pluginVersion"], "9.9.9")
        self.assertFalse(payload["speaking"])

    def test_stop(self):
        status, _headers, raw = self.request("POST", "/v1/stop", body=b"")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True})
        self.assertEqual(self.call_count("--stop"), 1)

    def test_unknown_method_is_405(self):
        status, _headers, _body = self.request("PUT", "/v1/health")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
