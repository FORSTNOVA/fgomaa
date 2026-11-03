"""
FGO MAA Fast Distribution Synchronizer - 快速软件发布包同步器
将工作区内的最新源码 (src)、前端资产 (web)、策略配置 (config)、数据字典 (data)
与战斗模拟运行时 (runtime)
毫秒级热同步到 dist/FGO-MAA/ 发布目录，确保软件无需等待耗时编译即可即时生效最新逻辑。
"""

import sys
import shutil
from pathlib import Path

root_dir = Path(__file__).resolve().parent
dist_app_dir = root_dir / "dist" / "FGO-MAA"

def sync(verbose: bool = True):
    if not dist_app_dir.exists():
        dist_app_dir.mkdir(parents=True, exist_ok=True)

    internal_dir = dist_app_dir / "_internal"

    synced_folders = ["src", "web", "config", "data", "runtime"]
    for folder in synced_folders:
        src_path = root_dir / folder
        # 1. 同步到发布根目录 (支持外部挂载优先级)
        dst_path = dist_app_dir / folder
        if src_path.exists():
            shutil.copytree(src_path, dst_path, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            if verbose:
                print(f"  √ 已同步目录: {folder} -> {dst_path}")

        # 2. 同步到 PyInstaller _internal 核心目录 (杜绝 EXE 内部读取旧版本缓存)
        if src_path.exists() and internal_dir.exists():
            dst_internal = internal_dir / folder
            shutil.copytree(src_path, dst_internal, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            if verbose:
                print(f"  √ 已同步内部目录: {folder} -> {dst_internal}")

    # 同步说明文件
    for name in ("使用说明.txt", "更新说明.txt"):
        readme = root_dir / name
        if readme.exists():
            shutil.copy2(readme, dist_app_dir / name)

    if verbose:
        print("√ 软件发布包 (dist/FGO-MAA) 资产与源码热同步完成！")

if __name__ == "__main__":
    print("=" * 60)
    print("   正在将最新变更热同步至 FGO-MAA 软件发布包...")
    print("=" * 60)
    sync(verbose=True)
    print("=" * 60)
