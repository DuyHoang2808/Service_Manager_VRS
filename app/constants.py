"""Hang so dung chung: co tao process Windows, trang thai module, dinh dang thoi gian."""

from __future__ import annotations

# Co Windows: tao process group rieng (de gui CTRL_BREAK) va khong hien console con
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# --- Trang thai module -------------------------------------------------
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
CRASHED = "crashed"
FINISHED = "finished"

STATE_TEXT = {
    STOPPED: "Da dung",
    STARTING: "Dang khoi dong...",
    RUNNING: "Dang chay",
    STOPPING: "Dang dung...",
    CRASHED: "Loi / thoat bat thuong",
    FINISHED: "Da xong",
}

STATE_COLOR = {
    STOPPED: "#6b7280",
    STARTING: "#f59e0b",
    RUNNING: "#22c55e",
    STOPPING: "#f59e0b",
    CRASHED: "#ef4444",
    FINISHED: "#3b82f6",
}


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
