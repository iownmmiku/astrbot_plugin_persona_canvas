"""Offline request-summary submission markers used by the WebUI."""
import unittest

from test_provider_modes import PNG, provider
from providers.base import ProviderError


class SubmissionSummaryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_keeps_submitted_marker_for_all_adapters(self):
        for kind in ("openai", "gemini", "novelai", "custom"):
            with self.subTest(kind=kind):
                item = provider(kind)

                async def fail_after_submission(*_args, **_kwargs):
                    self.assertIs(item.last_request["submitted"], True)
                    raise ProviderError("offline transport timeout")

                item._post.side_effect = fail_after_submission
                with self.assertRaisesRegex(ProviderError, "offline transport timeout"):
                    await item.generate("portrait", "extra fingers")
                self.assertIs(item.last_request["submitted"], True)
                self.assertEqual(item.last_request["prompt"], "portrait")

    async def test_novelai_v4_notes_are_attached_to_a_submitted_request(self):
        item = provider("novelai")
        item._post.return_value = (PNG, "image/png")
        await item.generate("portrait", "extra fingers")
        self.assertIs(item.last_request["submitted"], True)
        self.assertIn("v4_negative_prompt", item.last_request["notes"][0])
        self.assertEqual(item.last_request["negative_prompt"], "extra fingers")

    async def test_local_validation_failure_has_no_submitted_request(self):
        item = provider("novelai")
        with self.assertRaises(ProviderError):
            await item.generate("portrait", "extra fingers", options={"width": -1})
        item._post.assert_not_called()
        self.assertEqual(item.last_request, {})


if __name__ == "__main__":
    unittest.main()
