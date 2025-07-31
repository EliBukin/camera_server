import signal
import sys

from . import backend
from .gui import app

signal.signal(signal.SIGINT, backend.cleanup_handler)
signal.signal(signal.SIGTERM, backend.cleanup_handler)

if __name__ == "__main__":
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not backend.initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host='0.0.0.0', port=5000, threaded=True)

