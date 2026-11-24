import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
from unittest import TestCase, mock

from tornado.testing import AsyncHTTPTestCase

import settings


class SettingsSecurityTest(AsyncHTTPTestCase):
    passphrase = "synthetic test unlock passphrase"

    def setUp(self):
        config_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.config_path = Path(config_dir) / "settings.json"
        self.enterContext(mock.patch.object(settings.util, "get_app_root", return_value=config_dir))
        self.enterContext(mock.patch.object(settings, "launch_maxbot"))
        super().setUp()

    def get_app(self):
        self.access = settings.SettingsAccess(self.passphrase)
        return settings.create_settings_application(self.get_http_port(), self.access, asyncio.Event())

    def request(self, path, *, method="GET", data=None, authenticated=False, headers=None):
        request_headers = {
            "Origin": self.get_url(""),
            "Content-Type": "application/json",
        }
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})
        return self.fetch(
            path, method=method, headers=request_headers,
            body=json.dumps(data) if data is not None else b"" if method == "POST" else None,
        )

    def unlock(self):
        response = self.request("/unlock", method="POST", data={"passphrase": self.passphrase})
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.token = json.loads(response.body)["token"]

    def test_private_routes_require_authentication(self):
        for path in ("/load", "/profiles", "/status", "/instances", "/question"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).code, 401)
        for path in ("/save", "/run", "/shutdown", "/reset", "/pause", "/resume", "/stop"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, method="POST").code, 401)
        settings.launch_maxbot.assert_not_called()
        self.assertFalse(self.config_path.exists())

    def test_host_origin_and_safe_methods(self):
        self.assertEqual(self.request("/settings.html", headers={"Host": "other.invalid"}).code, 403)
        self.assertEqual(self.request("/unlock", method="POST", headers={"Origin": "null"}).code, 403)
        self.unlock()
        self.assertEqual(self.request("/load", authenticated=True, headers={"Host": "other.invalid"}).code, 403)
        self.assertEqual(
            self.request("/run", method="POST", authenticated=True, headers={"Origin": "https://other.invalid"}).code,
            403,
        )
        self.assertEqual(self.request("/run", authenticated=True).code, 405)
        settings.launch_maxbot.assert_not_called()
        self.assertEqual(self.request("/run", method="POST", authenticated=True).code, 200)
        settings.launch_maxbot.assert_called_once()

    def test_legacy_kham_credentials_are_neither_returned_nor_saved(self):
        legacy_accounts = {
            "kham_account": "synthetic-member",
            "kham_password": "synthetic-platform-password",
            "udn_account": "synthetic-other-platform-member",
        }
        self.config_path.write_text(json.dumps({"accounts": legacy_accounts}), encoding="utf-8")
        self.unlock()
        response = self.request("/load", authenticated=True)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        loaded = json.loads(response.body)
        self.assertNotIn("kham_account", loaded["accounts"])
        self.assertNotIn("kham_password", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["udn_account"], legacy_accounts["udn_account"])

        loaded["accounts"].update(legacy_accounts)
        saved = self.request("/save", method="POST", authenticated=True, data=loaded)
        self.assertEqual(saved.code, 200)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("kham_account", stored["accounts"])
        self.assertNotIn("kham_password", stored["accounts"])
        self.assertEqual(stored["accounts"]["udn_account"], legacy_accounts["udn_account"])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

        created = self.request("/profiles", method="POST", authenticated=True, data={"name": "kham", "config": loaded})
        self.assertEqual(created.code, 200)
        profile = json.loads((self.config_path.parent / "profiles" / "kham.json").read_text(encoding="utf-8"))
        self.assertNotIn("kham_password", profile["accounts"])

    def test_session_replacement_expiration_and_unlock_rate_limit(self):
        self.unlock()
        first_token = self.token
        self.unlock()
        self.assertEqual(self.request("/load", headers={"Authorization": f"Bearer {first_token}"}).code, 401)
        self.access.session_expires = 0
        self.assertEqual(self.request("/load", authenticated=True).code, 401)

        for _ in range(5):
            failed = self.request("/unlock", method="POST", data={"passphrase": "synthetic incorrect passphrase"})
            self.assertEqual(failed.code, 401)
        blocked = self.request("/unlock", method="POST", data={"passphrase": self.passphrase})
        self.assertEqual(blocked.code, 429)

    def test_failed_settings_replacement_preserves_previous_file(self):
        settings.util.save_json({"ticket_number": 1}, self.config_path)
        with mock.patch.object(settings.util.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                settings.util.save_json({"ticket_number": 2}, self.config_path)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), {"ticket_number": 1})
        self.assertEqual(list(self.config_path.parent.glob("*.tmp")), [])

    def test_prefill_body_goes_only_to_authenticated_launch(self):
        credentials = {"account": "synthetic-kham-user", "password": "synthetic-kham-password"}
        self.assertEqual(self.request("/run", method="POST", data=credentials).code, 401)
        self.unlock()
        self.assertEqual(
            self.request("/run", method="POST", authenticated=True, data={"account": credentials["account"]}).code,
            400,
        )
        settings.launch_maxbot.assert_not_called()

        response = self.request("/run", method="POST", authenticated=True, data=credentials)
        self.assertEqual(response.code, 200)
        settings.launch_maxbot.assert_called_once_with("", "", kham_prefill=tuple(credentials.values()))
        self.assertNotIn(credentials["account"].encode(), response.body)
        self.assertNotIn(credentials["password"].encode(), response.body)
        self.assertFalse(self.config_path.exists())


class KhamPrefillLaunchTest(TestCase):
    def test_prefill_requires_official_https_and_local_visible_browser(self):
        config = settings.get_default_config()
        config["homepage"] = "https://kham.com.tw/"
        self.assertTrue(settings.permits_kham_prefill(config))
        for homepage in ("http://kham.com.tw/", "https://kham.com.tw.invalid/", "https://kham.com.tw@invalid/"):
            with self.subTest(homepage=homepage):
                self.assertFalse(settings.permits_kham_prefill({**config, "homepage": homepage}))
        for key, value in (("headless", True), ("proxy_server_port", "localhost:8080"), ("mcp_debug_port", 9222)):
            with self.subTest(key=key):
                self.assertFalse(settings.permits_kham_prefill(
                    {**config, "advanced": {**config["advanced"], key: value}}
                ))

    def test_prefill_handoff_uses_child_stdin(self):
        payload = b'{"account":"synthetic","password":"synthetic"}'
        with mock.patch.object(settings.util.subprocess, "Popen") as spawn:
            settings.util.launch_maxbot(extra_args=("--kham-prefill-stdin",), stdin_payload=payload)
        command = spawn.call_args.args[0]
        self.assertEqual(command[-1], "--kham-prefill-stdin")
        self.assertNotIn("synthetic", " ".join(command))
        self.assertEqual(spawn.call_args.kwargs["stdin"], settings.util.subprocess.PIPE)
        spawn.return_value.stdin.__enter__.return_value.write.assert_called_once_with(payload)
