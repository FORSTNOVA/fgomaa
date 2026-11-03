"""
Path Helper - 运行时路径解析器
完美适配 Python 脚本运行模式与 PyInstaller 独立 EXE 打包模式。
"""

import sys
import os
from pathlib import Path


def get_root_dir() -> Path:
    """获取项目主根目录 (优先 exe 同级，其次 _MEIPASS，其次源码根目录)"""
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        return exe_dir
    return Path(__file__).resolve().parent.parent.parent


def get_resource_path(sub_path: str) -> Path:
    """
    智能寻找资源路径 (支持相对路径如 'data/free_quests_traits.json' 或 'web/index.html')
    搜索顺序:
      1. exe 同级目录 (例如 dist/FGO-MAA/web/index.html)
      2. PyInstaller 解压目录 sys._MEIPASS (例如 _MEIPASS/web/index.html)
      3. exe 同级下的 _internal 目录 (例如 dist/FGO-MAA/_internal/web/index.html)
      4. 源码项目根目录 (开发模式)
    """
    clean_sub = Path(sub_path)

    # 1. exe 同级目录
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        cand1 = exe_dir / clean_sub
        if cand1.exists():
            return cand1

        # 2. _MEIPASS
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            cand2 = Path(meipass) / clean_sub
            if cand2.exists():
                return cand2

        # 3. _internal
        cand3 = exe_dir / "_internal" / clean_sub
        if cand3.exists():
            return cand3

    # 4. 源码目录
    src_root = Path(__file__).resolve().parent.parent.parent
    cand4 = src_root / clean_sub
    return cand4
