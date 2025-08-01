import cv2
import time
import subprocess
import threading
import queue
import re
import os

from timelapse import TimeLapseCapturer
from recorder import VideoRecorder


def discover_cameras():
    """Return a list of tuples (name, device) for detected cameras"""
    cmd = "v4l2-ctl --list-devices"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    cameras = []
    lines = result.stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line.startswith("\t"):
            name = line.rstrip(":")
            i += 1
            while i < len(lines) and lines[i].startswith("\t"):
                dev = lines[i].strip()
                if dev.startswith("/dev/video"):
                    cap = cv2.VideoCapture(dev)
                    if cap.isOpened():
                        cameras.append((name, dev))
                        cap.release()
                    else:
                        print(f"Skipping {dev} - unable to open")
                    break
                i += 1
        else:
            i += 1

    def dev_index(dev_path):
        m = re.search(r"/dev/video(\d+)", dev_path)
        return int(m.group(1)) if m else 0

    cameras.sort(key=lambda x: dev_index(x[1]))
    return cameras


class ThreadSafeCameraController:
    def __init__(
        self, device="/dev/video0", timelapse_dir="timelapse", video_dir="videos"
    ):
        self.device_path = device
        self.cap = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.capture_thread = None
        self.streaming = False
        self.camera_lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.stored_defaults = {}
        self.original_hardware_defaults = {}

        self._initialize_camera()
        self.start_streaming()
        self.timelapse = TimeLapseCapturer(self, timelapse_dir)
        self.recorder = VideoRecorder(self, video_dir)

    def start_streaming(self):
        if not self.streaming:
            self.streaming = True
            self.capture_thread = threading.Thread(
                target=self._capture_loop, daemon=True
            )
            self.capture_thread.start()

    def stop_streaming(self):
        self.streaming = False
        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=2)

    def _capture_loop(self):
        failures = 0
        while self.streaming:
            with self.camera_lock:
                if not self.cap or not self.cap.isOpened():
                    time.sleep(0.1)
                    continue
                ret, frame = self.cap.read()
            if ret and frame is not None:
                ret_encode, jpeg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70]
                )
                if ret_encode:
                    try:
                        self.frame_queue.put(jpeg.tobytes(), block=False)
                    except queue.Full:
                        pass
                else:
                    failures += 1
            else:
                failures += 1
            if failures > 10:
                print("Reinitializing camera...")
                self._initialize_camera()
                failures = 0
            time.sleep(0.033)

    def get_current_resolution(self):
        with self.camera_lock:
            if self.cap is None:
                return 640, 480
            width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return width, height

    def get_latest_frame(self):
        frame = None
        while not self.frame_queue.empty():
            frame = self.frame_queue.get_nowait()
        return frame

    def _get_supported_resolutions(self):
        cmd = f"v4l2-ctl --device={self.device_path} --list-formats-ext"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise RuntimeError(f"v4l2-ctl command failed: {result.stderr}")

        resolutions = []
        fmt = None
        for line in result.stdout.splitlines():
            line = line.strip()
            m_fmt = re.match(r"\[\d+\]: '(\w+)'", line)
            m_size = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
            if m_fmt:
                fmt = m_fmt.group(1)
            elif m_size and fmt:
                w, h = map(int, m_size.groups())
                resolutions.append((fmt, w, h))

        if not resolutions:
            raise RuntimeError("No supported resolutions found")

        return sorted(resolutions, key=lambda x: (x[1], x[2]))

    def _set_camera_properties(self, width, height, fmt="MJPG"):
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fmt))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 15)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def get_camera_controls(self):
        try:
            cmd = f"v4l2-ctl --device={self.device_path} --list-ctrls"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"v4l2-ctl failed: {result.stderr}")
                return {}

            controls = {}
            output = result.stdout
            for line in output.strip().split("\n"):
                if not line.strip() or "Controls" in line or ":" not in line:
                    continue
                try:
                    name_part = line.split(":")[0].strip()
                    name = name_part.split()[0]
                    if "(int)" in line:
                        ctrl_type = "int"
                    elif "(bool)" in line:
                        ctrl_type = "bool"
                    elif "(menu)" in line:
                        ctrl_type = "menu"
                    else:
                        continue

                    default_match = re.search(r"default=(-?\d+)", line)
                    current_match = re.search(r"value=(-?\d+)", line)
                    if not default_match or not current_match:
                        continue
                    default_val = int(default_match.group(1))
                    current_val = int(current_match.group(1))

                    if ctrl_type == "int":
                        min_match = re.search(r"min=(-?\d+)", line)
                        max_match = re.search(r"max=(-?\d+)", line)
                        step_match = re.search(r"step=(-?\d+)", line)
                        if not all([min_match, max_match, step_match]):
                            continue
                        controls[name] = {
                            "type": ctrl_type,
                            "min": int(min_match.group(1)),
                            "max": int(max_match.group(1)),
                            "step": int(step_match.group(1)),
                            "default": default_val,
                            "current": current_val,
                        }
                    elif ctrl_type == "bool":
                        controls[name] = {
                            "type": ctrl_type,
                            "min": 0,
                            "max": 1,
                            "step": 1,
                            "default": default_val,
                            "current": current_val,
                        }
                    elif ctrl_type == "menu":
                        min_match = re.search(r"min=(-?\d+)", line)
                        max_match = re.search(r"max=(-?\d+)", line)
                        if not all([min_match, max_match]):
                            continue
                        controls[name] = {
                            "type": ctrl_type,
                            "min": int(min_match.group(1)),
                            "max": int(max_match.group(1)),
                            "step": 1,
                            "default": default_val,
                            "current": current_val,
                        }
                except Exception as e:
                    print(f"Error parsing control line '{line}': {e}")
                    continue
            print(f"Found {len(controls)} camera controls")
            return controls
        except Exception as e:
            print(f"Control parse error: {e}")
            return {}

    def calculate_default_values(self):
        defaults = {}
        for name, ctrl in self.controls_info.items():
            if ctrl["type"] == "int":
                defaults[name] = (ctrl["min"] + ctrl["max"]) // 2
            elif ctrl["type"] == "bool":
                defaults[name] = ctrl["min"]
            elif ctrl["type"] == "menu":
                if name == "auto_exposure" and ctrl["max"] >= 1:
                    defaults[name] = 1
                else:
                    defaults[name] = ctrl["min"]
        return defaults

    def set_control_value(self, control_name, value):
        try:
            if control_name in self.controls_info:
                ctrl = self.controls_info[control_name]
                if value < ctrl["min"] or value > ctrl["max"]:
                    print(
                        f"WARNING: {control_name} value {value} is out of bounds [{ctrl['min']}-{ctrl['max']}], skipping"
                    )
                    return False
            cmd = f"v4l2-ctl --device={self.device_path} --set-ctrl={control_name}={value}"
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                print(f"Set {control_name} = {value}")
                if control_name in self.controls_info:
                    self.controls_info[control_name]["current"] = value
                return True
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                print(f"Failed to set {control_name}: {error_msg}")
                return False
        except Exception as e:
            print(f"Error setting {control_name}: {e}")
            return False

    def set_default_values(self):
        print("Setting camera controls to default values...")
        default_values = self.calculate_default_values()
        failed_controls = []
        for control_name, value in default_values.items():
            if control_name in ["auto_exposure", "exposure_time_absolute"]:
                continue
            success = self.set_control_value(control_name, value)
            if not success:
                failed_controls.append(control_name)
        if "auto_exposure" in default_values:
            manual_mode = 1
            success = self.set_control_value("auto_exposure", manual_mode)
            if success:
                self.stored_defaults["auto_exposure"] = manual_mode
                if "exposure_time_absolute" in default_values:
                    success = self.set_control_value(
                        "exposure_time_absolute",
                        default_values["exposure_time_absolute"],
                    )
                    if not success:
                        failed_controls.append("exposure_time_absolute")
            else:
                failed_controls.append("auto_exposure")

        self.controls_info = self.get_camera_controls()
        for control_name, calculated_default in default_values.items():
            if control_name in self.controls_info:
                working_default = self.stored_defaults.get(
                    control_name, calculated_default
                )
                self.controls_info[control_name]["default"] = working_default

        self.current_values = self.get_all_current_values()
        if failed_controls:
            print(
                f"WARNING: Failed to set {len(failed_controls)} controls: {failed_controls}"
            )
        print(
            f"Applied defaults to {len(default_values) - len(failed_controls)} of {len(default_values)} controls"
        )

    def reset_to_stored_defaults(self):
        print("Resetting controls to stored defaults...")
        print(f"Using stored default values: {self.stored_defaults}")
        for control_name, value in self.stored_defaults.items():
            self.set_control_value(control_name, value)
        print("Reinitializing camera after reset...")
        self._initialize_camera()
        print("Reset to stored defaults completed")

    def _initialize_camera(self):
        with self.camera_lock:
            if self.cap:
                self.cap.release()
                time.sleep(0.3)
            self.cap = cv2.VideoCapture(self.device_path)
            if not self.cap.isOpened():
                raise RuntimeError(f"Could not open camera at {self.device_path}")
            self.supported_resolutions = self._get_supported_resolutions()
            self.controls_info = self.get_camera_controls()
            if (
                not hasattr(self, "original_hardware_defaults")
                or not self.original_hardware_defaults
            ):
                self.original_hardware_defaults = {}
                for name, ctrl in self.controls_info.items():
                    self.original_hardware_defaults[name] = ctrl["default"]
                print(
                    f"Original hardware defaults captured for {len(self.original_hardware_defaults)} controls"
                )
            calculated_defaults = self.calculate_default_values()
            if not hasattr(self, "stored_defaults") or not self.stored_defaults:
                self.stored_defaults = calculated_defaults.copy()
                print(f"Stored calculated defaults: {self.stored_defaults}")
            self.set_default_values()
            fmt, w, h = self.supported_resolutions[0]
            self._set_camera_properties(w, h, fmt)

    def get_all_current_values(self):
        return {k: v["current"] for k, v in self.controls_info.items()}

    def set_resolution(self, width, height, fmt="MJPG"):
        with self.camera_lock:
            try:
                self._set_camera_properties(width, height, fmt)
                while not self.frame_queue.empty():
                    self.frame_queue.get_nowait()
                return True, f"Resolution set to {width}x{height} ({fmt})"
            except Exception as e:
                return False, str(e)

    def cleanup(self):
        self.stop_streaming()
        if hasattr(self, "timelapse"):
            self.timelapse.stop()
        if hasattr(self, "recorder"):
            self.recorder.stop()
        with self.camera_lock:
            if self.cap:
                self.cap.release()

    def capture_photo(self, filepath):
        """Capture a single image and save it to ``filepath``."""
        dirpath = os.path.dirname(filepath)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with self.camera_lock:
            if not self.cap or not self.cap.isOpened():
                raise RuntimeError("Camera is not opened")
            ret, frame = self.cap.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to capture frame")
        cv2.imwrite(filepath, frame)
        return filepath
