#!/usr/bin/env python3
"""remove-ad-sdk.py — 从 apktool 反编译目录中删除广告/追踪/推送模块。
支持三层操作:
  Layer A — 安全删除已知广告 SDK 包目录
  Layer B — 保留 SDK 文件但对初始化方法打桩 (根据返回类型返回正确值)
  推送删除 — 删除推送包目录 + 清理 Manifest 组件声明
  权限精简 — 只保留白名单 uses-permission
  Cleartext — android:usesCleartextTraffic="true" → "false"

v2 修复:
  - 修复 _stub_method_body() 根据方法签名返回类型生成正确的桩代码
  - 修复 boolean 返回方法 return-void 导致的 VerifyError
  - 修复 object 返回方法的寄存器问题
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_permissions(path: str) -> set[str]:
    """加载权限白名单，跳过空行和 # 注释行。"""
    perms: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                perms.add(line)
    return perms

def safe_relative(path: Path, anchor: Path) -> str:
    """返回 path 相对于 anchor 的字符串；无法相对时返回绝对路径。"""
    try:
        return str(path.relative_to(anchor))
    except ValueError:
        return str(path)

# ---------------------------------------------------------------------------
# Layer A — 安全删除包目录
# ---------------------------------------------------------------------------

def delete_directories(source: Path, packages: list[str]) -> int:
    """在所有 smali* 子目录下删除指定的包目录（带路径逃逸防护）。"""
    count = 0
    source_resolved = source.resolve()
    for smali_dir in sorted(source.glob("smali*")):
        if not smali_dir.is_dir():
            continue
        for pkg in packages:
            target = smali_dir / pkg
            if target.exists():
                # Path escape guard: resolved path must be under source
                try:
                    target.resolve().relative_to(source_resolved)
                except ValueError:
                    print(f"  ✗ 跳过: {target} 不在 {source} 目录下")
                    continue
                shutil.rmtree(target)
                count += 1
                print(f"  ✓ 删除 {safe_relative(target, source)}")
    return count

# ---------------------------------------------------------------------------
# Layer B — 对 SDK 初始化方法打桩 (v2: 类型安全)
# ---------------------------------------------------------------------------

# Smali 方法签名 → 返回类型 的映射
# https://google.github.io/android-gradle-dsl/android/build/api/variant/impl/com/android/build/api/variant/impl/MethodV2Impl.html
RE_METHOD_SIG = re.compile(
    r'\.method\s+(?:static\s+)?(.+?)\s+\S+\(([^)]*)\)(\S+)',
    re.DOTALL,
)

def _parse_return_type(sig: str) -> str:
    """解析 smali 返回类型字符串为分类。
    返回: 'void' | 'boolean' | 'int' | 'object' | 'wide' | 'byte' | 'char' | 'short'
    """
    sig = sig.strip()
    if sig == 'V':
        return 'void'
    if sig == 'Z':
        return 'boolean'
    if sig in ('I', 'B', 'S', 'C'):
        return 'int'
    if sig == 'J':
        return 'wide'
    # Lxxx; 或 [ 开头的都是 object 引用
    return 'object'

def _stub_method_body(method_lines: list[str], first_target: str) -> str:
    """将 smali 方法体替换为类型安全的桩。
    根据方法签名中的返回类型生成正确的返回指令。
    """
    first = method_lines[0]
    # 解析方法签名获取返回类型
    sig_match = RE_METHOD_SIG.search(first)
    if sig_match:
        ret_type = _parse_return_type(sig_match.group(3))
    else:
        # 解析失败时默认按 void 处理
        ret_type = 'void'

    if ret_type == 'void':
        return f"{first}\n    .registers 1\n    return-void\n.end method"
    elif ret_type == 'boolean':
        # 返回 true (0x1) — 大多数初始化方法返回 true 表示成功
        return f"{first}\n    .registers 2\n    const/4 v0, 0x1\n    return v0\n.end method"
    elif ret_type == 'int':
        # 返回 0 — 常见的"成功"返回值
        return f"{first}\n    .registers 2\n    const/4 v0, 0x0\n    return v0\n.end method"
    elif ret_type == 'wide':
        # 返回 0L (需要 const-wide)
        return f"{first}\n    .registers 2\n    const-wide/16 v0, 0x0\n    return-wide v0\n.end method"
    else:
        # object — 返回 null
        return f"{first}\n    .registers 2\n    const/4 v0, 0x0\n    return-object v0\n.end method"

def stub_init_methods(source: Path, init_methods: list[str]) -> int:
    """在所有 .smali 文件中寻找调用 init_methods 的方法并打桩。"""
    count = 0
    for smali_dir in sorted(source.glob("smali*")):
        if not smali_dir.is_dir():
            continue
        for smali_file in sorted(smali_dir.rglob("*.smali")):
            try:
                content = smali_file.read_text(encoding="utf-8")
            except Exception:
                continue
            # 快速预检：只要任何一个 target 字符串不在文件中就跳过
            if not any(t in content for t in init_methods):
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
                    end_line = lines[j]  # .end method
                else:
                    end_line = ".end method"
                method_body = "\n".join(method_lines)
                # 检查是否包含任意 init target
                target_found: str | None = None
                for tgt in init_methods:
                    if tgt in method_body:
                        target_found = tgt
                        break
                if target_found is not None:
                    out.append(_stub_method_body(method_lines, target_found))
                    count += 1
                    file_modified = True
                    print(f"  ✓ 打桩 {safe_relative(smali_file, source)}: {target_found}")
                else:
                    out.extend(method_lines)
                    out.append(end_line)
                i = j + 1
            if file_modified:
                smali_file.write_text("\n".join(out), encoding="utf-8")
    return count

# ---------------------------------------------------------------------------
# Manifest 工具函数
# ---------------------------------------------------------------------------

def _manifest_path(source: Path) -> Path | None:
    p = source / "AndroidManifest.xml"
    return p if p.exists() else None

def remove_manifest_components(manifest_path: Path, components: list[str]) -> int:
    """从 AndroidManifest.xml 删除 <receiver>/<service>/<provider> 声明。"""
    content = manifest_path.read_text(encoding="utf-8")
    total = 0
    for comp in components:
        escaped = re.escape(comp)
        # 自闭合标签: <xxx ... android:name="comp" ... />
        pattern1 = re.compile(
            rf'<(receiver|service|provider)\s+[^>]*?android:name\s*=\s*"{escaped}"[^>]*?/>',
            re.DOTALL,
        )
        def _replacer1(m, c=comp):
            return f"<!-- removed: {c} -->"
        content, n1 = pattern1.subn(_replacer1, content)
        total += n1
        # 配对标签: <xxx ... android:name="comp" ...>...</xxx>
        pattern2 = re.compile(
            rf'<(receiver|service|provider)\s+[^>]*?android:name\s*=\s*"{escaped}"[^>]*?>.*?</\1>',
            re.DOTALL,
        )
        def _replacer2(m, c=comp):
            return f"<!-- removed: {c} -->"
        content, n2 = pattern2.subn(_replacer2, content)
        total += n2
        if n1 + n2 > 0:
            print(f"  ✓ 移除组件声明 ({n1 + n2} 处): {comp}")
    manifest_path.write_text(content, encoding="utf-8")
    return total

def filter_permissions(manifest_path: Path, keep: set[str]) -> int:
    """删除不在白名单中的 uses-permission 行。"""
    content = manifest_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'<uses-permission\s+android:name\s*=\s*"([^"]*)"\s*/?\s*>',
        re.DOTALL,
    )
    removed = 0
    def _replacer(m):
        nonlocal removed
        name = m.group(1)
        if name not in keep:
            removed += 1
            print(f"  ✓ 移除权限: {name}")
            return ""
        return m.group(0)
    content = pattern.sub(_replacer, content)
    manifest_path.write_text(content, encoding="utf-8")
    return removed

def disable_cleartext(manifest_path: Path) -> None:
    """android:usesCleartextTraffic="true" → "false" """
    content = manifest_path.read_text(encoding="utf-8")
    if 'android:usesCleartextTraffic="true"' in content:
        content = content.replace(
            'android:usesCleartextTraffic="true"',
            'android:usesCleartextTraffic="false"',
        )
        manifest_path.write_text(content, encoding="utf-8")
        print("  ✓ Cleartext true → false")

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从 apktool 反编译目录中删除广告 / 追踪 / 推送模块",
    )
    p.add_argument(
        "--source",
        required=True,
        help="apktool d 输出的根目录路径",
    )
    p.add_argument(
        "--ad-config",
        required=True,
        help="广告删除配置文件路径 (JSON)",
    )
    p.add_argument(
        "--push-config",
        required=True,
        help="推送删除配置文件路径 (JSON)",
    )
    p.add_argument(
        "--permissions-keep",
        required=True,
        help="权限白名单文件路径 (txt, 一行一条)",
    )
    return p

def main() -> int:
    args = build_parser().parse_args()
    source = Path(args.source)
    if not source.is_dir():
        print(f"[错误] 源目录不存在: {args.source}", file=sys.stderr)
        return 1

    ad_config = load_json(args.ad_config)
    push_config = load_json(args.push_config)
    keep_perms = load_permissions(args.permissions_keep)
    manifest = _manifest_path(source)
    if manifest is None:
        print("[警告] AndroidManifest.xml 不存在，跳过 manifest 相关操作")

    # ---- Phase 1: Layer A — 删除广告 SDK 目录 ----
    print("\n=== Phase 1: 删除广告 SDK 包目录 ===")
    packages = ad_config.get("packages_to_delete", [])
    if packages:
        n = delete_directories(source, packages)
        print(f"  → 删除 {n} 个广告包目录")
    else:
        print("  (packages_to_delete 为空，跳过)")

    # ---- Phase 2: Layer B — 打桩 SDK 初始化方法 ----
    print("\n=== Phase 2: 打桩 SDK 初始化方法 ===")
    init_methods = ad_config.get("sdk_init_methods", [])
    if init_methods:
        n = stub_init_methods(source, init_methods)
        print(f"  → 打桩 {n} 个初始化方法")
    else:
        print("  (sdk_init_methods 为空，跳过)")

    # ---- Phase 3: 删除推送 SDK 包目录 ----
    print("\n=== Phase 3: 删除推送 SDK 包目录 ===")
    push_packages = push_config.get("push_packages", [])
    if push_packages:
        n = delete_directories(source, push_packages)
        print(f"  → 删除 {n} 个推送包目录")
    else:
        print("  (push_packages 为空，跳过)")

    # ---- Phase 4: no manifest ops here — handled by modify-manifest.py ----
    # (permissions, cleartext, push components are all in modify-manifest.py)

    print("\n✓ 完成")
    return 0

if __name__ == "__main__":
    sys.exit(main())
