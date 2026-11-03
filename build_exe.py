"""
Build Script - 一键将 FGO MAA 编译打包为 Windows 原生独立 EXE 包体
生成免安装绿色便携包，双击 FGO-MAA.exe 直接弹出 MAA 风格桌面客户端原生窗口！
"""

import sys
import shutil
import subprocess
from pathlib import Path
import PyInstaller.__main__

root_dir = Path(__file__).resolve().parent

def build():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "FGO-MAA.exe"], capture_output=True)
    except Exception:
        pass
    print("=" * 65)
    print("   正在将 FGO MAA 打包为 Windows 原生 EXE 客户端发布包...")
    print("=" * 65)

    dist_dir = root_dir / "dist"
    build_dir = root_dir / "build"
    temp_dist = build_dir / "pyi_dist"

    args = [
        str(root_dir / "app.py"),
        "--name=FGO-MAA",
        "--onedir",                  # 绿色目录模式 (MAA 官方同款发布结构)
        "--noconsole",               # 纯净窗口模式，不弹出黑色控制台黑框
        "--clean",
        f"--add-data={root_dir / 'data'};data",
        f"--add-data={root_dir / 'config'};config",
        f"--add-data={root_dir / 'web'};web",
        "--collect-all=rapidocr_onnxruntime",
        "--collect-all=onnxruntime",
        "--collect-all=yaml",
        "--hidden-import=scipy.optimize",
        "--hidden-import=PIL",
        "--hidden-import=cv2",
        "--hidden-import=yaml",
        "--hidden-import=src.core.controller",
        "--hidden-import=src.core.device_manager",
        "--hidden-import=src.core.path_helper",
        "--hidden-import=src.strategy.anchor_verifier",
        "--hidden-import=src.strategy.team_detector",
        "--hidden-import=src.strategy.battle_plans",
        "--hidden-import=src.strategy.screen_detector",
        "--hidden-import=src.strategy.auto_team_builder",
        "--hidden-import=src.strategy.auto_party_assembler",
        "--hidden-import=src.strategy.smart_engine",
        "--hidden-import=src.parser.chaldea_parser",
        "--hidden-import=src.solver.mission_solver",
        "--hidden-import=src.workflows.weekly_runner",
        "--hidden-import=src.server.web_server",
        f"--distpath={temp_dist}",
        f"--workpath={build_dir / 'pyi_work'}",
        "-y"
    ]

    print("启动 PyInstaller 编译...")
    PyInstaller.__main__.run(args)

    built_source = temp_dist / "FGO-MAA"
    target_app_dir = dist_dir / "FGO-MAA"
    target_app_dir.mkdir(parents=True, exist_ok=True)

    if (built_source / "FGO-MAA.exe").exists():
        print("正在同步更新发布目录文件...")
        # 递归拷贝编译产物到发布根目录 (避免因目录被资源管理器打开导致整个目录删除失败)
        for item in built_source.iterdir():
            dest = target_app_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # 同步拷贝最新的资源与源码文件夹到发布根目录 (支持外部热更新优先挂载)
        for folder in ["data", "config", "web", "src", "runtime"]:
            src_f = root_dir / folder
            dst_f = target_app_dir / folder
            if src_f.exists():
                shutil.copytree(src_f, dst_f, dirs_exist_ok=True)

        # 采用 Windows 系统原生纯净 ucrtbase.dll 覆盖 Conda 的旧版本 DLL，彻底解决 0xc000012f 与 Failed to load Python DLL 问题
        internal_dir = target_app_dir / "_internal"
        sys32_ucrt = Path("C:/Windows/System32/ucrtbase.dll")
        if internal_dir.exists() and sys32_ucrt.exists():
            dest_ucrt = internal_dir / "ucrtbase.dll"
            try:
                shutil.copy2(sys32_ucrt, dest_ucrt)
                print(f"  √ 已采用 Windows 官方原生 UCRT 库覆盖 Conda 旧版 DLL ({sys32_ucrt.stat().st_size} bytes)")
            except Exception as e:
                print(f"  [提示] 同步系统 ucrtbase.dll 失败: {e}")

        # 深度完整性校验：自动检查并修复任何因文件锁定导致的损坏二进制文件
        for f in target_app_dir.rglob("*"):
            if f.suffix.lower() in [".dll", ".exe", ".pyd"]:
                try:
                    with open(f, "rb") as fp:
                        m = fp.read(2)
                    if m != b"MZ":
                        rel = f.relative_to(target_app_dir)
                        src_f = built_source / rel
                        if src_f.exists():
                            shutil.copy2(src_f, f)
                            print(f"  √ 自动修复损坏的二进制文件: {rel}")
                except Exception:
                    pass

        # 写入一键启动说明
        readme_path = target_app_dir / "使用说明.txt"
        readme_path.write_text(
            "FGO MAA 自动化助手客户端\n"
            "====================================\n"
            "1. 双击运行 FGO-MAA.exe 即可启动 MAA 风格桌面客户端。\n"
            "2. 本客户端支持自动连接 MuMu 12 / 雷电模拟器。\n"
            "3. 如遇端口占用或安全软件拦截，请将本目录加入白名单。\n",
            encoding="utf-8"
        )

        print("\n" + "=" * 65)
        print("√ 编译打包大功告成！")
        print(f"发布包体路径: {target_app_dir}")
        print(f"主程序入口: {target_app_dir / 'FGO-MAA.exe'}")
        print("=" * 65)


if __name__ == "__main__":
    build()
