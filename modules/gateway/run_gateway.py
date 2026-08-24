"""Entry point for PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat)."""

import asyncio
import sys
import uvicorn

# Khi dong goi bang PyInstaller, bootloader khong ton trong PYTHONIOENCODING
# nhu python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8 tai code,
# neu khong cac dong print() co emoji se nem UnicodeEncodeError va crash exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

if __name__ == "__main__":
    if sys.platform == "win32":
        # Tranh loi "ConnectionResetError" vo hai tu ProactorEventLoop khi
        # client (vd: health-check cua Service Manager) dong ket noi ngan han.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print("🚀 Starting PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat)...")
    print("📋 Port: 8083")
    print("📚 API Docs: http://localhost:8083/docs")
    print("🔧 Press Ctrl+C to stop")
    print()

    try:
        uvicorn.run(
            "plc_offset_gateway:app",
            host="0.0.0.0",
            port=8083,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n👋 Server stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)
