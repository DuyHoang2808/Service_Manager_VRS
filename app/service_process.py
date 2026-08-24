"""Boc mot subprocess: khoi dong, doc log realtime, ghi log ra file, dung an toan."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from .constants import (
    CRASHED,
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    FINISHED,
    RUNNING,
    STARTING,
    STOPPED,
    STOPPING,
)


class ServiceProcess(QObject):
    """Boc mot subprocess: khoi dong, doc log realtime, dung an toan."""

    log_line = Signal(str, str)      # (ten module, dong log)
    state_changed = Signal(str)      # (ten module)

    def __init__(self, cfg: Dict[str, Any], python_exe: str, log_dir: Path,
                 max_lines: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.name: str = cfg["name"]
        self.label: str = cfg.get("label", self.name)
        self.kind: str = cfg.get("type", "service")
        self.python_exe = python_exe
        # "exe": duong dan file .exe da build san (PyInstaller) - neu co, chay
        # thang exe nay, khong qua python_exe nua. "script" van khai bao de
        # doc/debug, nhung bi bo qua khi da co "exe".
        self.exe: Optional[str] = str(cfg["exe"]) if cfg.get("exe") else None
        self.script = str(cfg.get("script") or "")
        self.cwd = str(cfg.get("cwd") or Path(self.exe or self.script).parent)
        self.args: str = str(cfg.get("args", "") or "")
        self.health_url: Optional[str] = cfg.get("health_url")
        self.note: str = cfg.get("note", "")

        self.proc: Optional[subprocess.Popen] = None
        self.state = STOPPED
        self.exit_code: Optional[int] = None
        self.started_at: float = 0.0
        self.restart_count = 0
        self.healthy: Optional[bool] = None
        self.auto_restart = False

        self._manual_stop = False
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

        # Log chia thu muc: logs/<service>/<nam>/<thang>/<ngay>/<service>_yyyy_mm_dd.log
        # Ten file co san ngay -> nhieu lan bat lai trong cung ngay ghi noi vao
        # cung 1 file, va file van tu nhan dien duoc khi copy di noi khac.
        self.log_root = log_dir
        self._log_fh = None                       # file dang mo
        self._log_day = None                      # ngay cua file dang mo
        self.log_path = self.log_path_for(datetime.now())

    # --- tien ich -----------------------------------------------------
    @property
    def is_tool(self) -> bool:
        return self.kind == "tool"

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def buffered_lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(self.name)

    # --- ghi log ra file -------------------------------------------------
    def log_path_for(self, moment: datetime) -> Path:
        """logs/<service>/<nam>/<thang>/<ngay>/<service>_yyyy_mm_dd.log"""
        return (self.log_root / self.name / f"{moment:%Y}" / f"{moment:%m}"
                / f"{moment:%d}" / f"{self.name}_{moment:%Y_%m_%d}.log")

    def _write_file(self, line: str, moment: datetime) -> None:
        """Ghi 1 dong, tu chuyen sang file cua ngay moi khi qua nua dem.

        Goi trong self._lock nen khong lo 2 luong ghi chen nhau.
        """
        day = moment.date()
        if self._log_fh is None or self._log_day != day:
            if self._log_fh is not None:
                try:
                    self._log_fh.write(f"[{moment:%H:%M:%S}] ### Sang ngay moi -> chuyen sang "
                                       f"{self.log_path_for(moment).name}\n")
                    self._log_fh.close()
                except OSError:
                    pass
            path = self.log_path_for(moment)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(path, "a", encoding="utf-8", errors="replace")
            self._log_day = day
            self.log_path = path
        self._log_fh.write(line + "\n")
        self._log_fh.flush()

    def close_log(self) -> None:
        with self._lock:
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except OSError:
                    pass
                self._log_fh = None

    def _emit(self, text: str, to_file: bool = True) -> None:
        moment = datetime.now()
        line = f"[{moment:%H:%M:%S}] {text}"
        with self._lock:
            self._lines.append(line)
            if to_file:
                try:
                    self._write_file(line, moment)
                except OSError:
                    pass
        self.log_line.emit(self.name, line)

    # --- vong doi -----------------------------------------------------
    def start(self, args_override: Optional[str] = None) -> None:
        if self.is_alive:
            return

        target = self.exe or self.script
        if not target or not Path(target).exists():
            self._emit(f"### KHONG TIM THAY FILE: {target}")
            self._set_state(CRASHED)
            return

        args = args_override if args_override is not None else self.args
        extra_args = args.split() if args.strip() else []
        if self.exe:
            cmd = [self.exe] + extra_args
        else:
            cmd = [self.python_exe, "-u", self.script] + extra_args

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Bat buoc: script con in emoji (rocket, tick...), neu khong ep UTF-8 thi
        # print() se nem UnicodeEncodeError khi stdout bi redirect vao pipe.
        env["PYTHONIOENCODING"] = "utf-8"
        for key, value in (self.cfg.get("env") or {}).items():
            env[str(key)] = str(value)

        self._manual_stop = False
        self.exit_code = None
        self.healthy = None
        self._set_state(STARTING)
        # Moc phan tach phien: trong 1 file ngay co the co nhieu lan bat lai
        self._emit("=" * 78)
        self._emit(f"### PHIEN MOI luc {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._emit(f"### KHOI DONG: {' '.join(cmd)}")
        self._emit(f"### Thu muc lam viec: {self.cwd}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # gop stderr vao stdout de giu dung thu tu
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                bufsize=0,
            )
        except OSError as exc:
            self._emit(f"### KHONG KHOI DONG DUOC: {exc}")
            self.proc = None
            self._set_state(CRASHED)
            return

        self.started_at = time.time()
        self._emit(f"### PID = {self.proc.pid}")
        self._set_state(RUNNING)

        threading.Thread(target=self._pump_output, args=(self.proc,),
                         daemon=True, name=f"log-{self.name}").start()
        threading.Thread(target=self._wait_exit, args=(self.proc,),
                         daemon=True, name=f"wait-{self.name}").start()

    def _pump_output(self, proc: subprocess.Popen) -> None:
        """Doc stdout tung dong, day len GUI ngay lap tuc."""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    self._emit(text)
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait_exit(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        if proc is not self.proc:      # da bi thay bang lan chay moi
            return
        self.exit_code = code
        self.healthy = None      # tranh giu lai ket qua health cu sau khi da dung

        if self._manual_stop:
            self._emit(f"### DA DUNG (exit code {code})")
            self._set_state(STOPPED)
        elif self.is_tool and code == 0:
            self._emit("### HOAN TAT (exit code 0)")
            self._set_state(FINISHED)
        elif code == 0:
            self._emit("### TIEN TRINH TU THOAT (exit code 0)")
            self._set_state(STOPPED)
        else:
            self._emit(f"### THOAT BAT THUONG (exit code {code})")
            self._set_state(CRASHED)

    def stop(self, timeout: float = 6.0) -> None:
        """Dung nhe nhang truoc (CTRL_BREAK), khong duoc thi kill ca cay process.

        Kill theo cay (taskkill /T) la bat buoc: Stream camera sinh process con
        ffmpeg, kill moi python se de lai ffmpeg treo va giu camera.
        """
        if not self.is_alive:
            return
        proc = self.proc
        assert proc is not None

        self._manual_stop = True
        self._set_state(STOPPING)
        self._emit("### Dang gui tin hieu dung...")

        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError, AttributeError):
            pass

        def finisher() -> None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._emit("### Khong tu thoat - buoc dung ca cay process (taskkill /T /F)")
                self._kill_tree(proc.pid)
            else:
                # Van quet cay process phong khi con ffmpeg/uvicorn worker sot lai
                self._kill_tree(proc.pid, quiet=True)

        threading.Thread(target=finisher, daemon=True, name=f"stop-{self.name}").start()

    def _kill_tree(self, pid: int, quiet: bool = False) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if not quiet:
                self._emit(f"### Loi khi taskkill: {exc}")

    # --- health check --------------------------------------------------
    def check_health(self) -> None:
        if not self.health_url or not self.is_alive:
            self.healthy = None
            return
        # BAT BUOC doc het body truoc khi dong ket noi. Neu con du lieu chua doc
        # trong buffer nhan, Windows dong socket bang RST thay vi FIN -> phia
        # uvicorn nem "ConnectionResetError: [WinError 10054]" luc don dep
        # transport, lam ban log cua service (do da: ~1 loi / 18 lan goi).
        try:
            with urllib.request.urlopen(self.health_url, timeout=2) as resp:
                resp.read()
                self.healthy = 200 <= resp.status < 500
        except urllib.error.HTTPError as exc:
            # 404 van chung to co server dang tra loi
            try:
                exc.read()
            except Exception:
                pass
            self.healthy = exc.code < 500
        except Exception:
            self.healthy = False
