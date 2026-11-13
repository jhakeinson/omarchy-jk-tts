#!/usr/bin/env python3
"""Small integration checks for the public CLI/config boundary."""
import base64
import importlib.machinery
import importlib.util
import hashlib
import json
import shlex
import uuid
import zipfile
import shutil
import os
import re
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import threading
import io
import contextlib

ROOT = Path(__file__).resolve().parents[1]
SPEAK = ROOT / "bin" / "speak"
BINDINGS = ROOT / "bin" / "speak-bindings"
SETUP = ROOT / "bin" / "speak-setup"


def load_setup_module():
    """Load the extensionless setup helper without invoking its CLI."""
    name = f"_omarchy_tts_setup_test_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(SETUP))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    previous_umask = os.umask(0)
    os.umask(previous_umask)
    try:
        loader.exec_module(module)
    finally:
        os.umask(previous_umask)
    return module


def stub_trusted_helper_path(setup):
    """A distribution-installed real file to stand in for PKEXEC / PACMAN.

    /usr/bin/env is only a reasonable stand-in where it is a regular file;
    newer runner images symlink it, so take the first candidate that passes
    the trusted-path rules on the machine running the tests."""
    for candidate in ("/usr/bin/env", "/usr/bin/bash", "/usr/bin/true"):
        try:
            if setup.trusted_system_executable(Path(candidate)) == candidate:
                return Path(candidate)
        except RuntimeError:
            continue
    raise AssertionError("no distribution-installed executable found to stub")


DAEMON = ROOT / "bin" / "speak-daemon"


def load_daemon_module():
    """Load the resident synthesis daemon without invoking its CLI."""
    name = f"_omarchy_tts_daemon_test_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(DAEMON))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CliConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "XDG_CONFIG_HOME": self.temp.name,
                    "XDG_CACHE_HOME": self.temp.name,
                    "XDG_DATA_HOME": self.temp.name,
                    "XDG_RUNTIME_DIR": self.temp.name,
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.env.pop("OPENAI_API_KEY", None)
        self.env.pop("ELEVENLABS_API_KEY", None)
        self.env.pop("GOOGLE_API_KEY", None)
        self.env.pop("GEMINI_API_KEY", None)
        self.env.pop("OMARCHY_TTS_SETUP_LOCK_FD", None)
        self.env.pop("OMARCHY_TTS_DOWNLOAD_LOCK_FD", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_speak(self, *args, input_text=None, check=True):
        return subprocess.run([SPEAK, *args], input=input_text, text=True,
                              capture_output=True, env=self.env, check=check)

    def test_fresh_info_creates_complete_private_config(self):
        info = json.loads(self.run_speak("--info").stdout)
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        settings = json.loads(config.read_text())
        self.assertEqual(info["provider"], "piper")
        self.assertTrue(info["sanitizer"]["stripMarkdown"])
        self.assertNotIn("voiceId", settings["elevenlabs"])
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_partial_provider_objects_receive_missing_defaults(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"openai": {"voice": "nova"},
                                      "elevenlabs": {}}))
        self.run_speak("--info")
        settings = json.loads(config.read_text())
        self.assertEqual(settings["openai"]["voice"], "nova")
        self.assertEqual(settings["openai"]["model"], "gpt-4o-mini-tts")
        self.assertEqual(settings["elevenlabs"]["model"], "eleven_flash_v2_5")
        self.assertNotIn("voiceId", settings["elevenlabs"])

    def test_previous_default_elevenlabs_model_migrates_once(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"schemaVersion": 2,
                                      "elevenlabs": {"model": "eleven_turbo_v2_5"}}))
        self.run_speak("--info")
        settings = json.loads(config.read_text())
        self.assertEqual(settings["schemaVersion"], 4)
        self.assertEqual(settings["elevenlabs"]["model"], "eleven_flash_v2_5")

    def test_help_does_not_require_or_create_writable_state(self):
        blocked = Path(self.temp.name, "blocked")
        blocked.write_text("not a directory")
        env = {**self.env, "XDG_CONFIG_HOME": str(blocked),
               "XDG_CACHE_HOME": str(blocked), "XDG_RUNTIME_DIR": str(blocked)}
        result = subprocess.run([SPEAK, "--help"], env=env, check=True,
                                text=True, capture_output=True)
        self.assertIn("Usage: speak", result.stdout)

    def test_combined_state_watch_emits_initial_and_config_changes(self):
        watcher = subprocess.Popen(
            [SPEAK, "--watch-state"], env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(select.select([watcher.stdout], [], [], 3)[0])
            initial = json.loads(watcher.stdout.readline())
            self.assertEqual(initial, {"status": "idle", "provider": "piper"})

            self.run_speak("--set", ".provider", "kokoro")
            self.assertTrue(select.select([watcher.stdout], [], [], 3)[0])
            changed = json.loads(watcher.stdout.readline())
            self.assertEqual(changed["provider"], "kokoro")
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
            watcher.stdout.close()
            watcher.stderr.close()

    def test_info_never_contacts_a_cloud_provider(self):
        marker = Path(self.temp.name, "curl-was-run")
        tools = Path(self.temp.name, "tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(f"#!/usr/bin/env bash\nprintf touched > {marker}\nexit 99\n")
        curl.chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        self.env["ELEVENLABS_API_KEY"] = "safe-test-key"
        self.run_speak("--info")
        self.assertFalse(marker.exists(), "a read-only state query made a network request")

    def test_cloud_voice_refresh_is_explicit_private_and_normalized(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        provider = providers / "cloudtest"
        provider.write_text(
            "#!/usr/bin/env bash\n"
            "# desc: test cloud voice catalogue\n"
            "# kind: cloud\n"
            "[[ ${1:-} == --voices ]] || exit 2\n"
            "printf '%s\\n' '[{\"value\":\"b\",\"label\":\"Zulu\"},"
            "{\"value\":\"a\",\"label\":\"alpha\"},"
            "{\"value\":\"a\",\"label\":\"duplicate\"}]'\n"
        )
        provider.chmod(0o755)

        result = self.run_speak("--refresh-voices", "cloudtest")
        self.assertEqual(json.loads(result.stdout),
                         {"provider": "cloudtest", "count": 2})
        cache = Path(self.temp.name, "omarchy-tts", "voices", "cloudtest.json")
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(cache.read_text()), [
            {"value": "a", "label": "alpha"},
            {"value": "b", "label": "Zulu"},
        ])

    def test_first_elevenlabs_refresh_selects_an_account_voice_once(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        provider = providers / "elevenlabs"
        provider.write_text(
            "#!/usr/bin/env bash\n"
            "# desc: account voices\n# kind: cloud\n# voices: remote\n"
            "[[ ${1:-} == --voices ]] || exit 2\n"
            "printf '%s\\n' '[{\"value\":\"voice-z\",\"label\":\"Zulu\"},"
            "{\"value\":\"voice-a\",\"label\":\"Alpha\"}]'\n"
        )
        provider.chmod(0o755)

        self.run_speak("--refresh-voices", "elevenlabs")
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        self.assertEqual(json.loads(config.read_text())["elevenlabs"]["voiceId"],
                         "voice-a")
        self.run_speak("--set", ".elevenlabs.voiceId", "voice-z")
        self.run_speak("--refresh-voices", "elevenlabs")
        self.assertEqual(json.loads(config.read_text())["elevenlabs"]["voiceId"],
                         "voice-z", "refresh replaced an explicit user choice")

    def test_openai_static_metadata_does_not_require_a_key(self):
        provider_env = {**self.env,
                        "TTS_PLUGIN_DIR": str(ROOT),
                        "TTS_CONFIG": str(Path(self.temp.name, "missing.json")),
                        "TTS_METRICS_FILE": str(Path(self.temp.name, "missing-metrics.json"))}
        empty_path = Path(self.temp.name, "empty-path")
        empty_path.mkdir()
        provider_env["PATH"] = f"{empty_path}:/usr/bin:/bin"
        # A fake failing curl proves these local metadata operations never
        # attempt to invoke it; curl itself may exist in the base test image.
        fake_curl = empty_path / "curl"
        fake_curl.write_text("#!/usr/bin/env bash\nexit 99\n")
        fake_curl.chmod(0o755)
        voices = subprocess.run([ROOT / "providers" / "openai", "--voices"],
                                env=provider_env, check=True, text=True,
                                capture_output=True)
        self.assertIn("cedar", {item["value"] for item in json.loads(voices.stdout)})
        usage = subprocess.run([ROOT / "providers" / "openai", "--usage"],
                               env=provider_env, check=True, text=True,
                               capture_output=True)
        self.assertEqual(json.loads(usage.stdout)["account"]["source"],
                         "request_headers")

    def test_image_only_clipboard_is_ocrd_before_speech(self):
        tools = Path(self.temp.name, "tools")
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        ocr = Path(self.temp.name, "omarchy-tts", "ocr")
        tools.mkdir()
        providers.mkdir(parents=True)
        ocr.mkdir(parents=True)
        captured = Path(self.temp.name, "spoken-text")

        wl_paste = tools / "wl-paste"
        wl_paste.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ ${1:-} == --list-types ]]; then\n"
            "  [[ ${MIXED_CLIPBOARD:-0} == 1 ]] && printf 'text/plain;charset=utf-8\\n'\n"
            "  printf 'image/png\\n'\n"
            "elif [[ $* == *text/plain* ]]; then printf 'Preferred clipboard text.'\n"
            "else printf 'fake-png-bytes'; fi\n"
        )
        wl_paste.chmod(0o755)
        ocr_bin = ocr / "testocr"
        ocr_bin.write_text(
            "#!/usr/bin/env bash\n# desc: test OCR\ncat >/dev/null\n"
            "printf 'Words recovered from the image.\\n'\n"
        )
        ocr_bin.chmod(0o755)
        provider = providers / "capturetest"
        provider.write_text(
            "#!/usr/bin/env bash\n# desc: capture speech input\n# kind: local\n"
            "cat > \"$CAPTURED_TEXT\"\n"
        )
        provider.chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        self.env["CAPTURED_TEXT"] = str(captured)
        self.run_speak("--set", ".ocr.engine", "testocr")

        self.run_speak("--clipboard", "--provider", "capturetest")
        self.assertEqual(captured.read_text().strip(),
                         "Words recovered from the image.")

        self.env["MIXED_CLIPBOARD"] = "1"
        self.run_speak("--clipboard", "--provider", "capturetest")
        self.assertEqual(captured.read_text().strip(),
                         "Preferred clipboard text.")

    def test_screen_capture_never_falls_back_to_all_monitors(self):
        tools = Path(self.temp.name, "screen-tools")
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        tools.mkdir()
        providers.mkdir(parents=True)
        grim_marker = Path(self.temp.name, "grim-ran")
        hyprctl = tools / "hyprctl"
        hyprctl.write_text("#!/usr/bin/env bash\nprintf '[]\\n'\n")
        hyprctl.chmod(0o755)
        grim = tools / "grim"
        grim.write_text(
            "#!/usr/bin/env bash\nprintf ran > \"$GRIM_MARKER\"\nprintf fake-image\n"
        )
        grim.chmod(0o755)
        provider = providers / "capturetest"
        provider.write_text(
            "#!/usr/bin/env bash\n# desc: capture speech input\n# kind: local\n"
            "cat >/dev/null\n"
        )
        provider.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "GRIM_MARKER": str(grim_marker)}
        result = subprocess.run(
            [SPEAK, "--screen", "--provider", "capturetest"], env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("focused monitor", result.stderr)
        self.assertFalse(grim_marker.exists())

    def test_invalid_config_is_preserved_before_recovery(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        config.write_text('{"provider":')
        result = self.run_speak("--info")
        self.assertEqual(json.loads(result.stdout)["provider"], "piper")
        preserved = list(config.parent.glob("config.json.invalid.*"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), '{"provider":')
        self.assertEqual(preserved[0].stat().st_mode & 0o777, 0o600)

    def test_structurally_invalid_nested_config_is_preserved_and_repaired(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        original = {"provider": "piper", "openai": [],
                    "ocr": {"engine": "custom", "custom": "broken"}}
        config.write_text(json.dumps(original))
        result = self.run_speak("--info")
        self.assertEqual(result.returncode, 0, result.stderr)
        repaired = json.loads(config.read_text())
        self.assertIsInstance(repaired["openai"], dict)
        self.assertIsInstance(repaired["ocr"]["custom"], dict)
        preserved = list(config.parent.glob("config.json.invalid.*"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(json.loads(preserved[0].read_text()), original)

    def test_config_updates_preserve_a_dotfile_symlink(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        target = Path(self.temp.name, "dotfiles", "tts.json")
        config.parent.mkdir(parents=True)
        target.parent.mkdir()
        target.write_text("{}\n")
        config.symlink_to(target)

        self.run_speak("--set", ".rate", ".25")
        self.assertTrue(config.is_symlink())
        self.assertEqual(json.loads(target.read_text())["rate"], 0.25)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_dangling_config_symlink_is_refused_without_replacement(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        target = Path(self.temp.name, "missing", "tts.json")
        config.symlink_to(target)
        result = self.run_speak("--info", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(config.is_symlink())
        self.assertFalse(target.exists())

    def test_non_file_config_paths_are_refused_without_moving_them(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.mkdir(parents=True)
        marker = config / "keep"
        marker.write_text("unchanged")
        result = self.run_speak("--info", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(config.is_dir())
        self.assertEqual(marker.read_text(), "unchanged")

    def test_config_symlink_to_a_directory_is_refused_without_moving_target(self):
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True)
        target = Path(self.temp.name, "managed-config-directory")
        target.mkdir()
        marker = target / "keep"
        marker.write_text("unchanged")
        config = base / "config.json"
        config.symlink_to(target, target_is_directory=True)
        result = self.run_speak("--info", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(config.is_symlink())
        self.assertEqual(marker.read_text(), "unchanged")

    def test_noop_config_write_preserves_inode_and_mtime(self):
        self.run_speak("--info")
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        before = config.stat()
        self.run_speak("--set", ".rate", "1.0")
        after = config.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_control_commands_work_without_parsing_damaged_config(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        config.write_text('{"provider":')
        self.assertEqual(self.run_speak("--status").stdout.strip(), "idle")
        self.run_speak("--stop")
        self.assertEqual(config.read_text(), '{"provider":')
        self.assertEqual(list(config.parent.glob("config.json.invalid.*")), [])

    def test_stdin_json_protocol_preserves_strings_and_rejects_other_json(self):
        sample = '  true, "$HOME", backslash \\ and café  '
        saved = self.run_speak("--set-stdin-json", ".ui.sampleText",
                               input_text=json.dumps(sample) + "\n")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(json.loads(self.run_speak("--info").stdout)["ui"]["sampleText"],
                         sample)
        preview = self.run_speak("--preview-stdin-json",
                                 input_text=json.dumps("true") + "\n")
        self.assertEqual(preview.stdout, "true")
        for encoded in ('{"not":"a string"}\n', '"nul\\u0000byte"\n'):
            with self.subTest(encoded=encoded):
                self.assertNotEqual(
                    self.run_speak("--set-stdin-json", ".ui.sampleText",
                                   input_text=encoded, check=False).returncode,
                    0,
                )

    def test_input_and_length_limits_are_enforced_before_a_provider_runs(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        marker = Path(self.temp.name, "limit-provider-ran")
        provider = providers / "limitcheck"
        provider.write_text(
            "#!/usr/bin/env bash\n# kind: local\n# probe: true\n"
            "printf ran > \"$LIMIT_MARKER\"\ncat >/dev/null\n"
        )
        provider.chmod(0o755)
        self.env["LIMIT_MARKER"] = str(marker)
        result = self.run_speak("--raw", "--provider", "limitcheck",
                                input_text="x" * 1_048_577, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 MiB", result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotEqual(
            self.run_speak("--max-chars", "1048577", "hello", check=False).returncode,
            0,
        )

    def test_sanitizer_failure_is_not_misreported_as_empty_input(self):
        tools = Path(self.temp.name, "broken-sanitizer-tools")
        tools.mkdir()
        python = tools / "python3"
        python.write_text("#!/usr/bin/env bash\nexit 70\n")
        python.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}"}
        result = subprocess.run([SPEAK, "hello"], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("text preparation failed", result.stderr)

    def test_persisted_rate_is_clamped_but_explicit_unsupported_rate_fails(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        marker = Path(self.temp.name, "effective-rate")
        provider = providers / "narrowrate"
        provider.write_text(
            "#!/usr/bin/env bash\n# kind: local\n# probe: true\n"
            "# ratemin: 0.7\n# ratemax: 1.2\n"
            "cat >/dev/null\nprintf '%s' \"$TTS_RATE\" > \"$RATE_MARKER\"\n"
        )
        provider.chmod(0o755)
        self.env["RATE_MARKER"] = str(marker)
        self.run_speak("--set", ".rate", "4")
        self.run_speak("--raw", "--provider", "narrowrate", "hello")
        self.assertEqual(marker.read_text(), "1.2")
        marker.unlink()
        result = self.run_speak("--raw", "--provider", "narrowrate",
                                "--rate", "4", "hello", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0.7 to 1.2", result.stderr)
        self.assertFalse(marker.exists())

    def test_command_line_overrides_are_validated(self):
        for args in (("--rate", "fast", "hello"),
                     ("--rate", "3", "hello"),
                     ("--max-chars", "-1", "hello"),
                     ("--provider", "../bad", "hello")):
            self.assertNotEqual(self.run_speak(*args, check=False).returncode, 0)

    def test_google_limit_is_measured_in_utf8_bytes(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        marker = Path(self.temp.name, "provider-ran")
        provider = providers / "bytetest"
        provider.write_text(
            "#!/usr/bin/env bash\n# desc: byte limit test\n# kind: local\n"
            "# probe: true\n# maxbytes: 5\ncat >/dev/null\nprintf ran > \"$BYTE_MARKER\"\n"
        )
        provider.chmod(0o755)
        self.env["BYTE_MARKER"] = str(marker)
        result = self.run_speak("--raw", "--provider", "bytetest", "你好", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("5 UTF-8 bytes", result.stderr)
        self.assertFalse(marker.exists())

    def test_provider_limits_are_compared_as_decimal_metadata(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        marker = Path(self.temp.name, "provider-ran")
        provider = providers / "decimaltest"
        provider.write_text(
            "#!/usr/bin/env bash\n# desc: decimal limit test\n# kind: local\n"
            "# probe: true\n# maxchars: 08\ncat >/dev/null\n"
            "printf ran > \"$DECIMAL_MARKER\"\n"
        )
        provider.chmod(0o755)
        self.env["DECIMAL_MARKER"] = str(marker)
        result = self.run_speak("--raw", "--provider", "decimaltest",
                                "123456789", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at most 08 characters", result.stderr)
        self.assertFalse(marker.exists())

    def test_removed_system_providers_migrate_to_piper(self):
        """espeak-ng and Speech Dispatcher were dropped in 1.3.0. A config that
        still names one lands on the default instead of an absent provider."""
        for old in ("espeak-ng", "spd"):
            config = Path(self.temp.name, "omarchy-tts", "config.json")
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"schemaVersion": 2, "provider": old}))
            info = json.loads(self.run_speak("--info").stdout)
            self.assertEqual(info["provider"], "piper", old)
            self.assertNotIn(old, {item["name"] for item in info["providers"]})

    def test_invalid_optional_provider_metadata_cannot_break_info(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        provider = providers / "custommeta"
        provider.write_text(
            "#!/usr/bin/env bash\n# kind: cloud\n# keyname: custom\n"
            "# keyenv: NOT-A-VALID-NAME\n# maxchars: many\n# maxbytes: lots\n"
            "cat >/dev/null\n"
        )
        provider.chmod(0o755)
        info = json.loads(self.run_speak("--info").stdout)
        custom = next(item for item in info["providers"]
                      if item["name"] == "custommeta")
        self.assertIsNone(custom["keyEnv"])
        self.assertIsNone(custom["maxChars"])
        self.assertIsNone(custom["maxBytes"])

    def test_state_watch_reports_the_provider_actually_speaking(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        provider = providers / "override"
        provider.write_text("#!/usr/bin/env bash\n# kind: local\n# probe: true\ncat >/dev/null\nsleep 30\n")
        provider.chmod(0o755)
        watcher = subprocess.Popen(
            [SPEAK, "--watch-state"], env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        speaking = None
        speech = None
        try:
            self.assertTrue(select.select([watcher.stdout], [], [], 3)[0])
            watcher.stdout.readline()
            speech = subprocess.Popen(
                [SPEAK, "--provider", "override", "hello"], env=self.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if select.select([watcher.stdout], [], [], 0.25)[0]:
                    candidate = json.loads(watcher.stdout.readline())
                    if candidate["status"] == "speaking":
                        speaking = candidate
                        break
            self.assertEqual(speaking, {"status": "speaking", "provider": "override"})
            self.run_speak("--stop")
            speech.wait(timeout=3)
        finally:
            if speech is not None and speech.poll() is None:
                self.run_speak("--stop", check=False)
                speech.wait(timeout=3)
            watcher.terminate()
            watcher.wait(timeout=3)
            watcher.stdout.close()
            watcher.stderr.close()

    def test_stop_cancels_selection_preparation_before_provider_start(self):
        tools = Path(self.temp.name, "preparation-tools")
        tools.mkdir()
        started = Path(self.temp.name, "selection-started")
        provider_marker = Path(self.temp.name, "provider-started")
        wl_paste = tools / "wl-paste"
        wl_paste.write_text(
            "#!/usr/bin/env bash\n"
            "printf started > \"$SELECTION_MARKER\"\n"
            "sleep 30\nprintf 'too late'\n"
        )
        wl_paste.chmod(0o755)
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True)
        provider = providers / "neverstart"
        provider.write_text(
            "#!/usr/bin/env bash\n# kind: local\n# probe: true\n"
            "printf started > \"$PROVIDER_MARKER\"\ncat >/dev/null\n"
        )
        provider.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "SELECTION_MARKER": str(started),
               "PROVIDER_MARKER": str(provider_marker)}
        speech = subprocess.Popen(
            [SPEAK, "--selection", "--provider", "neverstart"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.01)
            self.assertTrue(started.exists(), "selection capture never entered preparation")
            stopped = subprocess.run([SPEAK, "--stop"], env=env,
                                     capture_output=True, text=True, timeout=3)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            speech.wait(timeout=3)
            self.assertFalse(provider_marker.exists())
            status = subprocess.run([SPEAK, "--status"], env=env,
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(status.stdout.strip(), "idle")
        finally:
            if speech.poll() is None:
                subprocess.run([SPEAK, "--stop"], env=env, capture_output=True)
                speech.wait(timeout=3)

    def test_concurrent_config_updates_do_not_overwrite_each_other(self):
        self.run_speak("--info")
        for _ in range(5):
            first = subprocess.Popen([SPEAK, "--set", ".rate", "1.25"],
                                     env=self.env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            second = subprocess.Popen([SPEAK, "--set", ".maxChars", "321"],
                                      env=self.env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            self.assertEqual(first.wait(timeout=3), 0)
            self.assertEqual(second.wait(timeout=3), 0)
            info = json.loads(self.run_speak("--info").stdout)
            self.assertEqual((info["rate"], info["maxChars"]), (1.25, 321))

    def test_set_accepts_known_path_and_rejects_unknown_path(self):
        self.run_speak("--set", ".rate", "1.25")
        self.assertEqual(json.loads(self.run_speak("--info").stdout)["rate"], 1.25)
        result = self.run_speak("--set", ".apiKeys.openai", "secret", check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_plaintext_config_api_keys_are_not_accepted(self):
        self.run_speak("--info")
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        data = json.loads(config.read_text())
        data["apiKeys"] = {"openai": "plaintext-must-not-be-used"}
        config.write_text(json.dumps(data))
        info = json.loads(self.run_speak("--info").stdout)
        openai = next(item for item in info["providers"] if item["name"] == "openai")
        # The property under test is that a plaintext key is never the source,
        # not that no key exists. Asserting "none" only held on a machine with
        # an empty keyring: it passed in CI and failed for anyone who had
        # actually stored a key, which is precisely who this protects.
        self.assertNotEqual(openai["keySource"], "config",
                            "a plaintext key in config.json was accepted")
        self.assertIn(openai["keySource"], ("none", "keyring", "env", "keyerror"))
        if openai["keySource"] == "none":
            self.assertEqual(openai["status"], "nokey")

    def test_invalid_multiline_environment_key_is_not_reported_as_usable(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True, exist_ok=True)
        provider = providers / "invalid-env"
        provider.write_text(
            "#!/usr/bin/env bash\n"
            "# kind: cloud\n"
            "# keyname: invalid-env\n"
            "# keyenv: INVALID_ENV_API_KEY\n"
            "# probe: true\n"
            "cat >/dev/null\n"
        )
        provider.chmod(0o755)
        self.env["INVALID_ENV_API_KEY"] = "first-line\nsecond-line"
        info = json.loads(self.run_speak("--info").stdout)
        entry = next(item for item in info["providers"]
                     if item["name"] == "invalid-env")
        # The malformed environment value is ignored. This test deliberately
        # points DBus at an unavailable keyring, which is an operational error
        # rather than proof that no stored key exists.
        self.assertEqual(entry["keySource"], "keyerror")
        self.assertEqual(entry["status"], "keyerror")

    def test_preview_uses_persisted_sanitizer_options(self):
        self.run_speak("--set", ".sanitizer.urls", "link")
        result = self.run_speak("--preview-text", "See https://example.com in 2s")
        self.assertEqual(result.stdout, "See link in 2 seconds")

    def test_browser_bridge_toggle_is_persisted_and_validated(self):
        self.run_speak("--set", ".browser.enabled", "true")
        info = json.loads(self.run_speak("--info").stdout)
        self.assertTrue(info["browser"]["enabled"])
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        self.assertTrue(json.loads(config.read_text())["browser"]["enabled"])
        self.run_speak("--set", ".browser.enabled", "false")
        self.assertFalse(json.loads(self.run_speak("--info").stdout)["browser"]["enabled"])
        for bad in ("yes", "1", "True", ""):
            result = self.run_speak("--set", ".browser.enabled", bad, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_malformed_browser_object_is_normalized_not_fatal(self):
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"browser": {"enabled": "banana"}}))
        self.run_speak("--info")
        settings = json.loads(config.read_text())
        self.assertEqual(settings["browser"], {"enabled": False})

    def test_restored_tab_can_reach_the_browser_tab(self):
        self.run_speak("--set", ".ui.lastTab", "6")
        self.assertEqual(json.loads(self.run_speak("--info").stdout)["ui"]["lastTab"], 6)
        result = self.run_speak("--set", ".ui.lastTab", "7", check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_info_never_carries_the_browser_token(self):
        token_path = Path(self.temp.name, "omarchy-tts", "browser-token")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("secret-browser-token-value\n")
        self.run_speak("--set", ".browser.enabled", "true")
        info = self.run_speak("--info").stdout
        self.assertNotIn("secret-browser-token-value", info)
        self.assertNotIn("token", json.loads(info)["browser"])

    def test_binding_manager_owns_only_its_marked_block(self):
        hypr = Path(self.temp.name, "hypr")
        hypr.mkdir()
        target = hypr / "bindings.lua"
        target.write_text('o.bind("SUPER + B", "Browser", "browser")\n')
        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        installed = target.read_text()
        self.assertIn("-- >>> omarchy-tts bindings", installed)
        self.assertIn('"Browser"', installed)
        subprocess.run([BINDINGS, "remove"], env=self.env, check=True,
                       capture_output=True, text=True)
        self.assertEqual(target.read_text().strip(),
                         'o.bind("SUPER + B", "Browser", "browser")')

    def test_managed_bindings_follow_the_stable_plugin_symlink(self):
        plugin = Path(self.temp.name, "omarchy", "plugins",
                      "io.github.jhakeinson.tts")
        first = Path(self.temp.name, "checkout-a")
        second = Path(self.temp.name, "checkout-b")
        for checkout in (first, second):
            (checkout / "bin").mkdir(parents=True)
            (checkout / "bin" / "speak").write_text("#!/bin/sh\n")
        plugin.parent.mkdir(parents=True)
        plugin.symlink_to(first, target_is_directory=True)

        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        target = Path(self.temp.name, "hypr", "bindings.lua")
        rendered = target.read_text()
        self.assertIn(str(plugin / "bin" / "speak"), rendered)
        self.assertNotIn(str(first / "bin" / "speak"), rendered)

        plugin.unlink()
        plugin.symlink_to(second, target_is_directory=True)
        self.assertEqual((plugin / "bin" / "speak").resolve(),
                         (second / "bin" / "speak").resolve())
        self.assertIn(str(plugin / "bin" / "speak"), target.read_text())

    def test_binding_backups_are_bounded(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        target.write_text("-- Personal keybindings\n")
        for _ in range(6):
            subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                           capture_output=True, text=True)
        self.assertEqual(len(list(target.parent.glob("bindings.lua.tts.bak.*"))), 3)

    def test_binding_remove_is_idempotent_and_does_not_create_a_file(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        subprocess.run([BINDINGS, "remove"], env=self.env, check=True,
                       capture_output=True, text=True)
        self.assertFalse(target.exists())

    def test_binding_manager_adopts_legacy_documented_bindings(self):
        hypr = Path(self.temp.name, "hypr")
        hypr.mkdir()
        target = hypr / "bindings.lua"
        target.write_text('o.bind("SUPER + ALT + E", "Speak selection", "speak --toggle")\n')
        status = subprocess.run([BINDINGS, "status"], env=self.env, check=True,
                                capture_output=True, text=True)
        self.assertTrue(json.loads(status.stdout)["canInstall"])
        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        installed = target.read_text()
        self.assertEqual(installed.count("Speak selection"), 1)
        self.assertIn("-- >>> omarchy-tts bindings", installed)

    def test_binding_manager_rejects_non_chord_input(self):
        for chord in ('SUPER + E\n")\nos.execute("bad")',
                      "SUPER + A + B", "SUPER + SUPER + E", "SUPER"):
            result = subprocess.run(
                [BINDINGS, "set", "selection", chord],
                env=self.env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_binding_config_persists_choices_not_derived_commands(self):
        config = Path(self.temp.name, "omarchy-tts", "bindings.json")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({
            "selection": {"chord": "SUPER + ALT + Q",
                          "label": "stale", "command": "/old/plugin/speak --toggle"}
        }))
        status = subprocess.run([BINDINGS, "status"], env=self.env, check=True,
                                capture_output=True, text=True)
        binding = json.loads(status.stdout)["bindings"]["selection"]
        self.assertEqual(binding["chord"], "SUPER + ALT + Q")
        self.assertNotEqual(binding["command"], "/old/plugin/speak --toggle")
        subprocess.run([BINDINGS, "set", "selection", "SUPER + ALT + Z"],
                       env=self.env, check=True, capture_output=True, text=True)
        stored = json.loads(config.read_text())
        self.assertEqual(stored["selection"], {"chord": "SUPER + ALT + Z"})

    def test_concurrent_binding_edits_preserve_both_choices(self):
        first = subprocess.Popen(
            [BINDINGS, "set", "selection", "SUPER + ALT + Z"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            [BINDINGS, "set", "clipboard", "SUPER + ALT + V"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        first_rc = first.wait(timeout=3)
        second_rc = second.wait(timeout=3)
        first_error = first.stderr.read()
        second_error = second.stderr.read()
        first.stderr.close()
        second.stderr.close()
        self.assertEqual(first_rc, 0, first_error)
        self.assertEqual(second_rc, 0, second_error)
        config = Path(self.temp.name, "omarchy-tts", "bindings.json")
        stored = json.loads(config.read_text())
        self.assertEqual(stored["selection"]["chord"], "SUPER + ALT + Z")
        self.assertEqual(stored["clipboard"]["chord"], "SUPER + ALT + V")

    def test_binding_manager_detects_active_default_conflicts(self):
        tools = Path(self.temp.name, "tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == binds ]]; then\n"
            "  printf '%s\\n' '[{\"modmask\":72,\"key\":\"E\",\"dispatcher\":\"exec\",\"arg\":\"editor\"}]'\n"
            "else exit 0; fi\n"
        )
        hyprctl.chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        self.env["HYPRLAND_INSTANCE_SIGNATURE"] = "test"
        status = subprocess.run([BINDINGS, "status"], env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertIn("SUPER + ALT + E", json.loads(status.stdout)["conflicts"])

    def test_binding_manager_fails_closed_when_active_bindings_are_unreadable(self):
        tools = Path(self.temp.name, "broken-hypr-tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text("#!/usr/bin/env bash\nexit 1\n")
        hyprctl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "HYPRLAND_INSTANCE_SIGNATURE": "test"}
        result = subprocess.run([BINDINGS, "install"], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inspect active Hyprland shortcuts", result.stderr)
        self.assertFalse(Path(self.temp.name, "hypr", "bindings.lua").exists())

    def test_binding_manager_never_reads_non_regular_configuration_nodes(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        choices = Path(self.temp.name, "omarchy-tts", "bindings.json")
        for path in (target, choices):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(path)
                result = subprocess.run(
                    [BINDINGS, "status"], env=self.env,
                    capture_output=True, text=True, timeout=2,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-regular", result.stderr)
                self.assertTrue(path.is_fifo())
                path.unlink()

    def test_binding_lock_never_follows_a_symlink(self):
        directory = Path(self.temp.name, "omarchy-tts")
        directory.mkdir(parents=True)
        victim = Path(self.temp.name, "binding-lock-victim")
        victim.write_text("keep me")
        (directory / "bindings.lock").symlink_to(victim)
        result = subprocess.run(
            [BINDINGS, "status"], env=self.env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private lock", result.stderr)
        self.assertEqual(victim.read_text(), "keep me")

    def test_binding_conflicts_ignore_spacing_and_modifier_order(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        target.write_text('o.bind("ALT+SUPER+E", "Editor", "code")\n')
        status = subprocess.run([BINDINGS, "status"], env=self.env,
                                capture_output=True, text=True, check=True)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["conflicts"], ["SUPER + ALT + E"])
        self.assertFalse(payload["canInstall"])

    def test_commented_bindings_do_not_create_false_conflicts(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        target.write_text('-- o.bind("SUPER + ALT + E", "Old", "editor")\n')
        result = subprocess.run([BINDINGS, "install"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-- o.bind", target.read_text())
        self.assertIn("omarchy-tts bindings", target.read_text())

    def test_active_documented_bindings_are_adoptable(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        target.write_text(
            'o.bind("SUPER + ALT + E", "Speak selection", "speak --toggle")\n'
        )
        tools = Path(self.temp.name, "legacy-binding-tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == binds ]]; then\n"
            "  printf '%s\\n' '[{\"modmask\":72,\"key\":\"E\","
            "\"dispatcher\":\"exec\",\"arg\":\"speak --toggle\"}]'\n"
            "else exit 0; fi\n"
        )
        hyprctl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "HYPRLAND_INSTANCE_SIGNATURE": "test"}
        result = subprocess.run([BINDINGS, "install"], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text().count("SUPER + ALT + E"), 1)

    def test_unrelated_exact_speak_command_is_never_adopted(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        original = 'o.bind("SUPER + ALT + E", "Other", "/usr/bin/speak --toggle")\n'
        target.write_text(original)
        result = subprocess.run([BINDINGS, "install"], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), original)

    def test_binding_manager_rejects_duplicate_managed_shortcuts(self):
        result = subprocess.run(
            [BINDINGS, "set", "selection", "SUPER + ALT + A"], env=self.env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shortcut already used", result.stderr)
        self.assertFalse(Path(self.temp.name, "omarchy-tts", "bindings.json").exists())

    def test_custom_program_named_speak_is_preserved_as_a_conflict(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        target.parent.mkdir()
        line = 'o.bind("SUPER + ALT + E", "Custom", "/usr/bin/speak --toggle --custom")\n'
        target.write_text(line)
        result = subprocess.run([BINDINGS, "install"], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), line)

    def test_binding_writes_preserve_dotfile_symlinks(self):
        target = Path(self.temp.name, "hypr", "bindings.lua")
        actual = Path(self.temp.name, "dotfiles", "bindings.lua")
        target.parent.mkdir()
        actual.parent.mkdir()
        actual.write_text("-- dotfile\n")
        target.symlink_to(actual)
        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        self.assertTrue(target.is_symlink())
        self.assertIn("omarchy-tts bindings", actual.read_text())

    def test_hyprland_rejection_removes_a_new_bindings_file(self):
        tools = Path(self.temp.name, "reject-tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == binds ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ $1 == configerrors ]]; then printf 'invalid generated binding\\n'; exit 0; fi\n"
            "exit 0\n"
        )
        hyprctl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "HYPRLAND_INSTANCE_SIGNATURE": "test"}
        target = Path(self.temp.name, "hypr", "bindings.lua")
        result = subprocess.run([BINDINGS, "install"], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid generated binding", result.stderr)
        self.assertFalse(target.exists())

    def test_hyprland_rejection_rolls_back_binding_and_choice_together(self):
        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        target = Path(self.temp.name, "hypr", "bindings.lua")
        choices = Path(self.temp.name, "omarchy-tts", "bindings.json")
        before_target = target.read_text()
        before_choices = choices.read_text() if choices.exists() else None

        tools = Path(self.temp.name, "rollback-tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == binds ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ $1 == configerrors ]]; then printf 'rejected\\n'; exit 0; fi\n"
            "exit 0\n"
        )
        hyprctl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "HYPRLAND_INSTANCE_SIGNATURE": "test"}
        result = subprocess.run(
            [BINDINGS, "set", "selection", "SUPER + ALT + Z"], env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), before_target)
        if before_choices is None:
            self.assertFalse(choices.exists())
        else:
            self.assertEqual(choices.read_text(), before_choices)

    def test_hyprland_launch_failure_rolls_back_both_files(self):
        subprocess.run([BINDINGS, "install"], env=self.env, check=True,
                       capture_output=True, text=True)
        target = Path(self.temp.name, "hypr", "bindings.lua")
        choices = Path(self.temp.name, "omarchy-tts", "bindings.json")
        before_target = target.read_text()

        tools = Path(self.temp.name, "missing-hypr-tools")
        tools.mkdir()
        hyprctl = tools / "hyprctl"
        hyprctl.write_text("#!/definitely/missing/interpreter\n")
        hyprctl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}",
               "HYPRLAND_INSTANCE_SIGNATURE": "test"}
        result = subprocess.run(
            [BINDINGS, "set", "selection", "SUPER + ALT + Z"], env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), before_target)
        self.assertFalse(choices.exists())

    def piper_plugin(self, catalogue, files=None):
        """A plugin tree whose shipped catalogue and digest manifest are the
        test's own. The trust root is repository-owned, so a test cannot inject
        it through the cache; it ships it the way a release does."""
        plugin = Path(self.temp.name, "plugin-" + uuid.uuid4().hex[:8])
        (plugin / "bin").mkdir(parents=True)
        (plugin / "lib").mkdir()
        shutil.copy(ROOT / "bin" / "speak-voice", plugin / "bin" / "speak-voice")
        for item in (ROOT / "lib").iterdir():
            if item.name not in ("piper-voices.json", "piper-voices.sha256"):
                (plugin / "lib" / item.name).symlink_to(item)
        (plugin / "lib" / "piper-voices.json").write_text(json.dumps(catalogue))
        lines = ["# test manifest", "0" * 64 + "  placeholder/none.onnx"]
        for path, content in (files or {}).items():
            lines.append(f"{hashlib.sha256(content).hexdigest()}  {path}")
        (plugin / "lib" / "piper-voices.sha256").write_text("\n".join(lines) + "\n")
        return plugin / "bin" / "speak-voice"

    def test_voice_catalogue_is_shipped_pinned_and_refresh_is_offline(self):
        """Every model and sidecar the plugin can install has a SHA-256 in the
        reviewed tree, the catalogue ships alongside, and refresh never
        contacts the network. A mutable upstream branch cannot change what
        is downloaded or what it must hash to."""
        catalogue = json.loads((ROOT / "lib" / "piper-voices.json").read_text())
        manifest = {}
        header = None
        for line in (ROOT / "lib" / "piper-voices.sha256").read_text().splitlines():
            if line.startswith("#"):
                header = header or line
                continue
            digest, path = line.split("  ", 1)
            self.assertRegex(digest, r"^[0-9a-f]{64}$", path)
            manifest[path] = digest
        revision = re.search(r"@([0-9a-f]{40})$", header).group(1)
        self.assertIn(f"PIPER_VOICES_REV={revision}",
                      (ROOT / "bin" / "speak-voice").read_text(),
                      "the download revision and the digest manifest must move together")
        for voice, entry in catalogue.items():
            for path in entry["files"]:
                if path.endswith(".onnx") or path.endswith(".onnx.json"):
                    self.assertIn(path, manifest, f"{voice}: {path} has no shipped digest")
        tools = Path(self.temp.name, "catalogue-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "refresh-curl-ran")
        curl = tools / "curl"
        curl.write_text("#!/usr/bin/env bash\nprintf ran > \"$REFRESH_MARKER\"\nexit 22\n")
        curl.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}", "REFRESH_MARKER": str(marker)}
        result = subprocess.run([ROOT / "bin" / "speak-voice", "refresh"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pinned", result.stdout)
        self.assertFalse(marker.exists(), "refresh must not touch the network")

    def test_download_is_refused_when_the_shipped_digest_does_not_match(self):
        voice = "en_US-tampered-medium"
        model_path = "en/en_US/tampered/medium/en_US-tampered-medium.onnx"
        catalogue = {voice: {"files": {
            model_path: {"size_bytes": 4},
            model_path + ".json": {"size_bytes": 2}}}}
        speak_voice = self.piper_plugin(catalogue, files={
            model_path: b"good", model_path + ".json": b"{}"})
        tools = Path(self.temp.name, "tamper-voice-tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "out=\nurl=\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == -o ]]; then out=$2; shift; elif [[ $1 == https:* ]]; then url=$1; fi\n"
            "  shift\n"
            "done\n"
            "if [[ $url == *.onnx.json* ]]; then printf '{}' > \"$out\"; else printf evil > \"$out\"; fi\n"
        )
        curl.chmod(0o755)
        env = {**self.env, "HOME": self.temp.name, "PATH": f"{tools}:{self.env['PATH']}"}
        result = subprocess.run([speak_voice, "add", voice], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification failed", result.stderr)
        voices = Path(self.temp.name, "omarchy-tts", "voices", "piper")
        self.assertFalse((voices / f"{voice}.onnx").exists())
        self.assertEqual(list(voices.glob("*.part")) if voices.exists() else [], [])

    def test_download_is_refused_for_a_file_without_a_shipped_digest(self):
        voice = "en_US-unlisted-medium"
        model_path = "en/en_US/unlisted/medium/en_US-unlisted-medium.onnx"
        speak_voice = self.piper_plugin({voice: {"files": {
            model_path: {"size_bytes": 4}, model_path + ".json": {"size_bytes": 2}}}})
        tools = Path(self.temp.name, "unlisted-voice-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "unlisted-curl-ran")
        curl = tools / "curl"
        curl.write_text("#!/usr/bin/env bash\nprintf ran > \"$VOICE_CURL_MARKER\"\nexit 1\n")
        curl.chmod(0o755)
        env = {**self.env, "HOME": self.temp.name, "PATH": f"{tools}:{self.env['PATH']}",
               "VOICE_CURL_MARKER": str(marker)}
        result = subprocess.run([speak_voice, "add", voice], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified manifest", result.stderr)
        self.assertFalse(marker.exists(), "an unlisted file must never be requested")

    def test_voice_metadata_limits_are_rejected_before_download(self):
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True, exist_ok=True)
        voice = "en_US-too-large-medium"
        speak_voice = self.piper_plugin({voice: {"files": {
            "voices/large.onnx": {"size_bytes": 1_073_741_825},
            "voices/large.onnx.json": {"size_bytes": 2},
        }}}, files={})
        tools = Path(self.temp.name, "limit-voice-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "voice-curl-ran")
        curl = tools / "curl"
        curl.write_text("#!/usr/bin/env bash\nprintf ran > \"$VOICE_CURL_MARKER\"\nexit 1\n")
        curl.chmod(0o755)
        env = {**self.env, "HOME": self.temp.name,
               "PATH": f"{tools}:{self.env['PATH']}",
               "VOICE_CURL_MARKER": str(marker)}
        available = subprocess.run(
            [speak_voice, "available", "--json"], env=env,
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn(voice, {item["key"] for item in json.loads(available.stdout)})
        result = subprocess.run(
            [speak_voice, "add", voice], env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_real_piper_catalogue_paths_download_without_a_fake_prefix(self):
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True, exist_ok=True)
        voice = "en_US-layout-medium"
        model_path = "en/en_US/layout/medium/en_US-layout-medium.onnx"
        sidecar_path = model_path + ".json"
        speak_voice = self.piper_plugin({voice: {"files": {
            model_path: {"size_bytes": 4},
            sidecar_path: {"size_bytes": 2},
        }}}, files={model_path: b"data", sidecar_path: b"{}"})
        tools = Path(self.temp.name, "layout-voice-tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "out=\nurl=\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == -o ]]; then out=$2; shift; elif [[ $1 == https:* ]]; then url=$1; fi\n"
            "  shift\n"
            "done\n"
            "printf '%s\\n' \"$url\" >> \"$VOICE_URLS\"\n"
            "if [[ $url == *.onnx.json* ]]; then printf '{}' > \"$out\"; else printf data > \"$out\"; fi\n"
        )
        curl.chmod(0o755)
        urls = Path(self.temp.name, "voice-urls")
        env = {**self.env, "HOME": self.temp.name,
               "PATH": f"{tools}:{self.env['PATH']}", "VOICE_URLS": str(urls)}
        result = subprocess.run(
            [speak_voice, "add", voice], env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(model_path, urls.read_text())
        self.assertIn("/resolve/1162a9173d0ce503555aed757976b7a9912eae4c/", urls.read_text())
        self.assertNotIn("/resolve/main/", urls.read_text())
        voices = base / "voices" / "piper"
        self.assertEqual((voices / f"{voice}.onnx").read_bytes(), b"data")
        self.assertEqual((voices / f"{voice}.onnx.json").read_text(), "{}")

    def test_ambiguous_voice_metadata_is_rejected_before_download(self):
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True, exist_ok=True)
        voice = "en_US-ambiguous-medium"
        speak_voice = self.piper_plugin({voice: {"files": {
            "voices/first.onnx": {"size_bytes": 2},
            "voices/second.onnx": {"size_bytes": 2},
            "voices/first.onnx.json": {"size_bytes": 2},
        }}}, files={})
        tools = Path(self.temp.name, "ambiguous-voice-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "voice-curl-ran")
        curl = tools / "curl"
        curl.write_text("#!/usr/bin/env bash\nprintf ran > \"$VOICE_CURL_MARKER\"\nexit 1\n")
        curl.chmod(0o755)
        env = {**self.env, "HOME": self.temp.name,
               "PATH": f"{tools}:{self.env['PATH']}",
               "VOICE_CURL_MARKER": str(marker)}
        result = subprocess.run(
            [speak_voice, "add", voice], env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid model metadata", result.stderr)
        self.assertFalse(marker.exists())

    def test_interrupted_voice_publication_is_recovered_before_reuse(self):
        base = Path(self.temp.name, "omarchy-tts")
        voices = base / "voices" / "piper"
        voices.mkdir(parents=True)
        voice = "en_US-recover-medium"
        speak_voice = self.piper_plugin({voice: {"files": {
            "voices/recover.onnx": {"size_bytes": 3},
            "voices/recover.onnx.json": {"size_bytes": 2},
        }}}, files={"voices/recover.onnx": b"old", "voices/recover.onnx.json": b"{}"})
        (voices / f"{voice}.onnx").write_bytes(b"x")
        (voices / f"{voice}.onnx.json").write_text("partial")
        (voices / f".{voice}.onnx.previous.4242").write_bytes(b"old")
        (voices / f".{voice}.onnx.json.previous.4242").write_text("{}")
        (voices / f".{voice}.onnx.stage.4242").write_bytes(b"new")

        result = subprocess.run(
            [speak_voice, "add", voice],
            env={**self.env, "HOME": self.temp.name},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((voices / f"{voice}.onnx").read_bytes(), b"old")
        self.assertEqual((voices / f"{voice}.onnx.json").read_text(), "{}")
        self.assertEqual(list(voices.glob(f".{voice}.*")), [])

    def test_active_voice_cannot_be_removed(self):
        base = Path(self.temp.name, "omarchy-tts")
        voices = base / "voices" / "piper"
        voices.mkdir(parents=True)
        voice = "en_US-active-medium"
        model = voices / f"{voice}.onnx"
        sidecar = voices / f"{voice}.onnx.json"
        model.write_bytes(b"model")
        sidecar.write_text("{}")
        (base / "config.json").write_text(json.dumps({"piper": {"voice": voice}}))
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "remove", voice],
            env={**self.env, "HOME": self.temp.name},
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("choose another active voice", result.stderr)
        self.assertTrue(model.exists())
        self.assertTrue(sidecar.exists())

    def test_installed_voice_can_be_selected_offline_without_a_catalogue(self):
        base = Path(self.temp.name, "omarchy-tts")
        voices = base / "voices" / "piper"
        voices.mkdir(parents=True)
        voice = "en_US-offline-medium"
        (voices / f"{voice}.onnx").write_bytes(b"model")
        (voices / f"{voice}.onnx.json").write_text("{}")

        tools = Path(self.temp.name, "offline-voice-tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text("#!/usr/bin/env bash\nexit 99\n")
        curl.chmod(0o755)
        env = {**self.env, "HOME": self.temp.name,
               "PATH": f"{tools}:{self.env['PATH']}"}
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "use", voice], env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((base / "config.json").read_text())
        self.assertEqual(config["piper"]["voice"], voice)

    def test_voice_status_does_not_initialize_config_or_data(self):
        root = Path(self.temp.name, "isolated-voice-status")
        env = {**self.env,
               "XDG_CONFIG_HOME": str(root / "config"),
               "XDG_CACHE_HOME": str(root / "cache"),
               "XDG_DATA_HOME": str(root / "data"),
               "XDG_RUNTIME_DIR": str(root / "runtime"),
               "HOME": str(root / "home")}
        result = subprocess.run([ROOT / "bin" / "speak-voice", "status"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "idle")
        self.assertFalse((root / "config" / "omarchy-tts").exists())
        self.assertFalse((root / "data" / "omarchy-tts").exists())
        self.assertFalse((root / "cache" / "omarchy-tts").exists())

    def test_async_voice_download_registers_identity_before_acknowledging_start(self):
        env = {**self.env, "HOME": self.temp.name}
        base = Path(self.temp.name, "omarchy-tts")
        voices = base / "voices" / "piper"
        voices.mkdir(parents=True)
        voice = "en_US-test-medium"
        (voices / f"{voice}.onnx").write_bytes(b"model")
        (voices / f"{voice}.onnx.json").write_text("{}")
        speak_voice = self.piper_plugin({voice: {"files": {
            "voices/test.onnx": {"size_bytes": 5},
            "voices/test.onnx.json": {"size_bytes": 2},
        }}}, files={"voices/test.onnx": b"model", "voices/test.onnx.json": b"{}"})

        result = subprocess.run(
            [speak_voice, "add", voice, "--async"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "started")
        state_path = base / "download.json"
        for _ in range(100):
            state = json.loads(state_path.read_text())
            if state["status"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual(state["status"], "done", state)
        self.assertGreater(state["pid"], 1)
        self.assertTrue(state["processIdentity"])

    def test_async_voice_worker_holds_the_download_lock(self):
        env = {**self.env, "HOME": self.temp.name}
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True, exist_ok=True)
        voice = "en_US-lock-medium"
        speak_voice = self.piper_plugin({
            voice: {
                "name": "lock", "quality": "medium",
                "language": {"name_english": "English",
                             "country_english": "United States", "code": "en_US"},
                "files": {
                    "voices/test.onnx": {"size_bytes": 4},
                    "voices/test.onnx.json": {"size_bytes": 2},
                },
            }
        }, files={"voices/test.onnx": b"data", "voices/test.onnx.json": b"{}"})
        tools = Path(self.temp.name, "voice-tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "out=\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == -o || $1 == --output ]]; then out=$2; shift; fi\n"
            "  shift\n"
            "done\n"
            "sleep 10\n"
            "printf data > \"$out\"\n"
        )
        curl.chmod(0o755)
        env["PATH"] = f"{tools}:{env['PATH']}"

        first = subprocess.run(
            [speak_voice, "add", voice, "--async"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        state = json.loads((base / "download.json").read_text())
        self.assertIn(state["status"], ("starting", "downloading"))
        self.assertGreater(state["pid"], 1)
        self.assertTrue(state["processIdentity"])
        self.assertEqual(os.getpgid(state["pid"]), state["pid"], state)
        second = subprocess.run(
            [speak_voice, "add", voice, "--async"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr)
        cancelled = subprocess.run(
            [speak_voice, "cancel"], env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)

    def test_cancelling_new_voice_rolls_back_model_and_sidecar_together(self):
        env = {**self.env, "HOME": self.temp.name}
        base = Path(self.temp.name, "omarchy-tts")
        base.mkdir(parents=True, exist_ok=True)
        voice = "en_US-atomic-medium"
        speak_voice = self.piper_plugin({
            voice: {
                "files": {
                    "voices/test.onnx": {"size_bytes": 4},
                    "voices/test.onnx.json": {"size_bytes": 2},
                },
            },
        }, files={"voices/test.onnx": b"data", "voices/test.onnx.json": b"{}"})
        marker = Path(self.temp.name, "metadata-started")
        tools = Path(self.temp.name, "atomic-voice-tools")
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "out=\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == -o || $1 == --output ]]; then out=$2; shift; fi\n"
            "  shift\n"
            "done\n"
            "if [[ $out == *.onnx.part ]]; then printf data > \"$out\"; exit 0; fi\n"
            "printf started > \"$VOICE_METADATA_MARKER\"\n"
            "sleep 30\n"
            "printf '{}' > \"$out\"\n"
        )
        curl.chmod(0o755)
        env["PATH"] = f"{tools}:{env['PATH']}"
        env["VOICE_METADATA_MARKER"] = str(marker)

        started = subprocess.run(
            [speak_voice, "add", voice, "--async"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        for _ in range(300):
            if marker.exists():
                break
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "metadata download never started")
        cancelled = subprocess.run(
            [speak_voice, "cancel"], env=env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        voices = base / "voices" / "piper"
        self.assertFalse((voices / f"{voice}.onnx").exists())
        self.assertFalse((voices / f"{voice}.onnx.json").exists())

    def test_voice_cancel_refuses_when_no_registered_job_exists(self):
        env = {**self.env, "HOME": self.temp.name}
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "cancel"],
            env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no active voice download", result.stderr)

    def test_voice_cancel_never_signals_a_completed_job_identity(self):
        env = {**self.env, "HOME": self.temp.name}
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            stat = Path(f"/proc/{sleeper.pid}/stat").read_text()
            identity = stat.rsplit(")", 1)[1].split()[19]
            state = Path(self.temp.name, "omarchy-tts", "download.json")
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({
                "status": "done", "voice": "test", "pid": sleeper.pid,
                "processIdentity": identity,
            }))
            result = subprocess.run(
                [ROOT / "bin" / "speak-voice", "cancel"], env=env,
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=3)

    def test_voice_status_repairs_corrupt_or_unowned_active_state(self):
        state = Path(self.temp.name, "omarchy-tts", "download.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        for value, message in (
                ('{"status":', "unreadable"),
                (json.dumps({"status": "downloading", "voice": "test",
                             "pid": 0, "processIdentity": ""}),
                 "stopped unexpectedly")):
            state.write_text(value)
            result = subprocess.run(
                [ROOT / "bin" / "speak-voice", "status"],
                env={**self.env, "HOME": self.temp.name},
                check=True, capture_output=True, text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertIn(message, payload["message"])
            self.assertEqual(json.loads(state.read_text()), payload)

    def test_voice_status_replaces_oversized_state_without_parsing_it(self):
        state = Path(self.temp.name, "omarchy-tts", "download.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(b" " * 65537)
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "status"],
            env={**self.env, "HOME": self.temp.name}, check=True,
            capture_output=True, text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertLess(state.stat().st_size, 65537)

    def test_voice_status_never_enters_or_reads_non_regular_state_nodes(self):
        state = Path(self.temp.name, "omarchy-tts", "download.json")
        state.mkdir(parents=True)
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "status"],
            env={**self.env, "HOME": self.temp.name}, check=True,
            capture_output=True, text=True, timeout=2,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("unsafe", payload["message"])
        self.assertTrue(state.is_dir())
        self.assertEqual(list(state.iterdir()), [])

        state.rmdir()
        os.mkfifo(state)
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "status"],
            env={**self.env, "HOME": self.temp.name}, check=True,
            capture_output=True, text=True, timeout=2,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertTrue(state.is_fifo())

    def test_setup_backend_status_and_allowlist(self):
        self.env["XDG_DATA_HOME"] = str(Path(self.temp.name, "data"))
        status = subprocess.run([SETUP, "status"], env=self.env, check=True,
                                capture_output=True, text=True)
        payload = json.loads(status.stdout)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["defaultVoice"], "en_US-amy-medium")
        rejected = subprocess.run([SETUP, "start", "anything-else"], env=self.env,
                                  check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(rejected.stdout)["code"], "unknown_target")

    def test_setup_job_tolerates_corrupt_json_shapes(self):
        state = Path(self.temp.name, "omarchy-tts", "setup.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("[]")
        result = subprocess.run([SETUP, "job"], env=self.env, check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["status"], "idle")

    def test_setup_job_rejects_oversized_control_state(self):
        state = Path(self.temp.name, "omarchy-tts", "setup.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(b" " * 65537)
        result = subprocess.run([SETUP, "job"], env=self.env, check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["status"], "idle")

    def test_setup_job_never_reads_a_non_regular_control_node(self):
        state = Path(self.temp.name, "omarchy-tts", "setup.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(state)
        result = subprocess.run([SETUP, "job"], env=self.env, check=True,
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(json.loads(result.stdout)["status"], "idle")

    def test_setup_cancel_refuses_a_completed_or_unowned_job(self):
        state = Path(self.temp.name, "omarchy-tts", "setup.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        original = {"status": "done", "pid": os.getpid(),
                    "processIdentity": "not-the-owner"}
        state.write_text(json.dumps(original))
        result = subprocess.run([SETUP, "cancel"], env=self.env, check=True,
                                capture_output=True, text=True)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(json.loads(state.read_text()), original)

    def test_setup_rejects_short_or_unknown_keys_without_storing(self):
        for provider, value, code in (("openai", "short", "invalid_key"),
                                      ("other", "long-enough-key", "unknown_provider")):
            result = subprocess.run([SETUP, "key-store", provider], input=value + "\n",
                                    env=self.env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["code"], code)

    def test_setup_cleanup_requires_explicit_confirmation(self):
        result = subprocess.run([SETUP, "cleanup"], env=self.env, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"],
                         "confirmation_required")

    def test_engine_setup_installs_only_hash_locked_closures(self):
        """Every managed engine installs from a complete lock shipped in the
        plugin, hash-enforced and wheel-only, and the installer never lets a
        library fetch code or models on its own."""
        source = SETUP.read_text()
        self.assertIn('"--require-hashes",', source)
        self.assertIn('"--only-binary", ":all:"', source)
        self.assertIn('"--find-links", str(ENGINE_SPECS / "wheels")', source)
        self.assertIn('"pip", "sync"', source)
        self.assertNotIn('"pip", "install"', source)
        self.assertNotIn("download_enabled=True", source)
        self.assertIn('"venv", "--relocatable"', source)
        for engine, top in (("piper", "piper-tts==1.7.0"), ("kokoro", "kokoro==0.9.4"),
                            ("kokoro-onnx", "kokoro-onnx==0.6.1"), ("easyocr", "easyocr==1.7.2")):
            spec = (ROOT / "lib" / "engines" / f"{engine}.in").read_text()
            self.assertIn(top, spec, engine)
            blocks = self.lock_blocks((ROOT / "lib" / "engines" / f"{engine}.lock").read_text())
            self.assertTrue(blocks, engine)
            for requirement, hashes in blocks:
                self.assertRegex(requirement, r"(==|@ https://)", f"{engine}: {requirement} is not pinned")
                self.assertTrue(hashes, f"{engine}: {requirement} has no hash")
                for digest in hashes:
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotIn("./", "".join(r for r, _ in blocks), f"{engine}: path requirements are not allowed in a lock")
        kokoro = dict(self.lock_blocks((ROOT / "lib" / "engines" / "kokoro.lock").read_text()))
        spacy = next(r for r in kokoro if r.startswith("en-core-web-sm @"))
        self.assertIn("1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85", kokoro[spacy])
        # The one dependency without a published wheel is served from the
        # reviewed flat index, and the lock admits exactly that wheel.
        wheels = ROOT / "lib" / "engines" / "wheels"
        sums = dict(line.split("  ", 1)[::-1] for line in (wheels / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(sums), {p.name for p in wheels.iterdir() if p.name != "SHA256SUMS"})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((wheels / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(kokoro["docopt==0.6.2"], [sums["docopt-0.6.2-py2.py3-none-any.whl"]])
        self.assertRegex((ROOT / "lib" / "engines" / "EXCLUDE_NEWER").read_text().strip(),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    @staticmethod
    def lock_blocks(lock):
        """[(requirement, [hashes])] from a uv requirements lock."""
        blocks, current = [], None
        for line in lock.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            if line.startswith(" "):
                for digest in re.findall(r"--hash=sha256:([0-9a-f]+)", line):
                    current[1].append(digest)
            else:
                current = [line.rstrip(" \\").strip(), []]
                blocks.append(current)
        return [(r, h) for r, h in blocks]

    def test_engine_builders_run_exactly_the_locked_wheel_only_install(self):
        setup = load_setup_module()
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            if argv[2] == "venv":
                Path(argv[-1], "bin").mkdir(parents=True)
        setup.run_checked = run
        setup.ensure_uv = lambda: "/usr/bin/uv"
        setup.install_kokoro_models = lambda models: calls.append(["models", str(models)])
        setup.install_easyocr_models = lambda models: calls.append(["models", str(models)])
        setup.install_kokoro_onnx_models = lambda models: calls.append(["models", str(models)])
        setup.write_state = lambda *a, **k: None
        for engine, build in (("piper", setup.build_piper), ("kokoro", setup.build_kokoro),
                              ("kokoro-onnx", setup.build_kokoro_onnx), ("easyocr", setup.build_easyocr)):
            calls.clear()
            target = Path(self.temp.name, f"stage-{engine}")
            build(target)
            self.assertEqual(calls[0], ["/usr/bin/uv", "--no-config", "venv", "--relocatable", "--python", "3.12", str(target)], engine)
            self.assertEqual(calls[1], ["/usr/bin/uv", "--no-config", "pip", "sync", "--python", str(target / "bin/python"),
                                        "--require-hashes", "--only-binary", ":all:",
                                        "--find-links", str(setup.ENGINE_SPECS / "wheels"),
                                        str(setup.ENGINE_LOCKS[engine])], engine)
            self.assertEqual((target / ".lock-sha256").read_text().strip(),
                             hashlib.sha256(setup.ENGINE_LOCKS[engine].read_bytes()).hexdigest(), engine)
            if engine != "piper":
                self.assertEqual(calls[2], ["models", str(target / "models")], engine)

    def test_uv_is_only_the_distribution_binary_and_runs_without_overrides(self):
        source = SETUP.read_text()
        self.assertNotIn('which("uv")', source)
        setup = load_setup_module()
        setup.SYSTEM_UV = Path(self.temp.name, "not-there")
        installed = []
        setup.privileged_pacman = lambda *pkgs: installed.append(pkgs)
        setup.write_state = lambda *a, **k: None
        real = stub_trusted_helper_path(setup)
        setup.PKEXEC = real
        setup.PACMAN = real
        with self.assertRaisesRegex(RuntimeError, "could not be found"):
            setup.ensure_uv()
        self.assertEqual(installed, [("uv",)], "a missing uv is installed through the privileged path, never taken from PATH")
        with unittest.mock.patch.dict(os.environ, {"UV_INDEX_URL": "https://evil.invalid/simple",
                                                   "UV_FIND_LINKS": "/tmp/evil", "PIP_INDEX_URL": "x",
                                                   "UV_CACHE_DIR": "/tmp/cache", "HOME": self.temp.name}):
            env = setup.uv_environment()
        self.assertNotIn("UV_INDEX_URL", env)
        self.assertNotIn("UV_FIND_LINKS", env)
        self.assertNotIn("PIP_INDEX_URL", env)
        self.assertEqual(env["UV_CACHE_DIR"], "/tmp/cache")
        self.assertEqual(env["HOME"], self.temp.name)

    def test_engines_from_other_releases_are_not_installed(self):
        setup = load_setup_module()
        setup.ENGINE = Path(self.temp.name, "engines")
        (setup.ENGINE / "piper" / "bin").mkdir(parents=True)
        self.assertFalse(setup.engine_built_from_lock("piper"), "no stamp: built by something else")
        (setup.ENGINE / "piper" / ".lock-sha256").write_text("0" * 64 + "\n")
        self.assertFalse(setup.engine_built_from_lock("piper"), "another lock's digest")
        (setup.ENGINE / "piper" / ".lock-sha256").write_text(
            hashlib.sha256(setup.ENGINE_LOCKS["piper"].read_bytes()).hexdigest() + "\n")
        self.assertTrue(setup.engine_built_from_lock("piper"))
        # and the engines themselves refuse such an environment before running anything
        tools = Path(self.temp.name, "stale-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "stale-ran")
        (tools / "python").write_text("#!/usr/bin/env bash\nprintf ran > \"$ENGINE_MARKER\"\nexit 0\n")
        (tools / "python").chmod(0o755)
        data = Path(self.temp.name, "stale-data")
        for engine in ("kokoro", "kokoro-onnx", "easyocr"):
            (data / "engines" / engine / "bin").mkdir(parents=True)
            shutil.copy(tools / "python", data / "engines" / engine / "bin" / "python")
            (data / "engines" / engine / "models").mkdir()
        (data / "engines" / "kokoro" / "models" / "kokoro-v1_0.pth").write_bytes(b"x")
        (data / "engines" / "kokoro-onnx" / "models" / "kokoro-v1.0.onnx").write_bytes(b"x")
        (data / "engines" / "easyocr" / "models" / "craft_mlt_25k.pth").write_bytes(b"x")
        env = {**os.environ, "TTS_PLUGIN_DIR": str(ROOT), "TTS_DATA_DIR": str(data),
               "TTS_CONFIG": str(Path(self.temp.name, "c.json")), "TTS_SILENT": "1", "ENGINE_MARKER": str(marker)}
        Path(self.temp.name, "c.json").write_text("{}")
        for engine, script in (("kokoro", ROOT / "providers" / "kokoro"),
                               ("kokoro-onnx", ROOT / "providers" / "kokoro-onnx"),
                               ("easyocr", ROOT / "ocr" / "easyocr")):
            result = subprocess.run([script], input=b"Probe.", capture_output=True, env=env)
            self.assertEqual(result.returncode, 127, (engine, result.stderr))
            self.assertIn(b"installed by an earlier release", result.stderr, engine)
            self.assertFalse(marker.exists(), engine)

    def test_language_install_goes_through_the_privileged_path_only(self):
        setup = load_setup_module()
        calls = []
        setup.PKEXEC = stub_trusted_helper_path(setup)
        setup.language_package = lambda code: f"tesseract-data-{code}"
        setup.privileged_pacman = lambda *pkgs: calls.append(pkgs)
        setup.write_state = lambda *a, **k: None
        setup.subprocess = unittest.mock.MagicMock()
        setup.subprocess.run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
        setup.install_language("deu")
        self.assertEqual(calls, [("tesseract-data-deu",)])
        setup.PKEXEC = Path(self.temp.name, "pkexec")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            setup.install_language("deu")
        self.assertEqual(len(calls), 1)

    def test_reviewed_wheel_index_is_verified_before_uv_runs(self):
        setup = load_setup_module()
        specs = Path(self.temp.name, "engines")
        shutil.copytree(ROOT / "lib" / "engines", specs)
        setup.ENGINE_SPECS = specs
        setup.ENGINE_LOCKS = {name: specs / f"{name}.lock" for name in ("piper", "kokoro", "easyocr")}
        setup.verify_reviewed_wheels()
        stray = specs / "wheels" / "extra-1.0-py3-none-any.whl"
        stray.write_bytes(b"not reviewed")
        with self.assertRaisesRegex(RuntimeError, "does not match its digest list"):
            setup.verify_reviewed_wheels()
        stray.unlink()
        wheel = next(specs.glob("wheels/*.whl"))
        wheel.write_bytes(wheel.read_bytes() + b"x")
        with self.assertRaisesRegex(RuntimeError, "failed verification"):
            setup.verify_reviewed_wheels()

    def test_easyocr_model_extraction_takes_only_the_named_verified_member(self):
        setup = load_setup_module()
        model = b"model-bytes"
        entry = {"url": "https://example.invalid/m.zip", "archive_sha256": "", "archive_size": 0,
                 "sha256": hashlib.sha256(model).hexdigest(), "size": len(model)}
        def archive_with(members):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as bundle:
                for name, data in members.items():
                    bundle.writestr(name, data)
            return buffer.getvalue()
        def install(members):
            data = archive_with(members)
            entry["archive_sha256"] = hashlib.sha256(data).hexdigest()
            entry["archive_size"] = len(data)
            setup.model_manifest = lambda: {"easyocr": {"models": {"english_g2.pth": entry}}}
            setup.fetch_verified = lambda url, dest, sha, size: dest.write_bytes(data)
            target = Path(self.temp.name, "models-" + uuid.uuid4().hex[:6])
            setup.install_easyocr_models(target)
            return target
        # the named member is extracted, a sibling with a traversal name is ignored
        target = install({"english_g2.pth": model, "../evil.pth": b"x", "other.pth": b"y"})
        self.assertEqual((target / "english_g2.pth").read_bytes(), model)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["english_g2.pth"])
        # a member whose bytes differ from the shipped digest is refused (same
        # length, so it is the digest and not the size check that catches it)
        with self.assertRaisesRegex(RuntimeError, "does not match its shipped digest"):
            install({"english_g2.pth": b"model-BYTES"})
        # and a member of the wrong length never even reaches the digest
        with self.assertRaisesRegex(RuntimeError, "size does not match"):
            install({"english_g2.pth": b"tampered-bytes"})
        # an archive without the member is refused
        with self.assertRaisesRegex(RuntimeError, "does not contain the expected model"):
            install({"something-else.pth": model})

    def test_piper_verifies_catalogue_voices_before_loading(self):
        tools = Path(self.temp.name, "piper-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "piper-ran")
        fake_piper = tools / "piper"
        fake_piper.write_text("#!/usr/bin/env bash\nprintf ran > \"$PIPER_MARKER\"\ncat >/dev/null\n")
        fake_piper.chmod(0o755)
        voices = Path(self.temp.name, "voices")
        voices.mkdir()
        (voices / "en_US-amy-medium.onnx").write_bytes(b"not the real model")
        (voices / "en_US-amy-medium.onnx.json").write_text('{"audio": {"sample_rate": 22050}}')
        (voices / "custom-voice.onnx").write_bytes(b"my own model")
        (voices / "custom-voice.onnx.json").write_text('{"audio": {"sample_rate": 22050}}')
        data = Path(self.temp.name, "piper-data")
        (data / "engines" / "piper" / "bin").mkdir(parents=True)
        shutil.copy(fake_piper, data / "engines" / "piper" / "bin" / "piper")
        (data / "engines" / "piper" / ".lock-sha256").write_text(
            hashlib.sha256((ROOT / "lib" / "engines" / "piper.lock").read_bytes()).hexdigest() + "\n")
        env = {**os.environ, "TTS_PLUGIN_DIR": str(ROOT), "TTS_CONFIG": str(Path(self.temp.name, "c.json")),
               "TTS_DATA_DIR": str(data), "PIPER_VOICES_DIR": str(voices), "TTS_SILENT": "1",
               "PIPER_MARKER": str(marker), "XDG_RUNTIME_DIR": str(Path(self.temp.name, "run")),
               "XDG_CACHE_HOME": str(Path(self.temp.name, "cache"))}
        Path(self.temp.name, "run").mkdir(exist_ok=True)
        Path(self.temp.name, "c.json").write_text("{}")
        result = subprocess.run([ROOT / "providers" / "piper"], input=b"Probe.", capture_output=True,
                                env={**env, "TTS_VOICE": "en_US-amy-medium"})
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertFalse(marker.exists(), "a catalogue voice with the wrong bytes must never reach Piper")
        result = subprocess.run([ROOT / "providers" / "piper"], input=b"Probe.", capture_output=True,
                                env={**env, "TTS_VOICE": "custom-voice"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "a voice outside the catalogue is the user's own")

    def test_piper_repeated_presses_come_from_the_cache(self):
        tools = Path(self.temp.name, "cache-piper-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "synth-runs")
        # The fake engine both records that synthesis ran and emits the
        # PCM-looking audio the cache would store.
        fake_piper = tools / "piper"
        fake_piper.write_text(
            "#!/usr/bin/env bash\ncat >/dev/null\nprintf ran >> \"$PIPER_MARKER\"\n"
            "printf '\\x01\\x00\\x02\\x00\\x03\\x00\\x04\\x00'\n")
        fake_piper.chmod(0o755)
        voices = Path(self.temp.name, "cache-voices")
        voices.mkdir()
        (voices / "custom-voice.onnx").write_bytes(b"my own model")
        (voices / "custom-voice.onnx.json").write_text('{"audio": {"sample_rate": 22050}}')
        data = Path(self.temp.name, "cache-piper-data")
        (data / "engines" / "piper" / "bin").mkdir(parents=True)
        shutil.copy(fake_piper, data / "engines" / "piper" / "bin" / "piper")
        (data / "engines" / "piper" / ".lock-sha256").write_text(
            hashlib.sha256((ROOT / "lib" / "engines" / "piper.lock").read_bytes()).hexdigest() + "\n")
        env = {**os.environ, "TTS_PLUGIN_DIR": str(ROOT), "TTS_CONFIG": str(Path(self.temp.name, "c.json")),
               "TTS_DATA_DIR": str(data), "PIPER_VOICES_DIR": str(voices), "TTS_SILENT": "1",
               "PIPER_MARKER": str(marker), "XDG_RUNTIME_DIR": str(Path(self.temp.name, "run")),
               "XDG_CACHE_HOME": str(Path(self.temp.name, "cache"))}
        Path(self.temp.name, "run").mkdir(exist_ok=True)
        Path(self.temp.name, "c.json").write_text("{}")
        press = lambda **extra: subprocess.run(
            [ROOT / "providers" / "piper"], input=b"Probe.", capture_output=True,
            env={**env, **extra}, check=False)

        first = press(TTS_VOICE="custom-voice")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(marker.read_text()), 3, "one synthesis ran")  # "ran"
        second = press(TTS_VOICE="custom-voice")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(marker.read_text()), 3,
                         "the repeated press is a file read, not a synthesis")
        audio = Path(self.temp.name, "cache", "omarchy-tts", "audio")
        entries = list(audio.glob("piper-*.pcm"))
        self.assertEqual(len(entries), 1, "one cache entry")
        self.assertEqual(entries[0].stat().st_mode & 0o777, 0o600)
        third = press(TTS_VOICE="custom-voice", TTS_CACHE="0")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(len(marker.read_text()), 6,
                         "with the cache off Piper synthesizes again")

    def test_model_artefacts_are_pinned_to_immutable_sources_with_digests(self):
        manifest = json.loads((ROOT / "lib" / "engines" / "models.json").read_text())
        for name, entry in manifest["easyocr"]["models"].items():
            self.assertRegex(entry["url"], r"^https://github\.com/JaidedAI/EasyOCR/releases/download/[^/]+/[^/]+\.zip$", name)
            self.assertRegex(entry["archive_sha256"], r"^[0-9a-f]{64}$", name)
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertGreater(entry["size"], 0)
        kokoro = manifest["kokoro"]
        self.assertRegex(kokoro["revision"], r"^[0-9a-f]{40}$")
        for path in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
            self.assertRegex(kokoro["files"][path]["sha256"], r"^[0-9a-f]{64}$", path)
        onnx = manifest["kokoro-onnx"]
        self.assertRegex(onnx["note"], r"int8 quantization measured 4\.2[xy] slower", "the bring-up benchmark stays on record")
        for path in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            self.assertRegex(onnx["files"][path]["url"],
                             r"^https://github\.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1\.0/[^/]+$", path)
            self.assertRegex(onnx["files"][path]["sha256"], r"^[0-9a-f]{64}$", path)
            self.assertGreater(onnx["files"][path]["size"], 0, path)
        provider = (ROOT / "providers" / "kokoro").read_text()
        for voice in re.findall(r'"(a[fm]_[a-z]+|b[fm]_[a-z]+)"', (ROOT / "bin" / "speak").read_text()):
            self.assertIn(f"voices/{voice}.pt", kokoro["files"], f"bundled voice {voice} has no digest")
        self.assertIn("HF_HUB_OFFLINE=1", provider)
        self.assertIn("weights_only=True", provider)
        self.assertNotIn("download_enabled=True", (ROOT / "ocr" / "easyocr").read_text())

    def test_privileged_helpers_are_fixed_paths_verified_at_use(self):
        source = SETUP.read_text()
        self.assertNotIn('which("pkexec")', source)
        self.assertNotIn('which("pacman")', source)
        self.assertIn('PKEXEC = Path("/usr/bin/pkexec")', source)
        self.assertIn('PACMAN = Path("/usr/bin/pacman")', source)
        setup = load_setup_module()
        # A distribution-installed executable passes. Not every distro keeps
        # the same real files under /usr/bin (newer runner images ship /usr/bin/env
        # as a symlink), so take the first candidate that satisfies the
        # trusted-path rules on this machine.
        real = None
        for candidate in ("/usr/bin/env", "/usr/bin/bash", "/usr/bin/true"):
            try:
                if setup.trusted_system_executable(Path(candidate)) == candidate:
                    real = Path(candidate)
                    break
            except RuntimeError:
                continue
        self.assertIsNotNone(real, "no real distribution executable found")
        # Anything under a user-writable tree is refused, however it is named.
        mine = Path(self.temp.name, "pacman")
        mine.write_text("#!/usr/bin/env bash\nexit 0\n")
        mine.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "not owned by root|writable by other users"):
            setup.trusted_system_executable(mine)
        # Symlink components are refused before ownership is even considered.
        link = Path(self.temp.name, "usr-bin-link")
        link.symlink_to("/usr/bin")
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            setup.trusted_system_executable(link / "env")
        # Relative and non-canonical spellings never reach the privilege helper.
        for bad in (Path("usr/bin/env"), Path("/usr/bin/../bin/env")):
            with self.assertRaisesRegex(RuntimeError, "non-canonical"):
                setup.trusted_system_executable(bad)
        # And the privileged call composes only from those checks.
        calls = []
        setup.run_checked = lambda argv, **kw: calls.append(argv)
        setup.PKEXEC = real
        setup.PACMAN = real
        setup.privileged_pacman("uv")
        self.assertEqual(calls, [[str(real), str(real), "-S", "--noconfirm", "--needed", "uv"]])
        setup.PACMAN = mine
        with self.assertRaises(RuntimeError):
            setup.privileged_pacman("uv")
        self.assertEqual(len(calls), 1, "a refused package manager must never be executed")

    def test_fetch_verified_rejects_a_digest_mismatch_and_keeps_nothing(self):
        setup = load_setup_module()
        served = Path(self.temp.name, "served.bin")
        served.write_bytes(b"evil")
        setup.run_checked = lambda argv, **kw: shutil.copy(served, argv[-1])
        dest = Path(self.temp.name, "model.pth")
        good = hashlib.sha256(b"good").hexdigest()
        with self.assertRaisesRegex(RuntimeError, "does not match its shipped digest"):
            setup.fetch_verified("https://example.invalid/model", dest, good, 4)
        self.assertFalse(dest.exists())
        self.assertEqual(list(Path(self.temp.name).glob("model.pth*")), [])
        with self.assertRaisesRegex(RuntimeError, "unpinned source"):
            setup.fetch_verified("http://example.invalid/model", dest, good, 4)
        served.write_bytes(b"good")
        setup.fetch_verified("https://example.invalid/model", dest, good, 4)
        self.assertEqual(dest.read_bytes(), b"good")

    def test_fetch_verified_creates_a_missing_destination_directory(self):
        # Preserve a one-line bug from 1.4.0 as a regression: curl was pointed
        # at `<dest>.part` inside a directory the builder never created, so
        # curl 8.x died with (23) "client returned ERROR on write of N bytes"
        # and the whole engine transaction rolled back.
        setup = load_setup_module()
        served = Path(self.temp.name, "served.bin")
        served.write_bytes(b"good")
        setup.run_checked = lambda argv, **kw: shutil.copyfile(served, argv[-1])
        dest = Path(self.temp.name, "fresh-dir", "model.onnx")
        good = hashlib.sha256(b"good").hexdigest()
        setup.fetch_verified("https://example.invalid/model", dest, good, 4)
        self.assertEqual(dest.read_bytes(), b"good")

    def test_kokoro_onnx_install_creates_its_models_directory(self):
        setup = load_setup_module()
        files = {
            "kokoro-v1.0.onnx": {"url": "https://example.invalid/kokoro-v1.0.onnx",
                                 "sha256": hashlib.sha256(b"m").hexdigest(), "size": 1},
            "voices-v1.0.bin": {"url": "https://example.invalid/voices-v1.0.bin",
                                "sha256": hashlib.sha256(b"v").hexdigest(), "size": 1},
        }
        setup.model_manifest = lambda: {"kokoro-onnx": {"files": files}}
        setup.fetch_verified = lambda url, dest, sha, size: dest.write_text(sha)
        # The verdict is recorded only after every file landed, so a passing
        # hook proves the directory existed by then.
        setup.record_model_verification = lambda engine, models: self.assertTrue(models.is_dir())
        models = Path(self.temp.name, "onnx-models")
        setup.install_kokoro_onnx_models(models)
        self.assertEqual(sorted(p.name for p in models.iterdir()),
                         ["kokoro-v1.0.onnx", "voices-v1.0.bin"])

    def test_engine_transaction_rolls_back_when_proof_fails(self):
        setup = load_setup_module()
        setup.ENGINE = Path(self.temp.name, "transaction-engines")
        target = setup.ENGINE / "piper"
        target.mkdir(parents=True)
        (target / "generation").write_text("old")

        def build(staging):
            staging.mkdir()
            (staging / "generation").write_text("new")

        def fail_proof(_name):
            raise RuntimeError("proof failed")

        setup.prove = fail_proof
        with self.assertRaisesRegex(RuntimeError, "proof failed"):
            setup.install_engine_transaction("piper", build)
        self.assertEqual((target / "generation").read_text(), "old")
        self.assertEqual(list(setup.ENGINE.glob(".piper.staging.*")), [])
        self.assertEqual(list(setup.ENGINE.glob("piper.previous.*")), [])
        self.assertFalse((setup.ENGINE / ".piper.transaction.json").exists())

    def test_engine_transaction_commits_and_prunes_old_generations(self):
        setup = load_setup_module()
        setup.ENGINE = Path(self.temp.name, "commit-engines")
        target = setup.ENGINE / "kokoro"
        target.mkdir(parents=True)
        (target / "generation").write_text("old")
        stale = setup.ENGINE / "kokoro.previous.1"
        stale.mkdir()
        (stale / "generation").write_text("older")

        def build(staging):
            staging.mkdir()
            (staging / "generation").write_text("new")

        setup.prove = lambda _name: None
        setup.install_engine_transaction("kokoro", build)
        self.assertEqual((target / "generation").read_text(), "new")
        self.assertEqual(list(setup.ENGINE.glob("kokoro.previous.*")), [])
        self.assertFalse((setup.ENGINE / ".kokoro.transaction.json").exists())

    def test_engine_transaction_journal_recovers_a_hard_interruption(self):
        setup = load_setup_module()
        setup.ENGINE = Path(self.temp.name, "recovery-engines")
        setup.ENGINE.mkdir()
        target = setup.ENGINE / "easyocr"
        backup = setup.ENGINE / "easyocr.previous.42"
        staging = setup.ENGINE / ".easyocr.staging.42"
        target.mkdir()
        backup.mkdir()
        staging.mkdir()
        (target / "generation").write_text("unproved")
        (backup / "generation").write_text("known-good")
        setup.replace_json(setup.ENGINE / ".easyocr.transaction.json", {
            "name": "easyocr", "staging": str(staging),
            "backup": str(backup), "hadPrevious": True,
        })

        setup.recover_engine_transaction("easyocr")
        self.assertEqual((target / "generation").read_text(), "known-good")
        self.assertFalse(staging.exists())
        self.assertFalse((setup.ENGINE / ".easyocr.transaction.json").exists())

    def test_engine_transaction_finishes_a_durably_proved_commit(self):
        setup = load_setup_module()
        setup.ENGINE = Path(self.temp.name, "proved-recovery-engines")
        setup.ENGINE.mkdir()
        target = setup.ENGINE / "kokoro"
        backup = setup.ENGINE / "kokoro.previous.42"
        staging = setup.ENGINE / ".kokoro.staging.42"
        target.mkdir()
        backup.mkdir()
        (target / "generation").write_text("proved")
        (backup / "generation").write_text("old")
        setup.replace_json(setup.ENGINE / ".kokoro.transaction.json", {
            "name": "kokoro", "staging": str(staging),
            "backup": str(backup), "hadPrevious": True, "phase": "proved",
        })

        setup.recover_engine_transaction("kokoro")
        self.assertEqual((target / "generation").read_text(), "proved")
        self.assertFalse(backup.exists())
        self.assertFalse((setup.ENGINE / ".kokoro.transaction.json").exists())

    def test_runtime_directory_symlinks_are_rejected(self):
        runtime = Path(self.temp.name, "omarchy-tts")
        destination = Path(self.temp.name, "runtime-destination")
        destination.mkdir()
        runtime.symlink_to(destination, target_is_directory=True)
        commands = ([SPEAK, "--status"], [ROOT / "bin" / "speak-voice", "list"],
                    [SETUP, "status"])
        for command in commands:
            result = subprocess.run(command, env=self.env, text=True,
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn("symlink", result.stderr.lower(), command)

    def test_speech_state_publication_refuses_non_regular_destination(self):
        status = Path(self.temp.name, "omarchy-tts", "status")
        status.mkdir(parents=True)
        result = self.run_speak("hello", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("register speech preparation", result.stderr)
        self.assertTrue(status.is_dir())
        self.assertEqual(list(status.iterdir()), [])

    def test_every_bundled_provider_has_a_silent_verification_path(self):
        for provider in (ROOT / "providers").iterdir():
            if not provider.is_file():
                continue
            source = provider.read_text()
            self.assertTrue(
                "TTS_SILENT" in source or "play_raw" in source or "play_file" in source,
                f"{provider.name} bypasses the non-audible verification contract",
            )

    def test_elevenlabs_verify_initializes_voice_for_environment_keys(self):
        providers = Path(self.temp.name, "omarchy-tts", "providers")
        providers.mkdir(parents=True, exist_ok=True)
        provider = providers / "elevenlabs"
        provider.write_text(
            "#!/usr/bin/env bash\n# kind: cloud\n"
            "if [[ ${1:-} == --voices ]]; then\n"
            "  printf '%s\\n' '[{\"value\":\"account-voice\",\"label\":\"Account voice\"}]'\n"
            "  exit 0\n"
            "fi\n"
            "[[ ${TTS_VOICE:-} == account-voice ]] || exit 3\n"
            "cat >/dev/null\n"
        )
        provider.chmod(0o755)
        result = self.run_speak("--verify", "elevenlabs")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = Path(self.temp.name, "omarchy-tts", "config.json")
        self.assertEqual(json.loads(config.read_text())["elevenlabs"]["voiceId"],
                         "account-voice")

    def test_test_dock_uses_the_same_sanitizer_as_normal_speech(self):
        controller = (ROOT / "components" / "TtsController.qml").read_text()
        speak_line = next(line for line in controller.splitlines()
                          if "function speak(text)" in line)
        self.assertNotIn("--raw", speak_line)

    def test_setup_registers_worker_identity_before_reporting_started(self):
        """start() must not acknowledge until the worker has published its
        PID and start-time identity. The launcher seam stands in for the real
        worker so no install target - and no privilege prompt - is needed."""
        fake_worker = Path(self.temp.name, "fake-worker.py")
        fake_worker.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "state = Path(os.environ['FAKE_WORKER_STATE'])\n"
            "identity = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]\n"
            "base = {'pid': os.getpid(), 'processIdentity': identity, 'target': sys.argv[1], 'cancellable': True}\n"
            "state.write_text(json.dumps({'status': 'running', 'step': 'prepare', 'progress': 2, 'message': 'Preparing', **base}))\n"
            "time.sleep(0.2)\n"
            "state.write_text(json.dumps({'status': 'done', 'step': 'complete', 'progress': 100, 'message': 'Ready', **base}))\n")
        with unittest.mock.patch.dict(os.environ, self.env):
            setup = load_setup_module()
            setup.ensure_private_runtime()
            os.environ["FAKE_WORKER_STATE"] = str(setup.STATE)
            setup.worker_command = lambda target: [sys.executable, str(fake_worker), target]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                setup.start("piper")
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"], payload)
        registered = json.loads(setup.STATE.read_text())
        self.assertEqual(registered["pid"], payload["pid"])
        self.assertTrue(registered["processIdentity"])
        for _ in range(100):
            current = json.loads(setup.STATE.read_text())
            if current["status"] in {"done", "error"}:
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "done", current)

    def test_engines_refuse_unverified_model_files_before_loading(self):
        """A model file the manifest does not vouch for, or one that no longer
        matches its digest, stops the engine before any Python starts."""
        tools = Path(self.temp.name, "engine-tools")
        tools.mkdir()
        marker = Path(self.temp.name, "python-ran")
        fake_python = tools / "python"
        fake_python.write_text("#!/usr/bin/env bash\nprintf ran > \"$ENGINE_MARKER\"\nexit 0\n")
        fake_python.chmod(0o755)
        env = {**os.environ, "TTS_PLUGIN_DIR": str(ROOT), "TTS_CONFIG": str(Path(self.temp.name, "c.json")),
               "TTS_SILENT": "1", "ENGINE_MARKER": str(marker)}
        Path(self.temp.name, "c.json").write_text("{}")
        # EasyOCR: an unknown .pth in the model directory.
        models = Path(self.temp.name, "easyocr-models")
        models.mkdir()
        (models / "surprise.pth").write_bytes(b"x")
        result = subprocess.run([ROOT / "ocr" / "easyocr"], input=(ROOT / "lib" / "ocr-probe.png").read_bytes(),
                                capture_output=True, env={**env, "EASYOCR_PYTHON": str(fake_python),
                                                          "EASYOCR_MODELS": str(models), "TTS_OCR_LANGS": "eng"})
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"not a verified model file", result.stderr)
        self.assertFalse(marker.exists())
        # EasyOCR: a known name whose bytes changed.
        (models / "surprise.pth").unlink()
        (models / "english_g2.pth").write_bytes(b"tampered")
        result = subprocess.run([ROOT / "ocr" / "easyocr"], input=(ROOT / "lib" / "ocr-probe.png").read_bytes(),
                                capture_output=True, env={**env, "EASYOCR_PYTHON": str(fake_python),
                                                          "EASYOCR_MODELS": str(models), "TTS_OCR_LANGS": "eng"})
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertFalse(marker.exists())
        # Kokoro: the model directory has the right names with the wrong bytes.
        kmodels = Path(self.temp.name, "kokoro-models")
        (kmodels / "voices").mkdir(parents=True)
        for name in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
            (kmodels / name).write_bytes(b"tampered")
        result = subprocess.run([ROOT / "providers" / "kokoro"], input=b"Probe.",
                                capture_output=True, env={**env, "KOKORO_PYTHON": str(fake_python),
                                                          "KOKORO_MODELS": str(kmodels), "TTS_VOICE": "af_heart"})
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertFalse(marker.exists())

    def test_internal_background_workers_require_a_supervised_lock(self):
        setup = subprocess.run(
            [SETUP, "_worker", "piper"], env=self.env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(setup.returncode, 0)
        self.assertIn("inherited lock", setup.stderr)

        voice = subprocess.run(
            [ROOT / "bin" / "speak-voice", "_download",
             "en_US-test-medium", "no"],
            env={**self.env, "HOME": self.temp.name},
            capture_output=True, text=True,
        )
        self.assertNotEqual(voice.returncode, 0)
        self.assertIn("inherited lock", voice.stderr)

    def test_setup_and_download_locks_never_follow_symlinks(self):
        runtime = Path(self.temp.name, "omarchy-tts")
        runtime.mkdir(parents=True, exist_ok=True)
        victim = Path(self.temp.name, "lock-victim")
        victim.write_text("keep me")

        setup_lock = runtime / "setup.lock"
        setup_lock.symlink_to(victim)
        result = subprocess.run(
            [SETUP, "start", "piper"], env=self.env,
            capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout)["code"], "lock_error")
        self.assertEqual(victim.read_text(), "keep me")

        setup_lock.unlink()
        download_lock = runtime / "download.lock"
        download_lock.symlink_to(victim)
        result = subprocess.run(
            [ROOT / "bin" / "speak-voice", "add",
             "en_US-test-medium", "--async"],
            env={**self.env, "HOME": self.temp.name},
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock symlink", result.stderr)
        self.assertEqual(victim.read_text(), "keep me")


class KokoroVerifiedRecordTests(unittest.TestCase):
    """Per-press model verification is stat-cheap on a current .verified
    record; any press without one pays a full digest re-verification that
    republishes the record on a pass."""

    PROVIDER = "kokoro"
    SECTION = "kokoro"
    ENV_PY = "KOKORO_PYTHON"
    ENV_MODELS = "KOKORO_MODELS"
    NAMES = ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt")
    MAIN_NAME = "config.json"
    HEAVY_NAME = "kokoro-v1_0.pth"
    VOICE_NAME = "voices/af_heart.pt"
    PAYLOADS = {"config.json": b"{}\n",
                "kokoro-v1_0.pth": b"fake-model-bytes",
                "voices/af_heart.pt": b"fake-voice-bytes"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        temp = Path(self.temp.name)
        self.plugin = temp / "plugin"
        (self.plugin / "lib" / "engines").mkdir(parents=True)
        shutil.copy(ROOT / "lib" / "play.sh", self.plugin / "lib" / "play.sh")
        shutil.copy(ROOT / "lib" / "cache.sh", self.plugin / "lib" / "cache.sh")
        self.models = temp / "models"
        self.models.mkdir(parents=True)
        if any("/" in name for name in self.NAMES):
            (self.models / "voices").mkdir(parents=True)
        self.payloads = {**self.PAYLOADS}
        for name, payload in self.payloads.items():
            (self.models / name).write_bytes(payload)
        manifest = {self.SECTION: {"repo": "example/fake", "revision": "0" * 40, "files": {
            name: {"sha256": hashlib.sha256(self.payloads[name]).hexdigest(),
                   "size": len(self.payloads[name])}
            for name in self.NAMES}}}
        (self.plugin / "lib" / "engines" / "models.json").write_text(json.dumps(manifest))

        self.tools = temp / "engine-tools"
        self.tools.mkdir()
        self.digest_marks = temp / "digest-ran"
        real_digest = shutil.which("sha256sum")
        self.assertNotEqual(real_digest, None)
        (self.tools / "sha256sum").write_text(
            f'#!/usr/bin/env bash\nprintf ran >> "$DIGEST_MARK"\nexec {real_digest} "$@"\n')
        (self.tools / "sha256sum").chmod(0o755)
        self.python_ran = temp / "python-ran"
        self.python = self.tools / "python"
        self.python.write_text(f'#!/usr/bin/env bash\ntouch "{self.python_ran}"\n')
        self.python.chmod(0o755)
        (temp / "config.json").write_text("{}")
        self.record = self.models.parent / ".verified"

    def tearDown(self):
        self.temp.cleanup()

    def run_kokoro(self):
        return subprocess.run([ROOT / "providers" / self.PROVIDER], input=b"Probe.",
                              capture_output=True, check=False,
                              env={**os.environ, "TTS_PLUGIN_DIR": str(self.plugin),
                                   self.ENV_PY: str(self.python),
                                   self.ENV_MODELS: str(self.models),
                                   "TTS_VOICE": "af_heart", "TTS_SILENT": "1",
                                   "TTS_CONFIG": str(Path(self.temp.name, "config.json")),
                                   "DIGEST_MARK": str(self.digest_marks),
                                   # The cache key digests the text, not the
                                   # models; these tests measure verification
                                   # cost specifically, so the cache stays off.
                                   "TTS_CACHE": "0",
                                   "PATH": str(self.tools) + os.pathsep + os.environ["PATH"]})

    def fingerprint(self, name=None):
        metadata = (self.models / name).stat()
        seconds, nanoseconds = divmod(metadata.st_mtime_ns, 1_000_000_000)
        return f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:{seconds}.{nanoseconds:09d}"

    def write_record(self, fingerprints=None, raw=None):
        if raw is not None:
            self.record.write_text(raw)
            self.record.chmod(0o600)
            return
        record = {"version": 1, "files": {}}
        for name in self.NAMES:
            record["files"][name] = {
                "sha256": hashlib.sha256(self.payloads[name]).hexdigest(),
                "fingerprint": self.fingerprint(name)}
        if fingerprints is not None:
            for name, fingerprint in fingerprints.items():
                record["files"][name]["fingerprint"] = fingerprint
        self.record.write_text(json.dumps(record, sort_keys=True) + "\n")
        self.record.chmod(0o600)

    def test_current_record_passes_without_a_digest_read(self):
        self.write_record()
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.python_ran.exists())
        self.assertFalse(self.digest_marks.exists())
        self.assertEqual((self.models / self.HEAVY_NAME).read_bytes(),
                         self.payloads[self.HEAVY_NAME])

    def test_missing_record_redigests_and_republishes(self):
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.digest_marks.exists())
        record = json.loads(self.record.read_text())
        self.assertEqual(record["version"], 1)
        for name in self.NAMES:
            self.assertEqual(record["files"][name]["fingerprint"], self.fingerprint(name))
            self.assertEqual(record["files"][name]["sha256"],
                             hashlib.sha256(self.payloads[name]).hexdigest())
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)

    def test_stale_record_redigests_and_republishes(self):
        self.write_record(fingerprints={self.MAIN_NAME: self.fingerprint(self.MAIN_NAME).replace(":", ".1", 2)})
        before = self.record.read_text()
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.digest_marks.exists())
        self.assertNotEqual(self.record.read_text(), before)
        self.assertEqual(json.loads(self.record.read_text())["files"][self.MAIN_NAME]["fingerprint"],
                         self.fingerprint(self.MAIN_NAME))
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)

    def test_stale_record_fails_loudly_on_tampered_bytes(self):
        self.write_record(fingerprints={self.HEAVY_NAME: self.fingerprint(self.HEAVY_NAME) + "9"})
        (self.models / self.HEAVY_NAME).write_bytes(b"tampered-bytes")
        before = self.record.read_text()
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertTrue(self.digest_marks.exists())
        self.assertFalse(self.python_ran.exists())
        self.assertEqual(self.record.read_text(), before,
                         "a failed re-verification must republish nothing")

    def test_record_never_follows_a_symlink(self):
        victim = Path(self.temp.name, "victim")
        victim.write_text("keep me")
        self.record.unlink(missing_ok=True)
        self.record.symlink_to(victim)
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.digest_marks.exists())
        self.assertEqual(victim.read_text(), "keep me")
        self.assertFalse(self.record.is_symlink(), "a passing re-verification replaces the symlink")
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)

        victim.write_text("still intact")
        self.record.unlink(missing_ok=True)
        self.record.symlink_to(victim)
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)  # digests still match
        self.assertEqual(victim.read_text(), "still intact")

        # And when the digest fallback cannot pass, the victim stays untouched.
        victim.write_text("keep me")
        (self.models / self.VOICE_NAME).write_bytes(b"tampered-voice")
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertEqual(victim.read_text(), "keep me")

    def test_corrupt_record_behaves_like_a_missing_one(self):
        self.write_record(raw="{not json at all\n")
        (self.models / self.VOICE_NAME).write_bytes(b"tampered-voice")
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn(b"failed verification", result.stderr)
        self.assertTrue(self.digest_marks.exists())

    def test_installer_publishes_the_record_the_provider_accepts(self):
        # Real manifest structure, small stand-in files: the record the
        # installer publishes must be exactly what the provider accepts.
        setup = load_setup_module()
        models = Path(self.temp.name, "installer-models")
        real_manifest = json.loads((ROOT / "lib" / "engines" / "models.json").read_text())
        for name in real_manifest[self.SECTION]["files"]:
            (models / name).parent.mkdir(parents=True, exist_ok=True)
            (models / name).write_bytes(b"stand-in")
        setup.record_model_verification(self.SECTION, models)
        record = models.parent / ".verified"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        published = json.loads(record.read_text())
        self.assertEqual(set(published["files"]),
                         set(real_manifest[self.SECTION]["files"]))
        self.models = models
        self.record = record
        result = self.run_kokoro()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.digest_marks.exists(),
                         "an installer-published record must make the press cheap")


class KokoroOnnxRecordTests(KokoroVerifiedRecordTests):
    """The ONNX engine's provider pays the same fingerprint check and the
    same fallback-and-republish behavior for the pinned graph and voice-pack."""

    PROVIDER = "kokoro-onnx"
    SECTION = "kokoro-onnx"
    ENV_PY = "KOKORO_ONNX_PYTHON"
    ENV_MODELS = "KOKORO_ONNX_MODELS"
    NAMES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
    MAIN_NAME = "kokoro-v1.0.onnx"
    HEAVY_NAME = "kokoro-v1.0.onnx"
    VOICE_NAME = "voices-v1.0.bin"
    PAYLOADS = {"kokoro-v1.0.onnx": b"fake-graph-bytes",
                "voices-v1.0.bin": b"fake-voicepack-bytes"}


class SpeakDaemonTests(unittest.TestCase):
    """The resident daemon serves the provider contract over a private unix
    socket: bounded JSON requests, s16le PCM replies, refused requests closed
    empty, idle self-exit and an ended daemon when the engine file changes."""

    temp: tempfile.TemporaryDirectory

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend_file = Path(self.temp.name, "backend.py")
        self.samples = [bytes(range(64, 96)), bytes(32) * 96]
        self.backend_file.write_text(
            "PCM = bytes(range(64, 96))\n"
            "def synthesize(text, voice, speed, cancelled):\n"
            "    yield PCM\n"
            "    if text == 'two chunks':\n"
            "        yield bytes(32) * 96\n")

    def tearDown(self):
        self.temp.cleanup()

    def socket_path(self) -> Path:
        return Path(self.temp.name, "provider.sock")

    def start_daemon(self, *args, engine="kokoro", explicit_socket=True) -> subprocess.Popen:
        command = [sys.executable, str(DAEMON), "--engine", engine, *args]
        if explicit_socket:
            command += ["--socket", str(self.socket_path())]
        return subprocess.Popen(
            command,
            env={**os.environ, "XDG_RUNTIME_DIR": self.temp.name},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_for_socket(self, process: subprocess.Popen = None,
                        timeout: float = 5.0) -> bool:
        path = self.socket_path()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                return False
            if path.is_socket():
                return True
            time.sleep(0.02)
        return False

    def daemon_stderr(self, process: subprocess.Popen = None) -> str:
        assert process is not None and process.poll() is not None
        return process.stderr.read().decode()

    def request(self, raw: bytes) -> bytes:
        """One press: send a header, read the whole reply."""
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(str(self.socket_path()))
        sock.sendall(raw + b"\n")
        reply = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply += chunk
        sock.close()
        return reply

    def header(self, **overrides) -> bytes:
        request = {"voice": "af_heart", "speed": 1.0, "text": "Probe."}
        request.update(overrides)
        return json.dumps(request).encode()

    def test_serves_pcm_over_the_private_socket(self):
        daemon = self.start_daemon("--test-backend", str(self.backend_file))
        try:
            ready = self.wait_for_socket(daemon)
            if not ready:
                self.fail(self.daemon_stderr(daemon))
            self.assertEqual(self.request(self.header()), self.samples[0])
            self.assertEqual(self.request(self.header(text="two chunks")),
                             self.samples[0] + self.samples[1])
            self.assertEqual(daemon.poll(), None, "the daemon keeps serving")
        finally:
            daemon.terminate()
            daemon.wait(timeout=5)

    def test_refused_requests_close_with_no_bytes(self):
        daemon = self.start_daemon("--test-backend", str(self.backend_file))
        try:
            ready = self.wait_for_socket(daemon)
            if not ready:
                self.fail(self.daemon_stderr(daemon))
            for label, raw in (("not json", b"{oops"),
                               ("not an object", b"[]"),
                               ("bad voice", self.header(voice="BAD")),
                               ("empty text", self.header(text="   ")),
                               ("speed NaN", b'{"voice":"af_heart","speed":NaN,"text":"x"}')):
                with self.subTest(label):
                    self.assertEqual(self.request(raw), b"", label)
            self.assertEqual(daemon.poll(), None, "refusals keep the daemon alive")
        finally:
            daemon.terminate()
            daemon.wait(timeout=5)

    def test_requests_are_bounded(self):
        daemon_module = load_daemon_module()
        import socket

        def pair():
            return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        losses = [
            ("oversized header",
             b'{"voice":"af_heart","speed":1.0,"text":"' + b"a" * (1 << 20) + b'"}\n'),
            ("oversized text",
             b'{"voice":"af_heart","speed":1.0,"text":"' + b"a" * (200_001) + b'"}\n'),
            ("no trailing newline",
             b'{"voice":"af_heart","speed":1.0,"text":"x"}'),
        ]
        for label, raw in losses:
            with self.subTest(label):
                client, server = pair()
                sender = threading.Thread(target=client.sendall, args=(raw,))
                sender.start()  # buffers may not hold `raw`; read while sending
                if label == "no trailing newline":
                    client.shutdown(socket.SHUT_WR)  # EOF, never a header end
                with self.assertRaises(daemon_module.RequestError, msg=label):
                    daemon_module.read_request(server)
                sender.join(timeout=5)
                client.close()
                server.close()

        # The good case passes every bound.
        client, server = pair()
        client.sendall(self.header() + b"\n")
        request = daemon_module.read_request(server)
        self.assertEqual(request["text"], "Probe.")
        client.close()
        server.close()

    def test_daemon_self_exits_after_idle(self):
        daemon = self.start_daemon("--test-backend", str(self.backend_file),
                                   "--idle-exit", "1")
        if not self.wait_for_socket(daemon):
            self.fail(self.daemon_stderr(daemon))
        self.assertEqual(daemon.wait(timeout=10), 0, self.daemon_stderr(daemon))
        self.assertFalse(self.wait_for_socket(timeout=0.5))

    def test_a_live_daemon_is_never_taken_over(self):
        daemon = self.start_daemon("--test-backend", str(self.backend_file))
        try:
            ready = self.wait_for_socket(daemon)
            if not ready:
                self.fail(self.daemon_stderr(daemon))
            rival = self.start_daemon("--test-backend", str(self.backend_file))
            rival.wait(timeout=10)
            self.assertNotEqual(rival.returncode, 0)
        finally:
            daemon.terminate()
            daemon.wait(timeout=5)

    def test_a_symlinked_socket_path_is_refused(self):
        victim = Path(self.temp.name, "victim")
        victim.write_text("keep me")
        runtime = Path(self.temp.name, "omarchy-tts")
        runtime.mkdir()
        os.symlink(victim, runtime / "kokoro.sock")
        daemon = self.start_daemon("--idle-exit", "5",
                                   explicit_socket=False)  # default path: the symlink
        out, err = daemon.communicate(timeout=10)
        self.assertNotEqual(daemon.returncode, 0)
        self.assertIn(b"refusing symlink", err)
        self.assertEqual(victim.read_text(), "keep me",
                         "the symlink target is never touched")

    def test_engine_replacement_ends_the_daemon(self):
        daemon_module = load_daemon_module()
        import socket

        class ReplacingBackend:
            def check_pins(self):
                raise daemon_module.EngineChanged("engine replaced")

            def synthesize(self, text, voice, speed, cancelled):
                raise RuntimeError("must never synthesize")

        replacements = unittest.mock.patch.object(
            daemon_module, "make_backend",
            lambda engine, test_backend: ReplacingBackend())
        socket_path = self.socket_path()
        outcomes = []

        def run():
            outcomes.append(daemon_module.serve("kokoro", socket_path, 60, None))

        with replacements:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(self.wait_for_socket(), "socket became ready")
                self.assertEqual(self.request(self.header()), b"",
                                 "an engine change refuses the request")
                thread.join(timeout=10)
            finally:
                pass
            self.assertFalse(thread.is_alive(), "the daemon ends after the change")
            self.assertEqual(outcomes[-1], 0)
            # And it stays ended for a fresh client.
            with self.assertRaises(OSError):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(str(socket_path))
                sock.sendall(self.header() + b"\n")
                sock.recv(1)

    def test_engine_fingerprint_notices_a_replaced_model(self):
        daemon_module = load_daemon_module()
        models = Path(self.temp.name, "models")
        models.mkdir()
        model = models / "kokoro-v1_0.pth"
        model.write_bytes(b"installed")
        pin = daemon_module.fingerprint(model)
        backend = daemon_module.TorchKokoro(models)
        backend._pins = [(model, pin)]
        backend._ready = True
        # stat-cheap while the file is untouched: no digest read is paid.
        backend.check_pins()
        os.utime(model, (1000, 1001))
        with self.assertRaises(daemon_module.EngineChanged):
            backend.check_pins()


class ProviderDaemonFastPathTests(unittest.TestCase):
    """With a resident daemon answering, a provider press is served over the
    socket and the engine interpreter never boots; without one, the press
    falls back to the inline synthesis it has always used."""

    PROVIDER = "kokoro"
    LOCK = "kokoro.lock"
    ENV_PY = "KOKORO_PYTHON"
    SECTION = "kokoro"
    NAMES = ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt")
    PAYLOADS = {"config.json": b"{}\n",
                "kokoro-v1_0.pth": b"fake-model-bytes",
                "voices/af_heart.pt": b"fake-voice-bytes"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        temp = Path(self.temp.name)
        self.plugin = temp / "plugin"
        (self.plugin / "lib" / "engines").mkdir(parents=True)
        shutil.copy(ROOT / "lib" / "play.sh", self.plugin / "lib" / "play.sh")
        shutil.copy(ROOT / "lib" / "cache.sh", self.plugin / "lib" / "cache.sh")
        shutil.copy(ROOT / "lib" / "engines" / self.LOCK,
                    self.plugin / "lib" / "engines" / self.LOCK)
        self.payloads = {**self.PAYLOADS}
        self.data = temp / "data"
        engine = self.data / "engines" / self.PROVIDER
        (engine / "bin").mkdir(parents=True)
        (engine / "models" / "voices").mkdir(parents=True)
        self.python_marker = temp / "engine-interpreter-ran"
        stub = engine / "bin" / "python"
        # The stub is a stand-in engine interpreter: it records that synthesis
        # ran, and emits real PCM-like bytes (the cache only stores audio).
        stub.write_text(f'#!/usr/bin/env bash\ntouch "{self.python_marker}"\n'
                        'printf \'\\x01\\x00\\x02\\x00\\xff\\x7f\\x00\\x00\'\n')
        stub.chmod(0o755)
        for name, payload in self.payloads.items():
            (engine / "models" / name).write_bytes(payload)
        (engine / ".lock-sha256").write_text(
            hashlib.sha256((self.plugin / "lib" / "engines" / self.LOCK)
                           .read_bytes()).hexdigest() + "\n")
        manifest = {self.SECTION: {"repo": "example/fake", "revision": "0" * 40,
                                   "files": {name: {"sha256": hashlib.sha256(payload).hexdigest(),
                                                    "size": len(payload)}
                                             for name, payload in self.payloads.items()}}}
        (self.plugin / "lib" / "engines" / "models.json").write_text(json.dumps(manifest))
        (temp / "config.json").write_text("{}")
        self.backend_file = temp / "backend.py"
        self.backend_file.write_text(
            "def synthesize(text, voice, speed, cancelled):\n"
            "    yield b'\\x00\\x01' * 600\n")

    def tearDown(self):
        if getattr(self, "daemon", None) is not None:
            self.daemon.terminate()
            self.daemon.wait(timeout=5)
        self.temp.cleanup()

    def run_provider(self, **env_extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [ROOT / "providers" / self.PROVIDER], input=b"Probe.",
            capture_output=True, check=False,
            env={**os.environ, "TTS_PLUGIN_DIR": str(self.plugin),
                 "TTS_DATA_DIR": str(self.data),
                 "TTS_VOICE": "af_heart", "TTS_RATE": "1.0",
                 "TTS_SILENT": "1", "TTS_CONFIG": str(Path(self.temp.name, "config.json")),
                 "XDG_CACHE_HOME": str(Path(self.temp.name, "cache")),
                 "XDG_RUNTIME_DIR": self.temp.name, **env_extra})

    def start_daemon(self):
        self.daemon = subprocess.Popen(
            [sys.executable, str(DAEMON), "--engine", self.PROVIDER,
             "--test-backend", str(self.backend_file), "--idle-exit", "120"],
            env={**os.environ, "XDG_RUNTIME_DIR": self.temp.name},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sock = Path(self.temp.name, "omarchy-tts", f"{self.PROVIDER}.sock")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sock.is_socket():
                return
            time.sleep(0.02)
        self.fail("daemon socket never became ready")

    def test_press_is_served_by_the_daemon(self):
        self.start_daemon()
        result = self.run_provider()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.python_marker.exists(),
                         "the daemon serves the press; no interpreter boots")

    def test_press_falls_back_inline_without_a_daemon(self):
        self.daemon = None
        result = self.run_provider()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.python_marker.exists(),
                        "without a daemon the inline path speaks")


class KokoroOnnxDaemonFastPathTests(ProviderDaemonFastPathTests):
    PROVIDER = "kokoro-onnx"
    LOCK = "kokoro-onnx.lock"
    ENV_PY = "KOKORO_ONNX_PYTHON"
    SECTION = "kokoro-onnx"
    NAMES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
    PAYLOADS = {"kokoro-v1.0.onnx": b"fake-graph-bytes",
                "voices-v1.0.bin": b"fake-voicepack-bytes"}


class SynthesisCacheUnitTests(unittest.TestCase):
    """The on-disk synthesis cache: deterministic keying over (engine, voice,
    speed, exact text bytes), hit/miss correctness, owner-only permissions,
    age eviction, LRU eviction and refusal of symlinked cache paths."""

    temp: tempfile.TemporaryDirectory

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache_home = Path(self.temp.name, "cache-home")
        # Hermetic: an explicit cache home replaces whatever this shell
        # inherits; PATH still carries the coreutils the library uses.
        self.env = {"PATH": os.environ["PATH"],
                    "HOME": str(self.temp.name),
                    "TTS_CACHE_HOME": str(self.cache_home)}

    def tearDown(self):
        self.temp.cleanup()

    def cache(self, script: str, **env_extra) -> subprocess.CompletedProcess:
        # `bash -u` keeps every unset-variable slip in the library fatal.
        return subprocess.run(
            ["bash", "-u", "-c",
             "source " + shlex.quote(str(ROOT / "lib" / "cache.sh")) + "\n" + script],
            env={**self.env, **env_extra}, capture_output=True, text=True)

    def audio_dir(self) -> Path:
        return self.cache_home / "omarchy-tts" / "audio"

    def entries(self) -> list[str]:
        if not self.audio_dir().is_dir():
            return []
        return sorted(p.name for p in self.audio_dir().iterdir())

    def key_of(self, text: str, voice="af_heart", provider="kokoro") -> str:
        result = self.cache(
            f"printf {shlex.quote(text)} | audio_cache_key {provider} {voice} 1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_keys_are_stable_and_sensitive(self):
        result = self.cache(r'''
        k1=$(printf 'same' | audio_cache_key kokoro af_heart 1.0)
        k2=$(printf 'same' | audio_cache_key kokoro af_heart 1.0)
        kv=$(printf 'same' | audio_cache_key kokoro af_bella 1.0)
        kr=$(printf 'same' | audio_cache_key kokoro af_heart 1.25)
        kp=$(printf 'same' | audio_cache_key piper af_heart 1.0)
        kt=$(printf 'diff' | audio_cache_key kokoro af_heart 1.0)
        [ "$k1" = "$k2" ] && [ ${#k1} -eq 64 ] && echo STABLE-HEX
        [ "$k1" != "$kv" ] && [ "$k1" != "$kr" ] && \
          [ "$k1" != "$kp" ] && [ "$k1" != "$kt" ] && echo SENSITIVE
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STABLE-HEX", result.stdout)
        self.assertIn("SENSITIVE", result.stdout)

    def test_miss_then_store_then_hit(self):
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        [ -z "$(audio_cache_lookup kokoro "$key")" ] && echo MISS
        stage=$(audio_cache_stage kokoro "$key")
        case "$stage" in *".inflight-$key.part") echo STAGED ;; esac
        printf 'AUDIO' > "$stage"
        audio_cache_commit kokoro "$key" "$stage" && echo STORED
        [ ! -e "$stage" ] && echo STAGE-GONE
        got=$(audio_cache_lookup kokoro "$key")
        [ "$(cat "$got")" = AUDIO ] && echo HIT
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for want in ("MISS", "STAGED", "STORED", "STAGE-GONE", "HIT"):
            self.assertIn(want, result.stdout, result.stdout + result.stderr)

    def test_bad_keys_are_refused(self):
        result = self.cache(r'''
        stage=$(audio_cache_stage kokoro "not-a-hash")
        [ -z "$stage" ] && echo REFUSED-STAGE
        true
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REFUSED-STAGE", result.stdout)
        self.assertEqual(self.entries(), [])

    def test_a_disabled_cache_refuses_every_perspective(self):
        result = self.cache(r'''
        audio_cache_enabled || echo OFF
        stage=$(audio_cache_stage kokoro abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789)
        [ -z "$stage" ] && echo NO-STAGE
        [ -z "$(audio_cache_lookup kokoro abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789)" ] \
          && echo NO-LOOKUP
        ''', TTS_CACHE="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        for want in ("OFF", "NO-STAGE", "NO-LOOKUP"):
            self.assertIn(want, result.stdout)

    def test_cache_files_are_owner_only(self):
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key"); printf 'AUDIO' > "$stage"
        audio_cache_commit kokoro "$key" "$stage"
        p=$(audio_cache_lookup kokoro "$key")
        [ "$(stat -c %a "$p")" = 600 ] && echo FILE600
        [ "$(stat -c %a "$(dirname "$p")")" = 700 ] && echo DIR700
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FILE600", result.stdout)
        self.assertIn("DIR700", result.stdout)

    def test_aged_entries_expire(self):
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key"); printf 'AUDIO' > "$stage"
        audio_cache_commit kokoro "$key" "$stage"
        touch -d '31 days ago' "$(audio_cache_dir)/kokoro-$key.pcm"
        [ -z "$(audio_cache_lookup kokoro "$key")" ] && echo EXPIRED-BY-LOOKUP
        audio_cache_trim
        [ $(ls "$(audio_cache_dir)"/*.pcm 2>/dev/null | wc -l) -eq 0 ] && echo TRIM-GONE
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EXPIRED-BY-LOOKUP", result.stdout)
        self.assertIn("TRIM-GONE", result.stdout)

    def test_deadline_hits_refresh_mtime(self):
        # A hit refreshes mtime, so an entry still in use does not age out.
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key"); printf 'AUDIO' > "$stage"
        audio_cache_commit kokoro "$key" "$stage"
        touch -d '25 days ago' "$(audio_cache_dir)/kokoro-$key.pcm"
        aged=$(stat -c %Y "$(audio_cache_dir)/kokoro-$key.pcm")
        audio_cache_lookup kokoro "$key" >/dev/null
        after=$(stat -c %Y "$(audio_cache_dir)/kokoro-$key.pcm")
        if [ "$after" -gt "$aged" ]; then echo TOUCHED; fi
        true
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOUCHED", result.stdout)

    def test_lru_cap_evicts_the_oldest_to_fit(self):
        result = self.cache(r'''
        for label in a b c; do
          key=$(printf "$label" | audio_cache_key kokoro af_heart 1.0)
          stage=$(audio_cache_stage kokoro "$key")
          printf '%128s' x > "$stage"
          audio_cache_commit kokoro "$key" "$stage"
          sleep 0.05   # distinct mtimes make eviction order observable
        done
        TTS_CACHE_LIMIT_MB=0 audio_cache_trim   # zero budget: everything fits nowhere
        [ $(ls "$(audio_cache_dir)"/*.pcm 2>/dev/null | wc -l) -eq 0 ] && echo ALL-EVICTED
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALL-EVICTED", result.stdout)

    def test_lru_cap_keeps_the_newest(self):
        # A tight budget keeps the newest entries and drops the older ones
        # first — entries LRU-ordered by their refreshed mtime.
        result = self.cache(r'''
        for label in a b c d; do
          key=$(printf "$label" | audio_cache_key kokoro af_heart 1.0)
          stage=$(audio_cache_stage kokoro "$key")
          head -c 700000 /dev/zero | tr '\0' x > "$stage"
          audio_cache_commit kokoro "$key" "$stage"
          sleep 0.05
        done
        dir=$(audio_cache_dir)
        for each in a b c d; do
          key=$(printf "$each" | audio_cache_key kokoro af_heart 1.0)
          if [ -f "$dir/kokoro-$key.pcm" ]; then printf %s "$each"; fi
        done
        echo ''
        ''', TTS_CACHE_LIMIT_MB="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "d",
                         "the 1 MiB budget keeps only the newest entry")

    def test_stale_inflight_stages_are_reclaimed(self):
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key"); printf 'HALF' > "$stage"
        touch -d '2 hours ago' "$stage"
        audio_cache_trim
        [ ! -e "$stage" ] && echo RECLAIMED
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RECLAIMED", result.stdout)

    def test_a_symlinked_audio_dir_is_refused(self):
        target = Path(self.temp.name, "elsewhere")
        target.mkdir()
        parent = self.audio_dir().parent
        parent.mkdir(parents=True)
        os.symlink(target, self.audio_dir())
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key")
        [ -z "$stage" ] && echo REFUSED
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REFUSED", result.stdout)
        self.assertEqual(list(target.iterdir()), [], "nothing written through the link")

    def test_a_symlinked_entry_is_refused(self):
        result = self.cache(r'''
        key=$(printf 'press' | audio_cache_key kokoro af_heart 1.0)
        stage=$(audio_cache_stage kokoro "$key"); printf 'AUDIO' > "$stage"
        audio_cache_commit kokoro "$key" "$stage"
        found=$(audio_cache_lookup kokoro "$key")
        [ -n "$found" ] && echo HIT
        mv "$found" "$found.real"
        ln -s /nonexistent "$found"
        [ -z "$(audio_cache_lookup kokoro "$key")" ] && echo LINK-REFUSED
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HIT", result.stdout)
        self.assertIn("LINK-REFUSED", result.stdout)

    def test_the_cache_can_be_opted_out(self):
        result = self.cache(r'''
        audio_cache_enabled || echo OFF
        [ -z "$(audio_cache_stage kokoro abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789)" ] \
          && echo NO-STAGE
        ''', TTS_CACHE="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFF", result.stdout)
        self.assertIn("NO-STAGE", result.stdout)


class ProviderCacheTests(ProviderDaemonFastPathTests):
    """A repeated press is played from the cache: no daemon request, no
    interpreter boot, and the stored entry is exactly what was played."""

    def marker_mtime(self):
        return self.python_marker.stat().st_mtime

    def test_repeated_press_serves_from_the_cache(self):
        self.start_daemon()
        first = self.run_provider()
        self.assertEqual(first.returncode, 0, first.stderr)
        audio = Path(self.temp.name, "cache", "omarchy-tts", "audio")
        entries = list(audio.glob("*.pcm")) if audio.is_dir() else []
        self.assertEqual(len(entries), 1, "the served press is cached")
        self.assertFalse(self.python_marker.exists(),
                         "the daemon synthesizes; the interpreter never boots")
        # The daemon and its socket are gone entirely: only the cache remains.
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.daemon = None
        second = self.run_provider(TTS_DAEMON="0")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(self.python_marker.exists(),
                         "a repeated press is served from the cache, "
                         "without booting the engine")

    def test_a_cache_hit_leaves_the_interpreter_untouched(self):
        self.daemon = None
        first = self.run_provider()  # no daemon anywhere: inline path synthesizes
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(self.python_marker.exists())
        before = self.marker_mtime()
        second = self.run_provider()  # same press: straight from the cache
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.marker_mtime(), before,
                         "the cache holds this text; nothing re-synthesized")
        audio = Path(self.temp.name, "cache", "omarchy-tts", "audio")
        self.assertEqual(len(list(audio.glob("*.pcm"))), 1, "no duplicate entries")

    def test_cache_opt_out_synthesizes_every_press(self):
        self.daemon = None
        for _ in range(2):
            result = self.run_provider(TTS_CACHE="0", TTS_DAEMON="0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.python_marker.exists(),
                            "synthesis always runs with the cache off")
        self.assertFalse(list(Path(self.temp.name, "cache").glob("**/*.pcm")),
                         "opted out: nothing is stored")


class DaemonUnitInstallTests(unittest.TestCase):
    """Installing a heavy engine publishes the resident daemon's systemd
    socket and service units and enables the listener — best effort, so an
    absent or broken systemd never fails an installation that proved fine."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.unit_dir = Path(self.temp.name, "systemd", "user")
        self.engine_root = Path(self.temp.name, "engines")
        (self.engine_root / "kokoro" / "bin").mkdir(parents=True)
        (self.engine_root / "kokoro" / "bin" / "python").write_text("stub")
        (self.engine_root / "kokoro-onnx" / "bin").mkdir(parents=True)
        (self.engine_root / "kokoro-onnx" / "bin" / "python").write_text("stub")

    def tearDown(self):
        self.temp.cleanup()

    def install(self, engine="kokoro", missing_python=False):
        setup = load_setup_module()
        units_dir = self.unit_dir
        engine_root = self.engine_root
        if missing_python:
            for engine in ("kokoro", "kokoro-onnx"):
                (engine_root / engine / "bin" / "python").unlink()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with contextlib.ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.object(setup, "CONFIG_HOME",
                                                           Path(self.temp.name)))
            stack.enter_context(unittest.mock.patch.object(setup, "ENGINE", engine_root))
            stack.enter_context(unittest.mock.patch.object(setup, "PLUGIN", ROOT))
            stack.enter_context(unittest.mock.patch.object(setup.subprocess, "run",
                                                           fake_run))
            setup.install_daemon_units(engine)
        return units_dir, calls

    def read(self, name):
        return (self.unit_dir / name).read_text()

    def test_units_are_published_and_the_listener_enabled(self):
        units_dir, calls = self.install()
        self.assertEqual(sorted(p.name for p in units_dir.iterdir()),
                         ["omarchy-tts-kokoro.service", "omarchy-tts-kokoro.socket"])
        self.assertIn("ListenStream=%t/omarchy-tts/kokoro.sock",
                      self.read("omarchy-tts-kokoro.socket"))
        self.assertIn("SocketMode=0600", self.read("omarchy-tts-kokoro.socket"))
        self.assertIn("RemoveOnStop=true", self.read("omarchy-tts-kokoro.socket"))
        service = self.read("omarchy-tts-kokoro.service")
        self.assertIn("--engine kokoro", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("Restart=no", service)
        self.assertIn("kokoro/bin/python", service)
        self.assertIn(str(ROOT / "bin" / "speak-daemon"), service)
        self.assertIn("StartLimitIntervalSec=0", service)
        self.assertEqual(calls[0], ["systemctl", "--user", "daemon-reload"])
        self.assertEqual(calls[1], ["systemctl", "--user", "enable", "--now",
                                    "omarchy-tts-kokoro.socket"])

    def test_unit_files_are_world_readable(self):
        units_dir, _ = self.install()
        for name in ("omarchy-tts-kokoro.socket", "omarchy-tts-kokoro.service"):
            self.assertEqual((units_dir / name).stat().st_mode & 0o777, 0o644, name)

    def test_unknown_engines_have_no_daemon(self):
        with self.assertRaises(RuntimeError):
            self.install(engine="piper")
        self.assertEqual(list(self.unit_dir.glob("*")), [])

    def test_units_are_skipped_when_the_engine_is_gone(self):
        units_dir, calls = self.install(missing_python=True)
        self.assertEqual(list(units_dir.glob("omarchy-tts-*")), [])
        self.assertEqual(calls, [], "nothing is enabled that would point nowhere")

    def test_the_onnx_engine_gets_its_own_units(self):
        _, calls = self.install(engine="kokoro-onnx")
        self.assertEqual(sorted(p.name for p in self.unit_dir.iterdir()),
                         ["omarchy-tts-kokoro-onnx.service",
                          "omarchy-tts-kokoro-onnx.socket"])


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.tools = self.home / "tools"
        self.tools.mkdir()
        self.env = {**os.environ,
                    "PATH": f"{self.tools}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_DATA_HOME": str(self.home / "data"),
                    "XDG_RUNTIME_DIR": str(self.home / "run"),
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"}
        (self.home / "run").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_secret_tool(self, source):
        tool = self.tools / "secret-tool"
        tool.write_text("#!/usr/bin/env bash\n" + source)
        tool.chmod(0o755)

    def test_missing_keyring_helper_is_reported_as_unavailable(self):
        script = (
            'source "$1"\n'
            'PATH=/path-that-does-not-exist\n'
            'get_key TEST_API_KEY test-provider\n'
        )
        env = {**self.env}
        env.pop("TEST_API_KEY", None)
        result = subprocess.run(
            ["/bin/bash", "-c", script, "bash", str(ROOT / "lib" / "keys.sh")],
            env=env, text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret-tool) is unavailable", result.stderr)
        self.assertNotIn("no API key", result.stderr)

    def test_saving_shared_key_invalidates_speech_and_ocr_health(self):
        self.write_secret_tool(
            "case ${1:-} in\n"
            "  store) cat >/dev/null; exit 0 ;;\n"
            "  lookup) exit 1 ;;\n"
            "  clear) exit 0 ;;\n"
            "esac\n"
        )
        health = self.home / "cache" / "omarchy-tts" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text(json.dumps({
            "google": {"status": "ok", "fingerprint": "old"},
            "ocr:google": {"status": "failed", "fingerprint": "old"},
            "piper": {"status": "ok", "fingerprint": "keep"},
        }))
        result = subprocess.run(
            [SETUP, "key-store", "google"], input="valid-test-key\n",
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        updated = json.loads(health.read_text())
        self.assertNotIn("google", updated)
        self.assertNotIn("ocr:google", updated)
        self.assertIn("piper", updated)

    def test_cleanup_reports_keyring_failures_and_returns_nonzero(self):
        self.write_secret_tool(
            "case ${1:-} in\n"
            "  lookup) exit 0 ;;\n"
            "  clear) exit 7 ;;\n"
            "  store) cat >/dev/null; exit 0 ;;\n"
            "esac\n"
        )
        result = subprocess.run(
            [SETUP, "cleanup", "--yes"], env=self.env,
            text=True, capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("API key" in failure for failure in payload["failures"]))

    def test_key_lookup_failure_is_not_misreported_as_absence(self):
        self.write_secret_tool(
            "case ${1:-} in\n"
            "  lookup) printf 'keyring unavailable\\n' >&2; exit 1 ;;\n"
            "  clear) exit 0 ;;\n"
            "esac\n"
        )
        result = subprocess.run(
            [SETUP, "key-remove", "openai"], env=self.env,
            text=True, capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "keyring_failed")

    def test_cleanup_does_not_claim_absent_keys_were_removed(self):
        self.write_secret_tool(
            "case ${1:-} in\n"
            "  lookup) exit 1 ;;\n"
            "  clear) exit 0 ;;\n"
            "esac\n"
        )
        result = subprocess.run(
            [SETUP, "cleanup", "--yes"], env=self.env,
            text=True, capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(any("API key" in item for item in payload["removed"]))


class ProviderHealthTests(unittest.TestCase):
    """A provider that is installed but cannot speak must never read "ready".

    This is the regression that silently disabled every keybinding: kokoro
    passed its probe, was reported ready, was selected as default, and could
    not synthesise a single sample.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_RUNTIME_DIR": str(self.home / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.providers = self.home / "config" / "omarchy-tts" / "providers"
        self.providers.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_provider(self, name, body, probe="true"):
        path = self.providers / name
        path.write_text("#!/usr/bin/env bash\n"
                        f"# desc: test provider {name}\n"
                        "# kind: local\n"
                        f"# probe: {probe}\n"
                        f"{body}\n")
        path.chmod(0o755)
        return path

    def speak(self, *args):
        return subprocess.run([SPEAK, *args], text=True, capture_output=True,
                              env=self.env, check=False)

    def status_of(self, name):
        info = json.loads(self.speak("--info").stdout)
        for entry in info["providers"]:
            if entry["name"] == name:
                return entry["status"]
        return None

    def test_installed_but_broken_provider_is_not_ready(self):
        self.write_provider("brokentest", "exit 3")
        self.assertEqual(self.status_of("brokentest"), "untested",
                         "an unproven provider must not claim to be ready")
        result = self.speak("--verify", "brokentest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status_of("brokentest"), "failing")

    def test_working_provider_becomes_ready_only_after_proof(self):
        self.write_provider("worktest", "cat > /dev/null")
        self.assertEqual(self.status_of("worktest"), "untested")
        result = self.speak("--verify", "worktest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status_of("worktest"), "ready")

    def test_health_proof_expires_when_the_adapter_changes(self):
        provider = self.write_provider("changing", "cat > /dev/null")
        self.assertEqual(self.speak("--verify", "changing").returncode, 0)
        self.assertEqual(self.status_of("changing"), "ready")
        provider.write_text(provider.read_text() + "# release changed\n")
        self.assertEqual(self.status_of("changing"), "untested")

    def test_proof_cannot_bless_config_changed_while_provider_runs(self):
        self.write_provider(
            "movingconfig",
            "cat >/dev/null\n"
            "tmp=${TTS_CONFIG}.provider-test\n"
            "jq '.movingconfig = {changed: true}' \"$TTS_CONFIG\" > \"$tmp\"\n"
            "mv \"$tmp\" \"$TTS_CONFIG\"",
        )
        result = self.speak("--verify", "movingconfig")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status_of("movingconfig"), "untested")

    def test_legacy_health_without_a_fingerprint_is_untrusted(self):
        self.write_provider("legacy", "cat > /dev/null")
        health = self.home / "cache" / "omarchy-tts" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text(json.dumps({"legacy": {"status": "ok"}}))
        self.assertEqual(self.status_of("legacy"), "untested")

    def test_provider_probe_has_a_deadline(self):
        self.write_provider("hangingprobe", "cat > /dev/null", probe="sleep 30")
        started = time.monotonic()
        self.assertEqual(self.status_of("hangingprobe"), "missing")
        self.assertLess(time.monotonic() - started, 5)

    def test_health_writer_recovers_a_truncated_cache_atomically(self):
        health = self.home / "cache" / "omarchy-tts" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text('{"worktest":')
        self.write_provider("worktest", "cat > /dev/null")
        result = self.speak("--verify", "worktest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(health.read_text())["worktest"]["status"], "ok")
        self.assertEqual(health.stat().st_mode & 0o777, 0o600)

    def test_provider_setting_change_invalidates_cached_proof(self):
        health = self.home / "cache" / "omarchy-tts" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text(json.dumps({
            "piper": {"status": "ok", "fingerprint": "old"},
            "unrelated": {"status": "ok", "fingerprint": "keep"},
        }))
        result = self.speak("--set", ".piper.voice", "en_US-test-medium")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(health.read_text())
        self.assertNotIn("piper", updated)
        self.assertIn("unrelated", updated)

    def test_absent_provider_reports_missing_not_failing(self):
        # Absence and breakage are different problems with different fixes.
        self.write_provider("absenttest", "cat > /dev/null", probe="false")
        self.assertEqual(self.status_of("absenttest"), "missing")
        result = self.speak("--verify", "absenttest")
        self.assertIn("not installed", result.stdout)

    def test_failure_during_real_speech_is_recorded(self):
        self.write_provider("livetest", "exit 4")
        self.speak("--set", ".provider", "livetest")
        self.speak("Hello there.")
        self.assertEqual(self.status_of("livetest"), "failing",
                         "a real failure should mark the provider without a separate test")

    def test_temporary_provider_limit_does_not_poison_health(self):
        for name, code in (("quotatest", 69), ("networktest", 74), ("ratetest", 75)):
            self.write_provider(name, f"exit {code}")
            self.speak("--set", ".provider", name)
            self.speak("Hello there.")
            self.assertEqual(self.status_of(name), "untested")

    def test_superseded_speech_cannot_erase_the_new_owner(self):
        self.write_provider(
            "slowtest",
            "trap 'exit 143' TERM INT\ncat > /dev/null\nsleep 30",
        )
        self.speak("--set", ".provider", "slowtest")
        first = subprocess.Popen([SPEAK, "first"], text=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=self.env)
        pid_file = self.home / "run" / "omarchy-tts" / "pgid"
        for _ in range(100):
            if pid_file.exists():
                break
            time.sleep(0.02)
        status_file = self.home / "run" / "omarchy-tts" / "status"
        self.assertEqual(pid_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(status_file.stat().st_mode & 0o777, 0o600)
        second = subprocess.Popen([SPEAK, "second"], text=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=self.env)
        for _ in range(100):
            if pid_file.exists() and first.poll() is not None:
                break
            time.sleep(0.02)
        self.assertEqual(self.speak("--status").stdout.strip(), "speaking")
        self.assertTrue(pid_file.exists())
        self.speak("--stop")
        first.wait(timeout=3)
        second.wait(timeout=3)

    def test_registered_playback_owner_is_the_provider_session(self):
        marker = self.home / "provider-pid"
        self.write_provider(
            "ownedgroup",
            'printf "%s\\n" "$$" > "$PROVIDER_PID_MARKER"\n'
            "cat > /dev/null\n"
            "sleep 30",
        )
        env = {**self.env, "PROVIDER_PID_MARKER": str(marker)}
        speaking = subprocess.Popen(
            [SPEAK, "--provider", "ownedgroup", "hello"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pid_file = self.home / "run" / "omarchy-tts" / "pgid"
        try:
            for _ in range(200):
                if marker.exists() and pid_file.exists():
                    break
                time.sleep(0.01)
            provider_pid = int(marker.read_text().strip())
            registered_pid = int(pid_file.read_text().split()[0])
            self.assertEqual(registered_pid, provider_pid)
            self.assertEqual(os.getpgid(provider_pid), provider_pid)
        finally:
            self.speak("--stop")
            speaking.wait(timeout=3)

    def test_stale_speech_identity_cannot_signal_a_reused_pid(self):
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            state = self.home / "run" / "omarchy-tts"
            state.mkdir(parents=True, exist_ok=True)
            (state / "pgid").write_text(f"{sleeper.pid} definitely-wrong\n")
            (state / "status").write_text("speaking\n")
            self.assertEqual(self.speak("--status").stdout.strip(), "idle")
            self.speak("--stop")
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=3)

    def test_speech_control_never_follows_runtime_state_symlinks(self):
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            state = self.home / "run" / "omarchy-tts"
            state.mkdir(parents=True, exist_ok=True)
            identity = Path(f"/proc/{sleeper.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            external_owner = self.home / "external-owner"
            external_owner.write_text(f"{sleeper.pid} {identity}\n")
            owner = state / "pgid"
            owner.symlink_to(external_owner)
            self.speak("--stop")
            self.assertIsNone(sleeper.poll())

            owner.write_text(f"{sleeper.pid} {identity}\n")
            external_status = self.home / "external-status"
            external_status.write_text("private text that must not be displayed\n")
            status_path = state / "status"
            status_path.unlink(missing_ok=True)
            status_path.symlink_to(external_status)
            status = self.speak("--status").stdout.strip()
            self.assertEqual(status, "preparing")
            self.assertNotIn("private text", status)
        finally:
            if sleeper.poll() is None:
                sleeper.terminate()
            sleeper.wait(timeout=3)


class CloudProviderPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.args = self.root / "curl-args"
        self.body = self.root / "curl-body"
        self.headers = self.root / "curl-headers"
        self.response_headers = self.root / "response-headers"
        self.response_headers.write_text(
            "HTTP/2 200\r\nx-request-id: req_private_test\r\n"
            "x-ratelimit-limit-requests: 100\r\n"
            "x-ratelimit-remaining-requests: 99\r\n"
            "x-ratelimit-reset-requests: 1s\r\nretry-after: 2s\r\n"
            "character-cost: 7\r\n\r\n"
        )
        fake_curl = self.bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_CURL_ARGS\"\n"
            "[[ ${FAKE_CURL_EXIT:-0} == 0 ]] || exit \"$FAKE_CURL_EXIT\"\n"
            "output=\n"
            "dump=\n"
            "writeout=\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ $1 == --output ]]; then output=$2; shift; fi\n"
            "  if [[ $1 == --dump-header ]]; then dump=$2; shift; fi\n"
            "  if [[ $1 == --write-out ]]; then writeout=$2; shift; fi\n"
            "  shift\n"
            "done\n"
            "cat > \"$FAKE_CURL_BODY\"\n"
            "cat <&3 > \"$FAKE_CURL_HEADERS\"\n"
            "[[ -z $dump ]] || cp \"$FAKE_RESPONSE_HEADERS\" \"$dump\"\n"
            "if [[ -n ${FAKE_OUTPUT_FILE:-} ]]; then cp \"$FAKE_OUTPUT_FILE\" \"$output\"\n"
            "elif [[ ${FAKE_EMPTY_AUDIO:-0} != 1 ]]; then printf ID3fake-audio > \"$output\"; fi\n"
            "[[ -z $writeout ]] || printf '%s' \"${FAKE_HTTP_STATUS:-200}\"\n"
        )
        fake_curl.chmod(0o755)
        self.config = self.root / "config.json"
        self.config.write_text("{}")

    def tearDown(self):
        self.temp.cleanup()

    def test_cloud_secrets_and_text_stay_out_of_curl_arguments(self):
        secret = "secret-key-that-must-not-leak"
        spoken = "private highlighted text that must not leak"
        canned = self.root / "gemini-response.json"
        canned.write_text(json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000",
            "data": base64.b64encode(b"\x00\x01" * 480).decode()}}]}}]}))
        wav = self.root / "google-response.json"
        wav.write_text(json.dumps({"audioContent": base64.b64encode(
            b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32).decode()}))
        for provider, variable in (("openai", "OPENAI_API_KEY"),
                                   ("elevenlabs", "ELEVENLABS_API_KEY"),
                                   ("gemini", "GEMINI_API_KEY"),
                                   ("google", "GOOGLE_API_KEY")):
            env = {**os.environ,
                   "PATH": f"{self.bin}:{os.environ['PATH']}",
                   "FAKE_CURL_ARGS": str(self.args),
                   "FAKE_CURL_BODY": str(self.body),
                   "FAKE_CURL_HEADERS": str(self.headers),
                   "FAKE_RESPONSE_HEADERS": str(self.response_headers),
                   "XDG_RUNTIME_DIR": str(self.root),
                   "TTS_PLUGIN_DIR": str(ROOT),
                   "TTS_CONFIG": str(self.config),
                   "TTS_SILENT": "1",
                   "TTS_VOICE": {"gemini": "Kore", "google": "en-US-Chirp3-HD-Aoede"}.get(provider, "test-voice"),
                   variable: secret}
            if provider == "gemini":
                env["FAKE_OUTPUT_FILE"] = str(canned)
            if provider == "google":
                env["FAKE_OUTPUT_FILE"] = str(wav)
            result = subprocess.run([ROOT / "providers" / provider], input=spoken,
                                    text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = self.args.read_text()
            self.assertNotIn(secret, arguments)
            self.assertNotIn(spoken, arguments)
            self.assertNotIn("key=", arguments)
            self.assertIn("--proto", arguments)
            self.assertIn("--max-filesize", arguments)
            payload = json.loads(self.body.read_text())
            if provider == "gemini":
                self.assertEqual(payload["contents"][0]["parts"][0]["text"], spoken)
                self.assertIn("aiplatform.googleapis.com", arguments,
                              "the default host takes a Vertex AI key")
            elif provider == "google":
                self.assertEqual(payload["input"]["text"], spoken)
                self.assertEqual(payload["voice"]["languageCode"], "en-US")
            else:
                self.assertEqual(payload["input" if provider == "openai" else "text"], spoken)
            self.assertIn(secret, self.headers.read_text())

    def test_cloud_ocr_sends_the_image_in_the_body_and_nothing_in_argv(self):
        for engine, variable in (("openai", "OPENAI_API_KEY"), ("google", "GOOGLE_API_KEY")):
            with self.subTest(engine=engine):
                self.cloud_ocr_check(engine, variable)

    def test_google_embedded_error_is_normalized_and_recorded_as_failure(self):
        response = self.root / "vision-error.json"
        remote_detail = "credential details that must not be echoed"
        response.write_text(json.dumps({"responses": [{"error": {
            "code": 401, "status": "UNAUTHENTICATED", "message": remote_detail,
        }}]}))
        metrics = self.root / "vision-error-metrics.json"
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(response),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "GOOGLE_API_KEY": "safe-test-key",
               "TTS_METRICS_FILE": str(metrics)}
        result = subprocess.run(
            [ROOT / "ocr" / "google"], input=(ROOT / "lib" / "ocr-probe.png").read_bytes(),
            capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertIn(b"API key was rejected", result.stderr)
        self.assertNotIn(remote_detail.encode(), result.stderr)
        telemetry = json.loads(metrics.read_text())
        self.assertEqual(telemetry["lastRequest"]["httpStatus"], "200")
        self.assertEqual(telemetry["lastRequest"]["outcome"], "error")
        self.assertEqual(telemetry["lastRequest"]["errorCode"], "auth")
        self.assertNotIn(remote_detail, metrics.read_text())

    def test_google_empty_response_envelope_is_not_recorded_as_success(self):
        response = self.root / "vision-empty.json"
        response.write_text('{"responses": []}\n')
        metrics = self.root / "vision-empty-metrics.json"
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(response),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "GOOGLE_API_KEY": "safe-test-key",
               "TTS_METRICS_FILE": str(metrics)}
        result = subprocess.run(
            [ROOT / "ocr" / "google"], input=(ROOT / "lib" / "ocr-probe.png").read_bytes(),
            capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 74, result.stderr)
        self.assertEqual(json.loads(metrics.read_text())["lastRequest"]["errorCode"],
                         "invalid_response")

    def test_openai_ocr_refusal_and_truncation_are_not_returned_as_text(self):
        metrics = self.root / "openai-ocr-terminal.json"
        base_env = {**os.environ,
                    "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
                    "FAKE_CURL_HEADERS": str(self.headers),
                    "FAKE_RESPONSE_HEADERS": str(self.response_headers),
                    "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
                    "TTS_CONFIG": str(self.config), "OPENAI_API_KEY": "safe-test-key",
                    "TTS_METRICS_FILE": str(metrics)}
        image = (ROOT / "lib" / "ocr-probe.png").read_bytes()
        for payload, message, code in (
            ({"choices": [{"finish_reason": "stop", "message": {
                "content": "must not escape", "refusal": "blocked"}}]},
             "refused", "refused"),
            ({"choices": [{"finish_reason": "length", "message": {
                "content": "partial must not escape"}}]},
             "truncated", "truncated"),
        ):
            with self.subTest(code=code):
                response = self.root / f"openai-{code}.json"
                response.write_text(json.dumps(payload))
                result = subprocess.run(
                    [ROOT / "ocr" / "openai"], input=image, capture_output=True,
                    env={**base_env, "FAKE_OUTPUT_FILE": str(response)},
                )
                self.assertEqual(result.returncode, 74, result.stderr)
                self.assertIn(message.encode(), result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(json.loads(metrics.read_text())["lastRequest"]["errorCode"],
                                 code)

    def test_openai_ocr_uses_the_declared_safe_clipboard_mime(self):
        response = self.root / "openai-jpeg-ocr.json"
        response.write_text(json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": "text"},
        }]}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(response),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "OPENAI_API_KEY": "safe-test-key",
               "TTS_OCR_IMAGE_MIME": "image/jpeg"}
        result = subprocess.run([ROOT / "ocr" / "openai"], input=b"jpeg-bytes",
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        image_url = self.body.read_text()
        self.assertIn("data:image/jpeg;base64,", image_url)

    def cloud_ocr_check(self, engine, variable):
        secret = "secret-key-that-must-not-leak"
        image = (ROOT / "lib" / "ocr-probe.png").read_bytes()
        self.body.write_text("")
        self.args.write_text("")
        response = self.root / f"{engine}-ocr-response.json"
        if engine == "openai":
            response.write_text(json.dumps({"choices": [{
                "finish_reason": "stop", "message": {"content": "Probe 123"},
            }]}))
        else:
            response.write_text(json.dumps({"responses": [{}]}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), variable: secret,
               "FAKE_OUTPUT_FILE": str(response),
               "TTS_METRICS_FILE": str(self.root / "ocr-metrics.json")}
        result = subprocess.run([ROOT / "ocr" / engine], input=image,
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.args.read_text()
        self.assertNotIn(secret, arguments)
        self.assertNotIn("key=", arguments, "the key must not ride in the URL")
        encoded = base64.b64encode(image).decode()[:64]
        self.assertNotIn(encoded, arguments, "the screenshot must not travel through argv")
        payload = json.loads(self.body.read_text())
        if engine == "openai":
            parts = payload["messages"][0]["content"]
            image_url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
            self.assertTrue(image_url.startswith("data:image/png;base64," + encoded))
        else:
            self.assertTrue(payload["requests"][0]["image"]["content"].startswith(encoded))
        self.assertIn(secret, self.headers.read_text())
        metrics = json.loads((self.root / "ocr-metrics.json").read_text())
        self.assertEqual(metrics["provider"], engine + "-ocr")
        self.assertNotIn("base64", (self.root / "ocr-metrics.json").read_text())

    def test_gemini_host_follows_the_configured_api_and_is_never_guessed(self):
        canned = self.root / "gemini-response.json"
        canned.write_text(json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000",
            "data": base64.b64encode(b"\x00\x01" * 480).decode()}}]}}]}))
        self.config.write_text(json.dumps({"gemini": {"api": "developer"}}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(canned),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1", "TTS_VOICE": "Kore",
               "GEMINI_API_KEY": "secret-key-that-must-not-leak"}
        result = subprocess.run([ROOT / "providers" / "gemini"], input="Probe.",
                                text=True, capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("generativelanguage.googleapis.com", self.args.read_text())
        self.assertEqual(self.args.read_text().count("googleapis.com"), 1,
                         "one configured host, one request")

    def test_gemini_selects_audio_even_when_metadata_is_the_first_part(self):
        canned = self.root / "gemini-multipart-response.json"
        canned.write_text(json.dumps({"candidates": [{"content": {"parts": [
            {"text": "internal metadata"},
            {"inlineData": {
                "mimeType": "audio/L16;codec=pcm;rate=24000",
                "data": base64.b64encode(b"\x00\x01" * 64).decode(),
            }},
        ]}}]}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(canned),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
               "TTS_VOICE": "Kore", "GEMINI_API_KEY": "safe-test-key"}
        result = subprocess.run([ROOT / "providers" / "gemini"], input="Probe.",
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_gemini_api_selection_is_rejected_before_network(self):
        self.config.write_text(json.dumps({"gemini": {"api": "typo"}}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
               "TTS_VOICE": "Kore", "GEMINI_API_KEY": "safe-test-key"}
        result = subprocess.run(
            [ROOT / "providers" / "gemini"], input="Probe.",
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("unsupported API selection", result.stderr)
        self.assertIn("vertex or developer", result.stderr)
        self.assertFalse(self.args.exists(), "invalid config reached curl")

    def test_google_voice_list_carries_what_a_browser_filters_on(self):
        listing = self.root / "voices.json"
        listing.write_text(json.dumps({"voices": [
            {"name": "en-US-Chirp3-HD-Aoede", "languageCodes": ["en-US"], "ssmlGender": "FEMALE"},
            {"name": "de-DE-Neural2-B", "languageCodes": ["de-DE"], "ssmlGender": "MALE"}]}))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_OUTPUT_FILE": str(listing),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "GOOGLE_API_KEY": "secret-key-that-must-not-leak"}
        result = subprocess.run([ROOT / "providers" / "google", "--voices"],
                                text=True, capture_output=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        voices = json.loads(result.stdout)
        self.assertEqual(voices[0], {"value": "en-US-Chirp3-HD-Aoede", "language": "en-US",
                                     "gender": "female", "family": "Chirp3-HD",
                                     "label": "Chirp3-HD-Aoede · female · en-US"})
        self.assertEqual(voices[1]["family"], "Neural2")
        self.assertNotIn("secret-key", self.args.read_text())

    def test_cloud_telemetry_contains_counts_and_limits_but_no_content(self):
        metrics = self.root / "metrics.json"
        metrics.write_text("not json")
        spoken = "private words"
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
               "TTS_INPUT_CHARS": str(len(spoken)), "TTS_METRICS_FILE": str(metrics),
               "OPENAI_API_KEY": "secret-key-that-must-not-leak"}
        result = subprocess.run([ROOT / "providers" / "openai"], input=spoken,
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(metrics.read_text())
        self.assertEqual(data["localObserved"],
                         {"requests": 1, "characters": len(spoken), "billedUnits": 0})
        self.assertEqual(data["rateLimits"]["requests"]["remaining"], "99")
        self.assertNotIn(spoken, metrics.read_text())
        self.assertEqual(metrics.stat().st_mode & 0o777, 0o600)

    def test_existing_metrics_permissions_are_repaired(self):
        metrics = self.root / "existing-metrics.json"
        metrics.write_text("{}\n")
        metrics.chmod(0o644)
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
               "TTS_METRICS_FILE": str(metrics), "OPENAI_API_KEY": "safe-test-key"}
        result = subprocess.run([ROOT / "providers" / "openai"], input="hello",
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(metrics.stat().st_mode & 0o777, 0o600)

    def test_remote_header_values_are_bounded_before_persistence(self):
        self.response_headers.write_text(
            "HTTP/2 200\r\nx-request-id: " + "x" * 10 + "\x1b" + "x" * 5000
            + "\r\n\r\n"
        )
        metrics = self.root / "bounded-headers.json"
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
               "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
               "TTS_METRICS_FILE": str(metrics), "OPENAI_API_KEY": "safe-test-key"}
        result = subprocess.run([ROOT / "providers" / "openai"], input="hello",
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        request_id = json.loads(metrics.read_text())["lastRequest"]["requestId"]
        self.assertEqual(len(request_id), 512)
        self.assertNotIn("\x1b", request_id)

    def test_provider_specific_rates_are_sent_unchanged_within_bounds(self):
        wav = self.root / "rate-google.json"
        wav.write_text(json.dumps({"audioContent": base64.b64encode(
            b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32).decode()}))
        cases = (
            ("openai", "OPENAI_API_KEY", "4", "speed", 4, None),
            ("elevenlabs", "ELEVENLABS_API_KEY", "1.2", "voice_settings", 1.2, None),
            ("google", "GOOGLE_API_KEY", "2", "audioConfig", 2, wav),
        )
        for provider, variable, rate, section, expected, response in cases:
            with self.subTest(provider=provider):
                env = {**os.environ,
                       "PATH": f"{self.bin}:{os.environ['PATH']}",
                       "FAKE_CURL_ARGS": str(self.args),
                       "FAKE_CURL_BODY": str(self.body),
                       "FAKE_CURL_HEADERS": str(self.headers),
                       "FAKE_RESPONSE_HEADERS": str(self.response_headers),
                       "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
                       "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
                       "TTS_RATE": rate, "TTS_VOICE": "en-US-Test-A",
                       variable: "safe-test-key"}
                if response is not None:
                    env["FAKE_OUTPUT_FILE"] = str(response)
                else:
                    env.pop("FAKE_OUTPUT_FILE", None)
                result = subprocess.run([ROOT / "providers" / provider], input="hello",
                                        text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(self.body.read_text())
                if provider == "openai":
                    actual = payload[section]
                elif provider == "elevenlabs":
                    actual = payload[section]["speed"]
                else:
                    actual = payload[section]["speakingRate"]
                self.assertEqual(actual, expected)

    def test_network_failure_is_reported_as_temporary(self):
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_CURL_EXIT": "28", "XDG_RUNTIME_DIR": str(self.root),
               "TTS_PLUGIN_DIR": str(ROOT), "TTS_CONFIG": str(self.config),
               "TTS_SILENT": "1", "OPENAI_API_KEY": "safe-test-key"}
        result = subprocess.run([ROOT / "providers" / "openai"], input="hello",
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 74)
        self.assertIn("network request failed", result.stderr)

    def test_paid_provider_limits_are_normalized_and_actionable(self):
        metrics = self.root / "limits.json"
        base = {**os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
                "FAKE_CURL_HEADERS": str(self.headers),
                "FAKE_RESPONSE_HEADERS": str(self.response_headers),
                "XDG_RUNTIME_DIR": str(self.root), "TTS_PLUGIN_DIR": str(ROOT),
                "TTS_CONFIG": str(self.config), "TTS_SILENT": "1",
                "TTS_METRICS_FILE": str(metrics),
                "OPENAI_API_KEY": "safe-test-key"}
        for status, returncode, error_code, message in (
            ("429", 75, "rate_limit", "rate or concurrency limit"),
            ("402", 69, "quota", "billing limit"),
            ("401", 77, "auth", "API key was rejected"),
            ("403", 77, "forbidden", "access was forbidden"),
        ):
            result = subprocess.run(
                [ROOT / "providers" / "openai"], input="hello", text=True,
                capture_output=True, env={**base, "FAKE_HTTP_STATUS": status},
            )
            self.assertEqual(result.returncode, returncode, result.stderr)
            self.assertIn(message, result.stderr)
            telemetry = json.loads(metrics.read_text())
            self.assertEqual(telemetry["lastRequest"]["errorCode"], error_code)
            self.assertEqual(telemetry["lastRequest"]["retryAfter"], "2s")

    def test_empty_success_response_is_rejected(self):
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_CURL_ARGS": str(self.args), "FAKE_CURL_BODY": str(self.body),
               "FAKE_CURL_HEADERS": str(self.headers),
               "FAKE_RESPONSE_HEADERS": str(self.response_headers),
               "FAKE_EMPTY_AUDIO": "1", "XDG_RUNTIME_DIR": str(self.root),
               "TTS_PLUGIN_DIR": str(ROOT), "TTS_CONFIG": str(self.config),
               "TTS_SILENT": "1", "OPENAI_API_KEY": "safe-test-key"}
        result = subprocess.run([ROOT / "providers" / "openai"], input="hello",
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 74)
        self.assertIn("returned invalid audio", result.stderr)


class BindingAdoptionTests(unittest.TestCase):
    """Shortcuts a user set up by hand are ours to adopt, not to duplicate."""

    LUA = """-- Personal keybindings
o.bind("SUPER + SHIFT + S", "Screenshot", "omarchy-capture-screenshot")
o.bind("SUPER + ALT + P", "Unrelated", "/usr/bin/speaker-test --nonsense")
o.bind("SUPER + ALT + E", "Speak selection", "/home/someone/.local/bin/speak --toggle")
o.bind("SUPER + ALT + X", "Stop speaking", "speak --stop")
"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "hypr").mkdir(parents=True)
        (self.home / "omarchy-tts").mkdir(parents=True)
        self.lua = self.home / "hypr" / "bindings.lua"
        self.lua.write_text(self.LUA)
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(self.home)}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_bindings(self, *args):
        return subprocess.run([BINDINGS, *args], text=True, capture_output=True,
                              env=self.env, check=False)

    def test_hand_written_bindings_are_adoptable_not_conflicts(self):
        state = json.loads(self.run_bindings("status").stdout)
        self.assertFalse(state["installed"])
        self.assertEqual(state["conflicts"], [])
        self.assertIn("SUPER + ALT + E", state["adoptable"])
        self.assertTrue(state["canInstall"], "adoption must not be a dead end")

    def test_install_adopts_without_duplicating(self):
        self.run_bindings("install")
        text = self.lua.read_text()
        self.assertEqual(text.count("SUPER + ALT + E"), 1, "chord bound twice")
        self.assertIn("Screenshot", text, "unrelated binding was removed")

    def test_similarly_named_program_is_left_alone(self):
        # speaker-test is not speak; adopting it would break the user's setup.
        self.run_bindings("install")
        self.assertIn("speaker-test", self.lua.read_text())

    def test_genuine_conflict_still_refuses_and_changes_nothing(self):
        self.lua.write_text('o.bind("SUPER + ALT + E", "Open editor", "code")\n')
        before = self.lua.read_text()
        state = json.loads(self.run_bindings("status").stdout)
        self.assertEqual(state["conflicts"], ["SUPER + ALT + E"])
        self.assertFalse(state["canInstall"])
        result = self.run_bindings("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.lua.read_text(), before)


class CancelledSpeechTests(unittest.TestCase):
    """Stopping speech must never be recorded as a broken provider.

    A provider that execs a player does not necessarily die by the signal sent
    to it. mpv catches SIGTERM and exits 4, so stopping cloud speech marked the
    backend failing, the panel then offered only "Replace key", and the
    provider could not be selected again.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_RUNTIME_DIR": str(self.home / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        (self.home / "run").mkdir(parents=True)
        self.providers = self.home / "config" / "omarchy-tts" / "providers"
        self.providers.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_provider(self, name, body):
        path = self.providers / name
        path.write_text("#!/usr/bin/env bash\n"
                        f"# desc: test provider {name}\n"
                        "# kind: local\n"
                        "# probe: true\n"
                        f"{body}\n")
        path.chmod(0o755)

    def status_of(self, name):
        info = json.loads(subprocess.run([SPEAK, "--info"], text=True,
                                         capture_output=True, env=self.env).stdout)
        return next(p["status"] for p in info["providers"] if p["name"] == name)

    def test_stopping_a_signal_translating_provider_is_not_a_failure(self):
        # Exits 4 on SIGTERM instead of dying by signal, exactly as mpv does.
        self.write_provider("mpvlike",
                            'trap "exit 4" TERM\ncat > /dev/null\nsleep 30 &\nwait $!')
        speaking = subprocess.Popen([SPEAK, "--provider", "mpvlike"],
                                    stdin=subprocess.PIPE, text=True, env=self.env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        speaking.stdin.write("Interrupt me.\n")
        speaking.stdin.close()
        time.sleep(1.5)
        subprocess.run([SPEAK, "--stop"], env=self.env, capture_output=True)
        speaking.wait(timeout=30)
        self.assertNotEqual(self.status_of("mpvlike"), "failing",
                            "a stopped provider was recorded as broken")

    def test_a_genuine_failure_is_still_recorded(self):
        self.write_provider("brokenlike", "cat > /dev/null; exit 4")
        subprocess.run([SPEAK, "--provider", "brokenlike"], input="Hello.",
                       text=True, env=self.env, capture_output=True)
        self.assertEqual(self.status_of("brokenlike"), "failing",
                         "an uncancelled failure must still mark the provider")


class LargeVoiceListTests(unittest.TestCase):
    """A cloud account can list thousands of voices; --info must still work.

    Passing the list as one jq argument failed with "Argument list too long"
    only for the accounts with the most voices - the ones that most needed
    the browser.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_RUNTIME_DIR": str(self.home / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        (self.home / "run").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_info_survives_thousands_of_cached_voices(self):
        cache = self.home / "cache" / "omarchy-tts" / "voices"
        cache.mkdir(parents=True)
        voices = [{"value": f"en-US-Chirp3-HD-Voice{i}", "language": "en-US", "gender": "female",
                   "family": "Chirp3-HD", "label": f"Chirp3-HD-Voice{i} · female · en-US"}
                  for i in range(3000)]
        (cache / "google.json").write_text(json.dumps(voices))
        subprocess.run([SPEAK, "--set", ".provider", "google"], env=self.env, capture_output=True)
        result = subprocess.run([SPEAK, "--info"], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(len(info["voices"]), 3000)
        self.assertEqual(info["voices"][0]["family"], "Chirp3-HD")


class OcrEngineTests(unittest.TestCase):
    """Engines are chosen the way voices are: listed, proven, then selected.

    Health for an engine lives under its own key so a reading engine and a
    speaking provider that share a name (openai) never overwrite each other.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_RUNTIME_DIR": str(self.home / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        (self.home / "run").mkdir(parents=True)
        self.engines = self.home / "config" / "omarchy-tts" / "ocr"
        self.engines.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_engine(self, name, body, kind="local", probe="true"):
        path = self.engines / name
        path.write_text("#!/usr/bin/env bash\n"
                        f"# desc: test engine {name}\n"
                        f"# kind: {kind}\n"
                        f"# probe: {probe}\n"
                        "cat > /dev/null\n" + body)
        path.chmod(0o755)

    def speak(self, *args):
        return subprocess.run([SPEAK, *args], text=True, capture_output=True, env=self.env)

    def engine(self, name):
        info = json.loads(self.speak("--info").stdout)
        return next(e for e in info["ocr"]["engines"] if e["name"] == name)

    def test_engine_is_untested_until_it_reads_the_probe(self):
        self.write_engine("reads", "printf 'Probe 123\\n'\n")
        self.write_engine("blind", "exit 1\n")
        self.write_engine("absent", "exit 0\n", probe="false")
        self.assertEqual(self.engine("reads")["status"], "untested")
        self.assertEqual(self.engine("absent")["status"], "missing")
        self.assertEqual(self.speak("--verify-ocr", "reads").returncode, 0)
        self.assertEqual(self.engine("reads")["status"], "ready")
        self.assertNotEqual(self.speak("--verify-ocr", "blind").returncode, 0)
        self.assertEqual(self.engine("blind")["status"], "failing")
        health = json.loads((self.home / "cache" / "omarchy-tts" / "health.json").read_text())
        self.assertIn("ocr:reads", health)
        self.assertNotIn("reads", health, "engine health must not be filed as a voice provider")

    def test_cloud_engine_without_a_key_is_nokey(self):
        self.write_engine("vision", "exit 0\n", kind="cloud", probe="false")
        self.assertEqual(self.engine("vision")["status"], "nokey")

    def test_languages_are_kept_per_engine(self):
        self.write_engine("one", "exit 0\n")
        self.speak("--set", ".ocr.tesseract.langs", "eng+jpn")
        self.speak("--set", ".ocr.easyocr.langs", "eng+deu")
        self.speak("--set", ".ocr.engine", "tesseract")
        self.assertEqual(json.loads(self.speak("--info").stdout)["ocr"]["langs"], "eng+jpn")
        self.speak("--set", ".ocr.engine", "easyocr")
        self.assertEqual(json.loads(self.speak("--info").stdout)["ocr"]["langs"], "eng+deu")
        self.assertNotEqual(self.speak("--set", ".ocr.easyocr.langs", "eng;rm").returncode, 0)
        self.assertEqual(json.loads(self.speak("--info").stdout)["ocr"]["langs"], "eng+deu")

    def test_custom_engine_languages_are_persisted_without_jq_injection(self):
        self.write_engine("custom-engine", "printf 'text\\n'\n")
        self.assertEqual(
            self.speak("--set", ".ocr.custom-engine.langs", "eng+jpn").returncode, 0
        )
        self.speak("--set", ".ocr.engine", "custom-engine")
        info = json.loads(self.speak("--info").stdout)
        self.assertEqual(info["ocr"]["langs"], "eng+jpn")
        self.assertNotEqual(
            self.speak("--set", ".ocr.bad] | .provider.langs", "eng").returncode, 0
        )

    def test_legacy_shared_languages_migrate_to_tesseract_only(self):
        config = self.home / "config" / "omarchy-tts" / "config.json"
        config.write_text('{"schemaVersion": 2, "ocr": {"engine": "tesseract", "langs": "eng+jpn+deu"}}')
        self.speak("--info")
        data = json.loads(config.read_text())
        self.assertEqual(data["ocr"]["tesseract"]["langs"], "eng+jpn+deu")
        self.assertEqual(data["ocr"]["easyocr"]["langs"], "eng",
                         "a language set tesseract accepts is not one EasyOCR can combine")


class OcrLanguageTests(unittest.TestCase):
    """Recognition languages are discovered, not typed into a free-text box.

    A configured language whose data was missing produced an empty result and
    exit 0: reading nothing, forever, without ever saying why.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_RUNTIME_DIR": str(self.home / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        (self.home / "run").mkdir(parents=True)
        self.engines = self.home / "config" / "omarchy-tts" / "ocr"
        self.engines.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_engine(self, name, languages_json):
        path = self.engines / name
        path.write_text("#!/usr/bin/env bash\n"
                        f"# desc: test engine {name}\n"
                        "# kind: local\n"
                        "# probe: true\n"
                        'if [[ "${1:-}" == "--languages" ]]; then\n'
                        f"  printf '%s' '{languages_json}'\n"
                        "  exit 0\n"
                        "fi\n"
                        "cat > /dev/null\n")
        path.chmod(0o755)

    def speak(self, *args):
        return subprocess.run([SPEAK, *args], text=True, capture_output=True, env=self.env)

    def test_languages_are_absent_until_explicitly_refreshed(self):
        self.write_engine("faketess", '[{"value":"eng","label":"eng","installed":true}]')
        self.speak("--set", ".ocr.engine", "faketess")
        info = json.loads(self.speak("--info").stdout)
        self.assertEqual(info["ocr"]["languages"], [],
                         "opening settings should not shell out to a package database")
        result = self.speak("--refresh-languages", "faketess")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(self.speak("--info").stdout)
        self.assertEqual([l["value"] for l in info["ocr"]["languages"]], ["eng"])
        self.assertTrue(info["ocr"]["languages"][0]["installed"])

    def test_available_and_installed_are_distinguished(self):
        self.write_engine("faketess",
                          '[{"value":"eng","label":"eng","installed":true},'
                          '{"value":"jpn","label":"jpn","installed":false}]')
        self.speak("--set", ".ocr.engine", "faketess")
        self.speak("--refresh-languages", "faketess")
        langs = json.loads(self.speak("--info").stdout)["ocr"]["languages"]
        installed = [l["value"] for l in langs if l["installed"]]
        available = [l["value"] for l in langs if not l["installed"]]
        self.assertEqual(installed, ["eng"])
        self.assertEqual(available, ["jpn"])

    def test_unknown_engine_is_refused(self):
        self.assertNotEqual(self.speak("--refresh-languages", "nope").returncode, 0)


class LanguageInstallTargetTests(unittest.TestCase):
    """QML may name a language; it may never name a command or a package."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {**os.environ,
                    "XDG_CONFIG_HOME": str(Path(self.temp.name) / "config"),
                    "XDG_RUNTIME_DIR": str(Path(self.temp.name) / "run")}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, target):
        result = subprocess.run([SETUP, "start", target], text=True,
                                capture_output=True, env=self.env)
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {"ok": False, "message": result.stdout + result.stderr}

    def test_malformed_language_codes_are_refused_by_shape(self):
        for target in ("lang:../../etc/passwd", "lang:jpn; rm -rf /", "lang:JPN",
                       "lang:", "lang:toolongcode"):
            with self.subTest(target=target):
                answer = self.start(target)
                self.assertFalse(answer.get("ok", False),
                                 f"{target} was accepted as an install target")
                self.assertNotIn("started", answer)


class LanguageNameTableTests(unittest.TestCase):
    """Language data files are identifiers; the panel must show names.

    The engine joins its codes against lib/tesseract-languages.tsv. The fake
    engines above never touch that table, so its shape is checked directly.
    """

    TABLE = Path(__file__).resolve().parents[1] / "lib" / "tesseract-languages.tsv"

    def rows(self):
        return [line.split("\t") for line in self.TABLE.read_text().splitlines() if line]

    def test_every_row_is_code_and_distinct_name(self):
        for row in self.rows():
            with self.subTest(row=row):
                self.assertEqual(len(row), 2)
                code, name = row
                self.assertRegex(code, r"^[a-z]{3,4}(_[a-z]+)*$")
                self.assertTrue(name.strip())
                self.assertNotEqual(code, name, "a code standing in for its own name")

    def test_script_variants_say_which_script(self):
        names = dict(self.rows())
        self.assertEqual(names["jpn"], "Japanese")
        self.assertIn("Cyrillic", names["aze_cyrl"])
        self.assertIn("Simplified", names["chi_sim"])
        self.assertIn("vertical", names["jpn_vert"])

    def test_no_duplicate_codes(self):
        codes = [r[0] for r in self.rows()]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
