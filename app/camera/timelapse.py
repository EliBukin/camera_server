import os
import subprocess

try:
    # When imported via the ``app`` package (e.g. ``python -m app.app``)
    from ..utils.config import load_config
except (ImportError, ValueError):  # pragma: no cover - fallback for direct execution
    # When executed directly with ``python app/app.py`` the ``camera`` package
    # is top-level, so we import from the sibling ``utils`` package instead.
    from utils.config import load_config

class TimeLapseCapturer:
    def __init__(self, camera, output_dir="timelapse"):
        self.camera = camera
        self.output_dir = output_dir
        self.interval = 5
        self.running = False
        self.process = None
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
            self._start_ffmpeg_capture(w, h, fmt)

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        # Restore previous settings after timelapse ends
        if self.prev_resolution:
            w, h = self.prev_resolution
            self.camera.set_resolution(w, h, self.prev_format)
        if self.prev_controls:
            for ctrl, value in self.prev_controls.items():
                self.camera.set_control_value(ctrl, value)


    def _start_ffmpeg_capture(self, width, height, fmt):
        device = self.camera.device_path
        output = os.path.join(self.output_dir, "frame_%05d.jpg")
        fps_filter = f"fps=1/{self.interval}"
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "v4l2",
            "-video_size",
            f"{width}x{height}",
            "-input_format",
            fmt,
            "-i",
            device,
            "-vf",
            fps_filter,
            output,
        ]
        self.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

