#!/usr/bin/env python3
"""Select, deploy, or remove a gpt-5.6-sol Codex instruction file.

The public repository stores each prompt as a ZIP archive. Applying a version
extracts its Markdown file into CODEX_HOME, snapshots config.toml, and sets the
top-level `model_instructions_file` entry. Reset only restores that managed
entry and removes prompt files owned by this script; it never replaces the
whole config.toml, so provider managers such as CCSwitch remain authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PROMPT_VERSIONS = {
    "v5": {
        "md_filename": "gpt-5.6-sol-unrestricted-v5.md",
        "archive": PROJECT_ROOT / "gpt-5.6-sol-unrestricted-v5.zip",
    },
    "v35": {
        "md_filename": "gpt-5.6-sol-unrestricted-v35.md",
        "archive": PROJECT_ROOT / "gpt-5.6-sol-unrestricted-v35.zip",
    },
}
MANAGED_PROMPT_FILENAMES = {
    *(choice["md_filename"] for choice in PROMPT_VERSIONS.values()),
}
BASELINE_BACKUP_SUFFIX = ".gpt56-sol-instruct.bak"
STATE_FILENAME = ".gpt56-sol-instruct-state.json"
STATE_VERSION = 1

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DARK_GREEN = "\033[38;2;0;100;0m"
ANSI_DARK_ORANGE = "\033[38;2;205;102;0m"
BANNER_WIDTH = 72


def color_enabled() -> bool:
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return sys.stdout.isatty()


def styled(text: str, *codes: str) -> str:
    if not color_enabled():
        return text
    return f"{''.join(codes)}{text}{ANSI_RESET}"


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def section_banner(title: str) -> str:
    label = f" {title} "
    fill_width = max(4, BANNER_WIDTH - display_width(label))
    left = fill_width // 2
    right = fill_width - left
    return styled(f"{'━' * left}{label}{'━' * right}", ANSI_BOLD)


def intro_text() -> str:
    zh_banner = section_banner("中文说明")
    en_banner = section_banner("English Instructions")
    zh_title = styled("gpt-5.6-sol Codex 提示词选择说明：", ANSI_BOLD)
    en_title = styled("gpt-5.6-sol Codex instruction selection instructions:", ANSI_BOLD)
    zh_recommended = styled("推荐", ANSI_BOLD, ANSI_DARK_GREEN)
    en_recommended = styled("recommended", ANSI_BOLD, ANSI_DARK_GREEN)
    zh_v35_notice = styled(
        "建议仅在 v5 无法满足需求时使用",
        ANSI_BOLD,
        ANSI_DARK_ORANGE,
    )
    en_v35_notice = styled(
        "use them only when v5 does not meet your needs",
        ANSI_BOLD,
        ANSI_DARK_ORANGE,
    )
    return f"""\
{zh_banner}
{zh_title}

v5 提示词较为简单，足够应付多数场景（{zh_recommended}）。
v35 提示词加入对特殊任务的优化，但安全性不如 v5（{zh_v35_notice}）。

选择后会将相应提示词.md文件复制到CODEX_HOME中，在config.toml中写入model_instructions_file项，并创建操作前快照。卸载时只恢复这一项，不会覆盖CCSwitch管理的provider、模型或认证配置。

{en_banner}
{en_title}

v5 instructions are simpler and sufficient for most scenarios ({en_recommended}).
v35 instructions add optimizations for specialized tasks, but are less safe than v5 ({en_v35_notice}).

After a version is selected, its prompt .md file is copied to CODEX_HOME, the model_instructions_file entry is written to config.toml, and a pre-operation snapshot is created. Uninstall restores only that entry and never replaces provider, model, or authentication settings managed by CCSwitch.
"""


def menu_text() -> str:
    selection_banner = section_banner("操作选择 / Select an Action")
    recommendation = styled("推荐 / Recommended", ANSI_BOLD, ANSI_DARK_GREEN)
    v35_notice = styled(
        "按说明谨慎使用 / Use with precaution",
        ANSI_BOLD,
        ANSI_DARK_ORANGE,
    )
    return f"""\
{selection_banner}
1. 植入 v5 提示词 / Apply v5 instructions file （{recommendation}）
2. 植入 v35 提示词 / Apply v35 instructions file （{v35_notice}）
3. 去除提示词并恢复原配置项 / Remove managed instructions
q. 退出而不执行任何操作 / Quit without modification
"""


def find_codex_dirs() -> list[Path]:
    candidates: set[Path] = set()
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        candidates.add(Path(env_home).expanduser())
    candidates.add(Path.home() / ".codex")
    return sorted(path.resolve() for path in candidates if (path / "config.toml").exists())


def selected_codex_dirs(codex_dir: str | None) -> list[Path]:
    if codex_dir:
        return [Path(codex_dir).expanduser().resolve()]
    return find_codex_dirs()


def backup_file(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = path.with_suffix(path.suffix + f".bak_{timestamp}")
    shutil.copy2(path, backup)
    return backup


def baseline_backup_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + BASELINE_BACKUP_SUFFIX)


def state_file_path(config_path: Path) -> Path:
    return config_path.parent / STATE_FILENAME


def top_level_model_instructions_line(text: str) -> str | None:
    """Return the root model_instructions_file assignment, ignoring TOML tables."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("["):
            break
        if re.match(r"^\s*model_instructions_file\s*=", line):
            return line
    return None


def line_references_managed_prompt(line: str | None, filenames: set[str]) -> bool:
    if not line:
        return False
    match = re.match(
        r"^\s*model_instructions_file\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$",
        line,
    )
    if not match:
        return False
    referenced_name = match.group(2).replace("\\", "/").rsplit("/", 1)[-1]
    return referenced_name in filenames


def replace_top_level_model_instructions(text: str, replacement: str | None) -> str:
    """Replace only the root assignment while preserving all unrelated TOML text."""
    lines = text.splitlines(keepends=True)
    table_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    assignment_indexes = [
        index
        for index, line in enumerate(lines[:table_index])
        if re.match(r"^\s*model_instructions_file\s*=", line)
    ]

    if assignment_indexes:
        first = assignment_indexes[0]
        newline = (
            "\r\n"
            if lines[first].endswith("\r\n")
            else "\n"
            if lines[first].endswith("\n")
            else ""
        )
        if replacement is None:
            del lines[first]
        else:
            lines[first] = replacement + newline
        return "".join(lines)

    if replacement is None:
        return text

    insert_at = next(
        (
            index + 1
            for index, line in enumerate(lines[:table_index])
            if re.match(r"^\s*model\s*=", line)
        ),
        table_index,
    )
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    if insert_at > 0 and lines[insert_at - 1] and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += newline
    lines.insert(insert_at, replacement + newline)
    return "".join(lines)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a text file while retaining its existing permissions."""
    previous_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, previous_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_state(config_path: Path) -> dict[str, object] | None:
    path = state_file_path(config_path)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return None
    return state


def save_state(config_path: Path, state: dict[str, object]) -> None:
    atomic_write_text(
        state_file_path(config_path),
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


def previous_instruction_from_legacy_baseline(config_path: Path) -> str | None:
    """Migrate only the old instruction entry, never provider data, from a legacy backup."""
    baseline = baseline_backup_path(config_path)
    if not baseline.exists():
        return None
    line = top_level_model_instructions_line(baseline.read_text(encoding="utf-8"))
    if line_references_managed_prompt(line, MANAGED_PROMPT_FILENAMES):
        return None
    return line


def prepare_deployment_state(config_path: Path, md_filename: str) -> dict[str, object]:
    state = read_state(config_path)
    if state is None:
        current_line = top_level_model_instructions_line(
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        )
        if line_references_managed_prompt(
            current_line,
            MANAGED_PROMPT_FILENAMES | {md_filename},
        ):
            current_line = previous_instruction_from_legacy_baseline(config_path)
        state = {
            "version": STATE_VERSION,
            "previous_model_instructions_line": current_line,
            "managed_prompt_filenames": [],
        }

    stored_filenames = state.get("managed_prompt_filenames", [])
    filenames = (
        {name for name in stored_filenames if isinstance(name, str)}
        if isinstance(stored_filenames, list)
        else set()
    )
    filenames.add(md_filename)
    state["managed_prompt_filenames"] = sorted(filenames)
    save_state(config_path, state)
    return state


def set_model_instructions(config_path: Path, md_filename: str) -> bool:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    target = f'model_instructions_file = "./{md_filename}"'
    new_text = replace_top_level_model_instructions(text, target)
    if new_text != text:
        atomic_write_text(config_path, new_text)
        return True
    return False


def restore_managed_model_instructions(config_path: Path) -> tuple[bool, str]:
    if not config_path.exists():
        return False, "missing"

    state = read_state(config_path)
    managed_filenames = set(MANAGED_PROMPT_FILENAMES)
    if state:
        stored_filenames = state.get("managed_prompt_filenames", [])
        if isinstance(stored_filenames, list):
            managed_filenames.update(
                name for name in stored_filenames if isinstance(name, str)
            )

    text = config_path.read_text(encoding="utf-8")
    current_line = top_level_model_instructions_line(text)
    if not line_references_managed_prompt(current_line, managed_filenames):
        return False, "not-managed"

    previous_line = state.get("previous_model_instructions_line") if state else None
    if previous_line is not None and not isinstance(previous_line, str):
        previous_line = None
    if state is None:
        previous_line = previous_instruction_from_legacy_baseline(config_path)

    new_text = replace_top_level_model_instructions(text, previous_line)
    if new_text != text:
        atomic_write_text(config_path, new_text)
        return True, "restored" if previous_line else "removed"
    return False, "unchanged"


def read_prompt(source_path: Path, expected_md_filename: str) -> str:
    """Read a Markdown prompt directly or extract it from a ZIP archive."""
    if source_path.suffix.lower() != ".zip":
        return source_path.read_text(encoding="utf-8")

    with zipfile.ZipFile(source_path) as archive:
        files = [name for name in archive.namelist() if not name.endswith("/")]
        preferred = [name for name in files if Path(name).name == expected_md_filename]
        markdown_files = [name for name in files if Path(name).suffix.lower() == ".md"]
        candidates = preferred or markdown_files
        if len(candidates) != 1:
            raise ValueError(
                f"压缩包应包含唯一的 {expected_md_filename}（或唯一 Markdown 文件），"
                f"实际候选: {candidates}"
            )
        member = candidates[0]
        with tempfile.TemporaryDirectory(prefix="gpt56-sol-prompt-") as temp_dir:
            extracted_path = Path(archive.extract(member, path=temp_dir))
            return extracted_path.read_text(encoding="utf-8")


def deploy_prompt(
    args: argparse.Namespace,
    prompt_path: Path,
    md_filename: str,
) -> int:
    if Path(md_filename).name != md_filename or not md_filename.endswith(".md"):
        print(f"[错误] 目标名称必须是不含路径的 .md 文件名: {md_filename}", file=sys.stderr)
        return 2
    if not prompt_path.exists():
        print(f"[错误] 提示词文件不存在: {prompt_path}", file=sys.stderr)
        return 2

    codex_dirs = selected_codex_dirs(args.codex_dir)
    if not codex_dirs:
        print("[错误] 未找到 .codex/config.toml；请使用 --codex-dir 指定。", file=sys.stderr)
        return 2

    try:
        prompt_text = read_prompt(prompt_path, md_filename)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"[错误] 读取或解压提示词失败: {exc}", file=sys.stderr)
        return 2

    source_kind = "ZIP（已解压校验）" if prompt_path.suffix.lower() == ".zip" else "Markdown"
    print(f"[+] Prompt: {prompt_path} [{source_kind}]")
    for codex_dir in codex_dirs:
        config_path = codex_dir / "config.toml"
        destination = codex_dir / md_filename
        print(f"\n── 目标 / Target: {codex_dir} ──")
        print(f"  写入 / Write: {destination}")
        print(f'  配置 / Config: model_instructions_file = "./{md_filename}"')
        print("  兼容模式 / Compatibility: 仅修改本项目管理的配置项")
        if args.dry_run:
            continue

        codex_dir.mkdir(parents=True, exist_ok=True)
        if not config_path.exists():
            atomic_write_text(config_path, "")
            print("  创建 / Created: config.toml")
        snapshot = backup_file(config_path)
        print(f"  已创建操作前备份 / Snapshot saved: {snapshot.name}")

        prepare_deployment_state(config_path, md_filename)
        atomic_write_text(destination, prompt_text)
        changed = set_model_instructions(config_path, md_filename)
        print("  状态 / Status:", "已更新 / Updated" if changed else "已是最新 / Already current")
    return 0


def confirm_reset() -> bool:
    print("  将只恢复本脚本管理的 model_instructions_file，并移除提示词文件。")
    print("  provider、模型、认证及CCSwitch在部署后写入的其他配置均保持不变。")
    print("  Only the managed model_instructions_file entry and prompt files will change.")
    print("  Provider, model, authentication, and later CCSwitch changes will be preserved.")
    try:
        answer = input("确认继续？/ Confirm removal? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes", "是"}


def reset_managed_install(args: argparse.Namespace) -> int:
    codex_dirs = selected_codex_dirs(args.codex_dir)
    if not codex_dirs:
        print("[错误] 未找到 .codex/config.toml；请使用 --codex-dir 指定。", file=sys.stderr)
        return 2

    for codex_dir in codex_dirs:
        config_path = codex_dir / "config.toml"
        print(f"\n── 目标 / Target: {codex_dir} ──")
        state = read_state(config_path)
        managed_filenames = set(MANAGED_PROMPT_FILENAMES)
        if state:
            stored_filenames = state.get("managed_prompt_filenames", [])
            if isinstance(stored_filenames, list):
                managed_filenames.update(
                    name
                    for name in stored_filenames
                    if isinstance(name, str) and Path(name).name == name
                )

        if not confirm_reset():
            print("  未确认，已取消 / Confirmation not received; reset cancelled.")
            continue

        print("  配置 / Config: 字段级恢复，不覆盖 config.toml")
        for filename in sorted(managed_filenames):
            print(f"  移除 / Remove: {filename}")
        if args.dry_run:
            print("  预览完成，未修改文件 / Dry run complete; no files changed.")
            continue

        codex_dir.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            snapshot = backup_file(config_path)
            print(f"  已创建恢复前备份 / Pre-reset snapshot: {snapshot.name}")

        _changed, status = restore_managed_model_instructions(config_path)
        status_messages = {
            "restored": "已恢复原配置项 / Previous entry restored",
            "removed": "已移除脚本配置项 / Managed entry removed",
            "not-managed": "当前配置项不属于本脚本，保持不变 / Current entry left unchanged",
            "missing": "config.toml 不存在 / Not found",
            "unchanged": "无需修改 / No change needed",
        }
        print("  配置状态 / Config status:", status_messages[status])

        removed = 0
        for filename in managed_filenames:
            prompt_path = codex_dir / filename
            if prompt_path.exists():
                prompt_path.unlink()
                removed += 1
        state_path = state_file_path(config_path)
        if state_path.exists():
            state_path.unlink()
        print(f"  提示词状态 / Prompt status: 已移除 {removed} 个文件 / Removed {removed} file(s)")
    return 0


def interactive_action() -> str:
    print(intro_text())
    print(menu_text())
    while True:
        try:
            choice = input("请选择 / Select [1/2/3/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        actions = {"1": "v5", "2": "v35", "3": "reset", "q": "quit"}
        if choice in actions:
            return actions[choice]
        print("[错误] 请输入 1、2、3 或 q / Enter 1, 2, 3, or q.")


def inferred_md_filename(source: Path, requested_name: str | None) -> str:
    if requested_name:
        return requested_name if requested_name.endswith(".md") else f"{requested_name}.md"
    return f"{source.stem}.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select, extract, deploy, or reset a gpt-5.6-sol Codex instruction file."
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--version", choices=("v5", "v35"), help="Apply v5 or v35")
    action_group.add_argument(
        "--reset",
        action="store_true",
        help="Remove managed prompts without replacing config.toml",
    )
    action_group.add_argument("--file", "-f", help="Apply a custom instruction ZIP or Markdown file")
    parser.add_argument("--name", "-n", help="Destination filename for --file, with or without .md")
    parser.add_argument("--codex-dir", help="Explicit Codex home directory, e.g. ~/.codex")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = parser.parse_args()

    if args.name and not args.file:
        parser.error("--name 仅能与 --file 一起使用 / --name requires --file")

    if args.version:
        action = args.version
    elif args.reset:
        action = "reset"
    elif args.file:
        source = Path(args.file).expanduser().resolve()
        return deploy_prompt(args, source, inferred_md_filename(source, args.name))
    else:
        action = interactive_action()

    if action == "quit":
        print("未执行修改 / No modification made.")
        return 0
    if action == "reset":
        return reset_managed_install(args)

    selected = PROMPT_VERSIONS[action]
    return deploy_prompt(args, selected["archive"], selected["md_filename"])


if __name__ == "__main__":
    raise SystemExit(main())
