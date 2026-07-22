from __future__ import annotations

import importlib.util
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_instruct",
    PROJECT_ROOT / "codex-instruct.py",
)
assert SPEC and SPEC.loader
codex_instruct = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_instruct)


class ManagedConfigTests(unittest.TestCase):
    def make_config(self, text: str) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        config_path = Path(temporary_directory.name) / "config.toml"
        config_path.write_text(text, encoding="utf-8")
        return temporary_directory, config_path

    def test_reset_preserves_ccswitch_provider_change(self) -> None:
        temporary_directory, config_path = self.make_config(
            'model_provider = "custom"\n'
            'model = "gpt-5.5"\n\n'
            '[model_providers.custom]\n'
            'base_url = "https://example.invalid/v1"\n'
        )
        self.addCleanup(temporary_directory.cleanup)

        codex_instruct.prepare_deployment_state(
            config_path,
            "gpt-5.6-sol-unrestricted-v5.md",
        )
        codex_instruct.set_model_instructions(
            config_path,
            "gpt-5.6-sol-unrestricted-v5.md",
        )

        # Simulate CCSwitch selecting the official provider after deployment.
        config_path.write_text(
            'model_provider = "openai"\n'
            'model = "gpt-5.5"\n'
            'model_instructions_file = "./gpt-5.6-sol-unrestricted-v5.md"\n\n'
            '[features]\nweb_search = true\n',
            encoding="utf-8",
        )

        changed, status = codex_instruct.restore_managed_model_instructions(config_path)

        self.assertTrue(changed)
        self.assertEqual(status, "removed")
        restored = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["model_provider"], "openai")
        self.assertTrue(restored["features"]["web_search"])
        self.assertNotIn("model_instructions_file", restored)

    def test_round_trip_restores_previous_instruction_entry(self) -> None:
        original_line = 'model_instructions_file = "./personal-instructions.md" # keep me'
        temporary_directory, config_path = self.make_config(
            f'model = "gpt-5.5"\n{original_line}\n'
        )
        self.addCleanup(temporary_directory.cleanup)

        codex_instruct.prepare_deployment_state(
            config_path,
            "gpt-5.6-sol-unrestricted-v35.md",
        )
        codex_instruct.set_model_instructions(
            config_path,
            "gpt-5.6-sol-unrestricted-v35.md",
        )
        changed, status = codex_instruct.restore_managed_model_instructions(config_path)

        self.assertTrue(changed)
        self.assertEqual(status, "restored")
        self.assertIn(original_line, config_path.read_text(encoding="utf-8"))

    def test_nested_assignment_is_not_rewritten(self) -> None:
        nested_line = 'model_instructions_file = "nested-value.md"'
        temporary_directory, config_path = self.make_config(
            f'[profile.test]\n{nested_line}\n'
        )
        self.addCleanup(temporary_directory.cleanup)

        codex_instruct.prepare_deployment_state(
            config_path,
            "gpt-5.6-sol-unrestricted-v5.md",
        )
        codex_instruct.set_model_instructions(
            config_path,
            "gpt-5.6-sol-unrestricted-v5.md",
        )
        codex_instruct.restore_managed_model_instructions(config_path)

        text = config_path.read_text(encoding="utf-8")
        self.assertEqual(text.count(nested_line), 1)
        self.assertNotIn("gpt-5.6-sol-unrestricted-v5.md", text)

    def test_legacy_baseline_migrates_only_previous_instruction(self) -> None:
        temporary_directory, config_path = self.make_config(
            'model_provider = "openai"\n'
            'model_instructions_file = "./gpt-5.6-sol-unrestricted-v5.md"\n'
        )
        self.addCleanup(temporary_directory.cleanup)
        baseline = codex_instruct.baseline_backup_path(config_path)
        baseline.write_text(
            'model_provider = "custom"\n'
            'model_instructions_file = "./personal.md"\n\n'
            '[model_providers.custom]\nbase_url = "https://example.invalid/v1"\n',
            encoding="utf-8",
        )

        changed, status = codex_instruct.restore_managed_model_instructions(config_path)

        self.assertTrue(changed)
        self.assertEqual(status, "restored")
        restored = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["model_provider"], "openai")
        self.assertEqual(restored["model_instructions_file"], "./personal.md")
        self.assertNotIn("model_providers", restored)

    def test_custom_prompt_filename_is_tracked(self) -> None:
        temporary_directory, config_path = self.make_config('model = "gpt-5.5"\n')
        self.addCleanup(temporary_directory.cleanup)
        filename = "custom-prompt.md"

        codex_instruct.prepare_deployment_state(config_path, filename)
        codex_instruct.set_model_instructions(config_path, filename)
        changed, status = codex_instruct.restore_managed_model_instructions(config_path)

        self.assertTrue(changed)
        self.assertEqual(status, "removed")
        self.assertNotIn("model_instructions_file", config_path.read_text(encoding="utf-8"))

    def test_full_reset_removes_state_and_prompt_without_reverting_provider(self) -> None:
        temporary_directory, config_path = self.make_config(
            'model_provider = "custom"\nmodel = "gpt-5.5"\n'
        )
        self.addCleanup(temporary_directory.cleanup)
        codex_home = config_path.parent
        source = codex_home / "source.md"
        source.write_text("test instructions\n", encoding="utf-8")
        args = SimpleNamespace(codex_dir=str(codex_home), dry_run=False)

        result = codex_instruct.deploy_prompt(args, source, "custom-prompt.md")
        self.assertEqual(result, 0)

        # CCSwitch changes provider state while retaining the common instruction entry.
        config_path.write_text(
            'model_provider = "openai"\n'
            'model_instructions_file = "./custom-prompt.md"\n',
            encoding="utf-8",
        )
        with patch("builtins.input", return_value="y"):
            result = codex_instruct.reset_managed_install(args)

        self.assertEqual(result, 0)
        restored = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["model_provider"], "openai")
        self.assertNotIn("model_instructions_file", restored)
        self.assertFalse((codex_home / "custom-prompt.md").exists())
        self.assertFalse(codex_instruct.state_file_path(config_path).exists())

    def test_crlf_line_endings_are_preserved(self) -> None:
        text = 'model = "gpt-5.5"\r\n[features]\r\nweb_search = true\r\n'
        updated = codex_instruct.replace_top_level_model_instructions(
            text,
            'model_instructions_file = "./prompt.md"',
        )

        self.assertNotIn("\n", updated.replace("\r\n", ""))


if __name__ == "__main__":
    unittest.main()
