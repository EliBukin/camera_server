from . import create_app
from .camera import initialize_camera, camera
import signal
import sys

app = create_app()


def cleanup_handler(signum, frame):
    if camera:
        if hasattr(camera, "timelapse"):
            camera.timelapse.stop()
        if hasattr(camera, "recorder"):
            camera.recorder.stop()
        camera.cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup_handler)
signal.signal(signal.SIGTERM, cleanup_handler)

if __name__ == "__main__":
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host="0.0.0.0", port=5000, threaded=True)
