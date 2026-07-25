#!/usr/bin/env python3
"""stub-cross-references.py — 清理已删除包在剩余 smali 中的交叉引用。

当广告/推送 SDK 包目录被整包删除后，剩余代码中仍可能通过
invoke-static / invoke-virtual / invoke-direct / invoke-interface
引用已删除包中的类和方法。这会导致运行时 ClassNotFoundException 或
NoSuchMethodError，进而引发应用闪退。

本脚本扫描所有剩余 smali 文件，找到引用已删除包的 invoke 指令，
将包含这些 invoke 的方法体替换为安全的空操作桩（根据返回类型返回正确值）。

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
# 方法签名解析
# ---------------------------------------------------------------------------

RE_METHOD_SIG = re.compile(
    r'\.method\s+(?:static\s+)?(.+?)\s+\S+\(([^)]*)\)(\S+)',
    re.DOTALL,
)

def _parse_return_type(sig: str) -> str:
    sig = sig.strip()
    if sig == 'V':
        return 'void'
    if sig == 'Z':
        return 'boolean'
    if sig in ('I', 'B', 'S', 'C'):
        return 'int'
    if sig == 'J':
        return 'wide'
    return 'object'

def _make_safe_stub(method_line: str) -> str:
    """根据方法签名生成类型安全的空操作桩。"""
    sig_match = RE_METHOD_SIG.search(method_line)
    if sig_match:
        ret_type = _parse_return_type(sig_match.group(3))
    else:
        ret_type = 'void'

    if ret_type == 'void':
        return f"{method_line}\n    .registers 1\n    return-void\n.end method"
    elif ret_type == 'boolean':
        return f"{method_line}\n    .registers 2\n    const/4 v0, 0x1\n    return v0\n.end method"
    elif ret_type == 'int':
        return f"{method_line}\n    .registers 2\n    const/4 v0, 0x0\n    return v0\n.end method"
    elif ret_type == 'wide':
        return f"{method_line}\n    .registers 2\n    const-wide/16 v0, 0x0\n    return-wide v0\n.end method"
    else:
        return f"{method_line}\n    .registers 2\n    const/4 v0, 0x0\n    return-object v0\n.end method"

# ---------------------------------------------------------------------------
# 核心扫描与打桩
# ---------------------------------------------------------------------------

# 匹配 invoke 指令中的类型引用
# invoke-virtual {v0, v1}, Lcom/luckycat/sdk/Init;->init()V
RE_INVOKE = re.compile(r'invoke-\w+\s+.*?(L[\w/]+);')

def stub_cross_references(source: Path, deleted_prefixes: list[str]) -> tuple[int, int]:
    """扫描所有 smali 文件，找到引用已删除包的方法并打桩。
    返回 (文件修改数, 方法打桩数)。
    """
    files_modified = 0
    methods_stubbed = 0

    for smali_dir in sorted(source.glob("smali*")):
        if not smali_dir.is_dir():
            continue
        for smali_file in sorted(smali_dir.rglob("*.smali")):
            try:
                content = smali_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # 快速预检：检查文件是否包含任何已删除包前缀
            has_ref = False
            for prefix in deleted_prefixes:
                if prefix in content:
                    has_ref = True
                    break
            if not has_ref:
                continue

            lines = content.split("\n")
            out: list[str] = []
            i = 0
            file_modified = False

            while i < len(lines):
                stripped = lines[i].strip()

                if not stripped.startswith(".method "):
                    out.append(lines[i])
                    i += 1
                    continue

                # 收集整个 method block
                method_lines: list[str] = [lines[i]]
                j = i + 1
                while j < len(lines) and lines[j].strip() != ".end method":
                    method_lines.append(lines[j])
                    j += 1
                if j < len(lines):
                    end_line = lines[j]
                else:
                    end_line = ".end method"

                method_body = "\n".join(method_lines)

                # 检查方法体是否引用了任何已删除包
                ref_found = None
                for prefix in deleted_prefixes:
                    if prefix in method_body:
                        ref_found = prefix
                        break

                if ref_found is not None:
                    out.append(_make_safe_stub(method_lines[0]))
                    methods_stubbed += 1
                    file_modified = True
                    print(f"  ✓ 打桩 {safe_relative(smali_file, source)} (引用: {ref_found})")
                else:
                    out.extend(method_lines)
                    out.append(end_line)

                i = j + 1

            if file_modified:
                smali_file.write_text("\n".join(out), encoding="utf-8")
                files_modified += 1

    return files_modified, methods_stubbed

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理已删除包在剩余 smali 中的交叉引用",
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
    print(f"=== 清理交叉引用 ===")
    print(f"  → 已删除包前缀 ({len(deleted_prefixes)}):")
    for p in deleted_prefixes:
        print(f"      {p}")

    files_modified, methods_stubbed = stub_cross_references(source, deleted_prefixes)
    print(f"\n  → 修改 {files_modified} 个文件，打桩 {methods_stubbed} 个方法")
    print("\n✓ 完成")
    return 0

if __name__ == "__main__":
    sys.exit(main())
