import signal
import sys

try:
    from . import backend  # type: ignore
    from .gui import app  # type: ignore
except ImportError:  # Fallback when run as a script
    import backend  # type: ignore
    from gui import app  # type: ignore

signal.signal(signal.SIGINT, backend.cleanup_handler)
signal.signal(signal.SIGTERM, backend.cleanup_handler)

if __name__ == "__main__":
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not backend.initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host='0.0.0.0', port=5000, threaded=True)

