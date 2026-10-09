"""Choose a bounded transfer window from available RAM and temp disk space."""
from dataclasses import dataclass
import shutil
import tempfile

import psutil


@dataclass(frozen=True)
class BufferPlan:
    workers: int
    slots: int
    memory_available: int
    disk_available: int


def buffer_plan(requested, count, chunk_size):
    memory = psutil.virtual_memory().available
    disk = shutil.disk_usage(tempfile.gettempdir()).free
    # Leave room for Python, encryption, HTTP copies and other machine workloads.
    memory_budget = min(512 * 1024**2, max(0, memory - 256 * 1024**2) // 5)
    disk_budget = min(1024**3, max(0, disk - 512 * 1024**2) // 10)
    memory_workers = memory_budget // (2 * chunk_size) - 1
    slots = min(count, disk_budget // chunk_size)
    if memory_workers < 1 or slots < 1:
        raise RuntimeError('可用内存或临时磁盘不足，请释放资源后重试')
    return BufferPlan(min(requested, count, memory_workers, slots), slots, memory, disk)
