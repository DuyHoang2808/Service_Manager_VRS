"""Entry point for Fiducial Detector Service (chạy độc lập, port riêng)."""

import asyncio
import sys
import uvicorn

from fiducial_service import SERVICE_PORT

if __name__ == "__main__":
    if sys.platform == "win32":
        # Tranh loi "ConnectionResetError" vo hai tu ProactorEventLoop khi
        # client (vd: health-check cua Service Manager) dong ket noi ngan han.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    port = SERVICE_PORT
    print("🎯 Starting Fiducial Detector Service...")
    print(f"📋 Port: {port}")
    print(f"📚 API Docs: http://localhost:{port}/docs")
    print("🔧 Press Ctrl+C to stop")
    print()

    try:
        uvicorn.run(
            "fiducial_service:app",
            host="0.0.0.0",
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n👋 Service stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)
