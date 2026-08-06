"""The muse-spark inference provider: credential resolution and the
fail-closed behaviour when its endpoint is not configured.

No real credential appears here; every key is an obvious placeholder.
"""
import sys
import types
import unittest
from unittest import mock

import alr_quote_verifier as verifier

FAKE_META_KEY = "test-meta-key-not-a-real-credential"
FAKE_BASE_URL = "https://muse.invalid/v1"


def _stub_keys_module(**attrs):
    """Stand in for the gitignored dev keys.py so the test never depends on
    whether the developer's real module defines a name."""
    module = types.ModuleType("keys")
    for name, value in attrs.items():
        setattr(module, name, value)
    return mock.patch.dict(sys.modules, {"keys": module})


class MuseKeyResolutionTests(unittest.TestCase):
    def test_key_resolves_from_the_environment(self):
        with _stub_keys_module(), \
             mock.patch.dict(verifier.os.environ, {"META_API_KEY": FAKE_META_KEY}), \
             mock.patch.object(verifier, "LLM_API_KEY", ""):
            self.assertEqual(verifier._resolve_muse_api_key(), FAKE_META_KEY)

    def test_key_resolves_from_the_dev_keys_module(self):
        env = {k: v for k, v in verifier.os.environ.items()
               if k not in ("META_API_KEY", "MODEL_API_KEY")}
        with _stub_keys_module(META_API_KEY=FAKE_META_KEY), \
             mock.patch.dict(verifier.os.environ, env, clear=True), \
             mock.patch.object(verifier, "LLM_API_KEY", ""):
            self.assertEqual(verifier._resolve_muse_api_key(), FAKE_META_KEY)

    def test_missing_key_resolves_empty_rather_than_borrowing_openai(self):
        env = {k: v for k, v in verifier.os.environ.items()
               if k not in ("META_API_KEY", "MODEL_API_KEY")}
        with _stub_keys_module(ALT_OPENAI_API_KEY="test-openai-key"), \
             mock.patch.dict(verifier.os.environ, env, clear=True), \
             mock.patch.object(verifier, "LLM_API_KEY", ""):
            self.assertEqual(verifier._resolve_muse_api_key(), "")


class MuseEndpointTests(unittest.TestCase):
    def test_selecting_the_provider_without_a_base_url_is_an_error(self):
        with mock.patch.object(verifier, "MUSE_BASE_URL", ""), \
             mock.patch.object(verifier, "LLM_PROVIDER", verifier.MUSE_PROVIDER):
            with self.assertRaises(RuntimeError) as caught:
                verifier._resolve_llm_endpoint()

        message = str(caught.exception)
        self.assertIn("MUSE_BASE_URL", message)
        self.assertIn(verifier.MUSE_PROVIDER, message)

    def test_select_provider_refuses_without_a_base_url(self):
        with mock.patch.object(verifier, "MUSE_BASE_URL", ""), \
             mock.patch.object(verifier, "LLM_PROVIDER", "openai"), \
             mock.patch.object(verifier, "LLM_MODEL", verifier._DEFAULT_LLM_MODEL):
            with self.assertRaises(RuntimeError):
                verifier._select_llm_provider(verifier.MUSE_PROVIDER)
            # The failed selection must not have switched the live provider.
            self.assertEqual(verifier.LLM_PROVIDER, "openai")
            self.assertEqual(verifier.LLM_MODEL, verifier._DEFAULT_LLM_MODEL)

    def test_configured_provider_uses_its_own_key_and_endpoint(self):
        with _stub_keys_module(), \
             mock.patch.dict(verifier.os.environ, {"META_API_KEY": FAKE_META_KEY}), \
             mock.patch.object(verifier, "LLM_API_KEY", ""), \
             mock.patch.object(verifier, "MUSE_BASE_URL", FAKE_BASE_URL), \
             mock.patch.object(verifier, "LLM_PROVIDER", verifier.MUSE_PROVIDER):
            self.assertEqual(
                verifier._resolve_llm_endpoint(), (FAKE_META_KEY, FAKE_BASE_URL))

    def test_missing_key_falls_back_to_the_default_provider(self):
        env = {k: v for k, v in verifier.os.environ.items()
               if k not in ("META_API_KEY", "MODEL_API_KEY")}
        with _stub_keys_module(), \
             mock.patch.dict(verifier.os.environ, env, clear=True), \
             mock.patch.object(verifier, "LLM_API_KEY", ""), \
             mock.patch.object(verifier, "MUSE_BASE_URL", FAKE_BASE_URL), \
             mock.patch.object(verifier, "LLM_PROVIDER", verifier.MUSE_PROVIDER), \
             mock.patch.object(verifier, "LLM_BASE_URL", ""), \
             mock.patch.object(verifier, "_resolve_api_key", return_value="openai-key"):
            key, base_url = verifier._resolve_llm_endpoint()

        # Falls back whole: the OpenAI key never travels to the muse endpoint.
        self.assertEqual((key, base_url), ("openai-key", ""))

    def test_select_provider_degrades_to_openai_without_a_key(self):
        env = {k: v for k, v in verifier.os.environ.items()
               if k not in ("META_API_KEY", "MODEL_API_KEY")}
        with _stub_keys_module(), \
             mock.patch.dict(verifier.os.environ, env, clear=True), \
             mock.patch.object(verifier, "LLM_API_KEY", ""), \
             mock.patch.object(verifier, "MUSE_BASE_URL", FAKE_BASE_URL), \
             mock.patch.object(verifier, "LLM_PROVIDER", "openai"), \
             mock.patch.object(verifier, "LLM_MODEL", verifier._DEFAULT_LLM_MODEL), \
             mock.patch.object(verifier, "client", None):
            self.assertEqual(
                verifier._select_llm_provider(verifier.MUSE_PROVIDER), "openai")
            self.assertEqual(verifier.LLM_MODEL, verifier._DEFAULT_LLM_MODEL)

    def test_select_provider_switches_model_and_resets_the_client(self):
        with _stub_keys_module(), \
             mock.patch.dict(verifier.os.environ, {"META_API_KEY": FAKE_META_KEY}), \
             mock.patch.object(verifier, "LLM_API_KEY", ""), \
             mock.patch.object(verifier, "MUSE_BASE_URL", FAKE_BASE_URL), \
             mock.patch.object(verifier, "MUSE_MODEL", "muse-spark-1.2"), \
             mock.patch.object(verifier, "LLM_PROVIDER", "openai"), \
             mock.patch.object(verifier, "LLM_MODEL", verifier._DEFAULT_LLM_MODEL), \
             mock.patch.object(verifier, "client", object()):
            selected = verifier._select_llm_provider(verifier.MUSE_PROVIDER)

            self.assertEqual(selected, verifier.MUSE_PROVIDER)
            self.assertEqual(verifier.LLM_MODEL, "muse-spark-1.2")
            self.assertIsNone(verifier.client)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(RuntimeError):
            verifier._select_llm_provider("definitely-not-a-provider")

    def test_default_provider_is_unchanged(self):
        # The production path must not move just because muse exists.
        self.assertEqual(verifier.LLM_PROVIDER, "openai")
        self.assertEqual(verifier.MUSE_BASE_URL, "")


if __name__ == "__main__":
    unittest.main()
