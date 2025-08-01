import os
import cv2
import time
import threading

try:
    # When executed as part of the package
    from ..utils.config import load_config
except Exception:  # pragma: no cover - fallback for direct execution
    # Fallback if the package layout isn't available
    from app.utils.config import load_config

class TimeLapseCapturer:
    def __init__(self, camera, output_dir="timelapse"):
        self.camera = camera
        self.output_dir = output_dir
        self.interval = 5
        self.running = False
        self.thread = None
        self.prev_resolution = None
        self.prev_format = None
        self.prev_controls = None
        os.makedirs(self.output_dir, exist_ok=True)

    def start(self, interval):
        self.interval = interval
        if not self.running:
            # Load latest config each start
            cfg = load_config()

            # Update output directory from config
            self.output_dir = cfg.get("image_output", self.output_dir)
            os.makedirs(self.output_dir, exist_ok=True)

            # Preserve current preview settings
            self.prev_resolution = self.camera.get_current_resolution()
            self.prev_format = getattr(self.camera, "current_format", "MJPG")
            self.prev_controls = self.camera.get_all_current_values()

            # Apply configured resolution
            res = cfg.get("resolution", {})
            w = res.get("width", self.prev_resolution[0])
            h = res.get("height", self.prev_resolution[1])
            fmt = res.get("format", self.prev_format)
            self.camera.set_resolution(w, h, fmt)

            # Apply configured controls
            for ctrl, value in cfg.get("controls", {}).items():
                self.camera.set_control_value(ctrl, value)

            self.running = True
            self.thread = threading.Thread(target=self.capture_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        # Restore previous settings after timelapse ends
        if self.prev_resolution:
            w, h = self.prev_resolution
            self.camera.set_resolution(w, h, self.prev_format)
        if self.prev_controls:
            for ctrl, value in self.prev_controls.items():
                self.camera.set_control_value(ctrl, value)

    def capture_loop(self):
        count = 0
        while self.running:
            with self.camera.camera_lock:
                ret, frame = self.camera.cap.read()
            if ret and frame is not None:
                filename = os.path.join(self.output_dir, f"frame_{count:05d}.jpg")
                cv2.imwrite(filename, frame)
                print(f"[TimeLapse] Captured: {filename}")
                count += 1
            time.sleep(self.interval)

