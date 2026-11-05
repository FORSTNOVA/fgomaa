"""原子替换文件；Windows 临时文件占用只做有限重试，不丢弃写入错误。"""
import time
from pathlib import Path


def replace_file(source, target):
    for attempt in range(6):
        try:
            Path(source).replace(target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.04*(attempt+1))
