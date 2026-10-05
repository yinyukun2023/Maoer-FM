"""A serial download queue whose paused jobs yield their slot without losing data."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import threading
from typing import Callable

from downloads import DownloadCancelled, DownloadControl


class TaskDownloadControl(DownloadControl):
    def __init__(self, batch: DownloadBatch, index: int) -> None:
        super().__init__()
        self.batch = batch
        self.index = index
        self.started = self.done = False

    def set(self) -> None:
        with self.batch.condition:
            super().set()
            self.batch.condition.notify_all()

    def pause(self) -> None:
        with self.batch.condition:
            super().pause()
            self.batch.condition.notify_all()

    def resume(self) -> None:
        with self.batch.condition:
            super().resume()
            self.batch.condition.notify_all()

    def is_set(self) -> bool:
        return super().is_set() or self.batch.is_set()

    def checkpoint(self) -> None:
        batch = self.batch
        with batch.condition:
            while True:
                if self.is_set():
                    if batch.active == self.index:
                        batch.active = None
                        batch.condition.notify_all()
                    raise DownloadCancelled()
                if self.is_paused() or batch.is_paused():
                    if batch.active == self.index:
                        batch.active = None
                        batch.condition.notify_all()
                elif batch.active == self.index:
                    return
                batch.condition.wait(0.1)


class DownloadBatch(DownloadControl):
    def __init__(self, count: int) -> None:
        super().__init__()
        self.condition = threading.Condition(threading.RLock())
        self.active: int | None = None
        self.controls = [TaskDownloadControl(self, index) for index in range(count)]

    def set(self) -> None:
        with self.condition:
            super().set()
            self.condition.notify_all()

    def pause(self) -> None:
        with self.condition:
            super().pause()
            self.condition.notify_all()

    def resume(self) -> None:
        with self.condition:
            super().resume()
            self.condition.notify_all()

    def retry(self, indices: list[int]) -> None:
        """Requeue jobs whose failure was reported, including before worker exit."""
        with self.condition:
            for index in indices:
                self.controls[index] = TaskDownloadControl(self, index)
            self.condition.notify_all()

    def run(self, work: Callable, result: Callable) -> None:
        """Run work(index, control); report result(index, value, error) exactly once."""
        threads = []

        def execute(control):
            value = error = None
            try:
                control.checkpoint()
                value = work(control.index, control)
            except Exception as exc:
                error = exc
            finally:
                try:
                    # Publish output ownership before releasing the slot/ending.
                    result(control.index, value, error)
                finally:
                    with self.condition:
                        control.done = True
                        if self.active == control.index:
                            self.active = None
                        self.condition.notify_all()

        while True:
            with self.condition:
                for control in self.controls:
                    if not control.started and not control.done and control.is_set():
                        result(control.index, None, DownloadCancelled())
                        control.done = True
                if all(control.done for control in self.controls):
                    break
                if self.active is None and not self.is_set() and not self.is_paused():
                    ready = next((control for control in self.controls
                                  if not control.done and not control.is_set() and not control.is_paused()), None)
                    if ready is not None:
                        self.active = ready.index
                        if not ready.started:
                            ready.started = True
                            worker = threading.Thread(target=execute, args=(ready,), daemon=True)
                            threads.append(worker)
                            worker.start()
                        self.condition.notify_all()
                self.condition.wait(0.1)
        for worker in threads:
            worker.join()


@dataclass(frozen=True)
class CreatedDownload:
    """An exact output owned by this batch, never a directory-wide deletion."""
    path: Path
    folder: Path
    identity: tuple[int, int, int, int]

    @classmethod
    def capture(cls, path: Path, folder: Path) -> CreatedDownload:
        folder = folder.resolve(strict=True)
        path = Path(path).absolute()
        resolved = path.resolve(strict=True)
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or resolved.parent != folder:
            raise OSError('下载文件不在本次任务目录中')
        return cls(path, folder, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))

    def remove(self) -> None:
        """Remove only the same unchanged output, refusing replaced or moved paths."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (self.path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or self.path.resolve(strict=True).parent != self.folder
                or self.folder.resolve(strict=True) != self.folder
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != self.identity):
            raise OSError('文件位置或内容已改变，为避免误删已保留')
        self.path.unlink()
