"""Redacted provider editors must preserve credentials without returning them."""
import copy
import json
import unittest

import test_core as fixtures
from canvas_test.providers.base import ProviderError, provider_from_config
from canvas_test.storage import Storage


class ProviderSecretsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def configure(self, extra):
        return await self.plugin.web_save_provider({
            "name": "body-auth", "kind": "custom", "endpoint": "https://drawing.example",
            "model": "test-model", "extra_body": extra,
        })

    def config(self):
        return self.store.settings["providers"]["body-auth"]

    async def test_redacted_nested_and_list_secrets_survive_editor_save_and_restart(self):
        original = {"request": {"token": "fixture-body-token", "quality": "high"},
                    "accounts": [{"api-key": "fixture-first-key", "label": "first"},
                                 {"authorization": "Bearer fixture-second-key"}]}
        editor = await self.configure(original)
        self.assertNotIn("fixture-body-token", json.dumps(editor))
        self.assertNotIn("fixture-first-key", json.dumps(editor))
        self.assertNotIn("fixture-second-key", json.dumps(editor))
        before = copy.deepcopy(editor)
        editor["timeout"] = 240
        saved = await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config()["extra_body"], original)
        self.assertEqual(editor["extra_body"], before["extra_body"])
        self.assertNotIn("fixture-body-token", json.dumps(saved))
        prepared = provider_from_config("body-auth", self.config())._prepare_generation("portrait", "")
        self.assertEqual(prepared.body["request"], original["request"])
        self.assertEqual(prepared.body["accounts"], original["accounts"])
        self.assertNotIn("has_token", json.dumps(prepared.body))
        self.assertNotIn("has_api_key", json.dumps(prepared.body))
        self.assertNotIn("has_authorization", json.dumps(prepared.body))
        reopened = Storage(self.store.root)
        try:
            self.assertEqual(reopened.settings["providers"]["body-auth"]["extra_body"], original)
        finally:
            reopened.close()

    async def test_explicit_replacement_empty_and_removed_secret_take_precedence(self):
        editor = await self.configure({"request": {"token": "fixture-original-token", "quality": "high"}})
        editor["extra_body"]["request"]["token"] = "fixture-replacement-token"
        editor = await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config()["extra_body"]["request"]["token"], "fixture-replacement-token")
        self.assertNotIn("fixture-replacement-token", json.dumps(editor))
        editor["extra_body"]["request"]["token"] = ""
        editor = await self.plugin.web_save_provider(editor)
        self.assertFalse(editor["extra_body"]["request"]["has_token"])
        editor = await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config()["extra_body"]["request"]["token"], "")
        del editor["extra_body"]["request"]["has_token"]
        await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config()["extra_body"], {"request": {"quality": "high"}})

    async def test_removing_container_does_not_resurrect_old_credentials(self):
        editor = await self.configure({"accounts": [{"api_key": "fixture-secret-one"},
                                                     {"token": "fixture-secret-two"}],
                                       "optional": {"password": "fixture-password"}})
        editor["extra_body"] = {"accounts": [{"api_key": "fixture-new-secret"}]}
        await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config()["extra_body"], {"accounts": [{"api_key": "fixture-new-secret"}]})
        await self.plugin.web_save_provider({"name": "body-auth", "extra_body": {}})
        self.assertEqual(self.config()["extra_body"], {})

    async def test_invalid_provider_update_preserves_original_credentials(self):
        editor = await self.configure({"token": "fixture-secret-token"})
        before = copy.deepcopy(self.config())
        editor["timeout"] = 0
        with self.assertRaises(ProviderError):
            await self.plugin.web_save_provider(editor)
        self.assertEqual(self.config(), before)

    async def test_runtime_errors_redact_nested_body_and_workflow_credentials(self):
        await self.configure({"request": {"token": "fixture-body-token"},
                              "accounts": [{"password": "fixture-account-password"}]})
        self.config()["workflow"] = {"10": {"inputs": {"api_key": "fixture-node-key"}}}
        self.config()["api_key"] = "short"
        message = self.plugin._error(ValueError(
            "Provider rejected token=fixture-body-token password=fixture-account-password "
            "node=fixture-node-key header=short Bearer unexpected-header-key"
        ))
        for secret in ("fixture-body-token", "fixture-account-password", "fixture-node-key", "short", "unexpected-header-key"):
            self.assertNotIn(secret, message)
        self.assertIn("Provider rejected", message)
        self.assertLessEqual(len(message), 500)

    async def test_runtime_error_redacts_before_clipping_a_long_credential(self):
        secret = "fixture-secret-" + "a" * 1800
        await self.configure({"token": secret})
        message = self.plugin._error(ValueError("Rejected " + secret))
        self.assertNotIn("fixture-secret-", message)
        self.assertNotIn("a" * 20, message)
        self.assertIn("Rejected", message)


if __name__ == "__main__":
    unittest.main()
