#!/usr/bin/env python3
"""stub-cross-references.py — 清理已删除包在剩余 smali 中的交叉引用。

策略 (v2): 精确注释 invoke 指令行，而非替换整个方法体。
v1 版本过于激进，将引用已删除包的整个方法替换为空桩，
会破坏关键生命周期方法 (如 onCreate, onResume 等)，导致构建失败。

本脚本只做两件事:
  1. 注释掉 invoke 指令中对已删除包的调用（前面加 # ）
  2. 注释掉紧随其后的 move-result* 指令，并插入安全默认值
  
对于 void 调用，只需注释 invoke 行。
对于有返回值的调用，同时注释 move-result 行并插入默认值。

用法:
  python3 scripts/stub-cross-references.py \
    --source _build/source \
    --ad-config config/ad-removal.json \
    --push-config config/push-removal.json
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def safe_relative(path: Path, anchor: Path) -> str:
    try:
        return str(path.relative_to(anchor))
    except ValueError:
        return str(path)

# ---------------------------------------------------------------------------
# 构建已删除包的前缀集合
# ---------------------------------------------------------------------------

def build_deleted_prefixes(ad_config: dict, push_config: dict) -> list[str]:
    """从配置中收集所有已删除包的 smali 路径前缀。
    返回类似 ['Lcom/luckycat', 'Lcom/bytedance/pangle', ...] 的列表。
    """
    prefixes: set[str] = set()
    for pkg in ad_config.get("packages_to_delete", []):
        prefixes.add(f"L{pkg}")
    for pkg in push_config.get("push_packages", []):
        prefixes.add(f"L{pkg}")
    return sorted(prefixes)

# ---------------------------------------------------------------------------
# 核心扫描与精确注释
# ---------------------------------------------------------------------------

# 匹配 move-result 指令
RE_MOVE_RESULT = re.compile(r'^(\s*)(move-result\w*)\s')

# 匹配 invoke 的返回类型 (从方法描述符中提取)
RE_INVOKE_RET = re.compile(r'->\w+\([^)]*\)(\w+)\s*$')

def _is_deleted_ref(line: str, deleted_prefixes: list[str]) -> str | None:
    """检查一行代码是否引用了已删除包。返回匹配的前缀或 None。"""
    for prefix in deleted_prefixes:
        if prefix in line:
            return prefix
    return None

def _get_invoke_return_type(line: str) -> str | None:
    """从 invoke 指令中提取返回类型。"""
    m = RE_INVOKE_RET.search(line)
    return m.group(1) if m else None

def stub_cross_references(source: Path, deleted_prefixes: list[str]) -> tuple[int, int]:
    """精确注释掉 invoke 指令中对已删除包的引用。
    返回 (文件修改数, 指令注释数)。
    """
    files_modified = 0
    lines_commented = 0

    for smali_dir in sorted(source.glob("smali*")):
        if not smali_dir.is_dir():
            continue
        for smali_file in sorted(smali_dir.rglob("*.smali")):
            try:
                content = smali_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # 快速预检
            has_ref = False
            for prefix in deleted_prefixes:
                if prefix in content:
                    has_ref = True
                    break
            if not has_ref:
                continue

            lines = content.split("\n")
            out: list[str] = []
            file_modified = False
            skip_next = False  # 标记跳过已处理的 move-result 行

            for idx, line in enumerate(lines):
                # 如果上一轮标记了跳过
                if skip_next:
                    skip_next = False
                    continue

                stripped = line.strip()

                # 空行和标签直接输出
                if not stripped or stripped.startswith(".") or stripped == ":":
                    out.append(line)
                    continue

                # 已经被注释过的行直接输出
                if stripped.startswith("#"):
                    out.append(line)
                    continue

                # 检查是否是 invoke 指令且引用了已删除包
                ref = _is_deleted_ref(stripped, deleted_prefixes)
                if ref and "invoke-" in stripped:
                    ret_type = _get_invoke_return_type(stripped)

                    # 注释掉 invoke 行
                    out.append(f"# [cross-ref-stub] {line}")
                    lines_commented += 1
                    file_modified = True

                    # 检查下一行是否是 move-result
                    if idx + 1 < len(lines):
                        next_line = lines[idx + 1]
                        next_stripped = next_line.strip()
                        mr_match = RE_MOVE_RESULT.match(next_stripped)

                        if mr_match and not next_stripped.startswith("#"):
                            mr_indent = mr_match.group(1)

                            if ret_type == 'V':
                                # void 方法不应该有 move-result，直接跳过
                                skip_next = True
                            elif ret_type == 'Z':
                                # boolean: 插入 true
                                out.append(f"# [cross-ref-stub] {next_line}")
                                out.append(f"{mr_indent}const/4 v0, 0x1")
                                lines_commented += 1
                                skip_next = True
                            elif ret_type in ('J', 'D'):
                                # wide/double: 插入 0L
                                out.append(f"# [cross-ref-stub] {next_line}")
                                out.append(f"{mr_indent}const-wide/16 v0, 0x0")
                                lines_commented += 1
                                skip_next = True
                            elif ret_type and (ret_type.startswith('L') or ret_type.startswith('[')):
                                # object: 插入 null
                                out.append(f"# [cross-ref-stub] {next_line}")
                                out.append(f"{mr_indent}const/4 v0, 0x0")
                                lines_commented += 1
                                skip_next = True
                            else:
                                # int/byte/short/char: 插入 0
                                out.append(f"# [cross-ref-stub] {next_line}")
                                out.append(f"{mr_indent}const/4 v0, 0x0")
                                lines_commented += 1
                                skip_next = True
                    continue

                out.append(line)

            if file_modified:
                smali_file.write_text("\n".join(out), encoding="utf-8")
                files_modified += 1
                print(f"  ✓ 处理 {safe_relative(smali_file, source)}")

    return files_modified, lines_commented

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理已删除包在剩余 smali 中的交叉引用 (精确注释模式)",
    )
    parser.add_argument("--source", required=True, help="apktool 反编译根目录")
    parser.add_argument("--ad-config", required=True, help="广告删除配置 (JSON)")
    parser.add_argument("--push-config", required=True, help="推送删除配置 (JSON)")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"[错误] 源目录不存在: {args.source}", file=sys.stderr)
        return 1

    ad_config = load_json(args.ad_config)
    push_config = load_json(args.push_config)

    deleted_prefixes = build_deleted_prefixes(ad_config, push_config)
    print(f"=== 清理交叉引用 (v2: 精确注释模式) ===")
    print(f"  → 已删除包前缀 ({len(deleted_prefixes)}):")
    for p in deleted_prefixes:
        print(f"      {p}")

    files_modified, lines_commented = stub_cross_references(source, deleted_prefixes)
    print(f"\n  → 修改 {files_modified} 个文件，注释 {lines_commented} 条指令")
    print("\n✓ 完成")
    return 0

if __name__ == "__main__":
    sys.exit(main())
