#!/usr/bin/env python3
from flask import Flask, Response, request, jsonify, render_template_string
import cv2
import time
import subprocess
import threading
import queue
import signal
import sys
import json
import re
import os
import logging
from logging.handlers import RotatingFileHandler

# ==================== Flask App ====================
app = Flask(__name__)

# ==================== Logging ======================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.getenv("LOG_FILE", "camera_server.log")

logger = logging.getLogger("camera_server")
logger.setLevel(LOG_LEVEL)

# File handler with rotation (~5MB x 5 files)
fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
fh.setLevel(LOG_LEVEL)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(LOG_LEVEL)

fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s")
fh.setFormatter(fmt)
ch.setFormatter(fmt)

if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)

# Make Flask use the same handlers/level
app.logger.handlers = logger.handlers
app.logger.setLevel(LOG_LEVEL)

# ==================== Camera Detection ====================
def detect_available_cameras():
    """Detect all available video devices - improved detection"""
    cameras = []
    
    logger.info("Starting camera detection...")
    
    # Method 1: Check /dev/video* devices
    for i in range(20):  # Check up to 20 devices
        device_path = f"/dev/video{i}"
        if os.path.exists(device_path):
            logger.debug("Found device: %s", device_path)
            
            # Try to get device info using v4l2-ctl
            try:
                cmd = f"v4l2-ctl --device={device_path} --info"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
                
                # Extract device name from output
                name = f"Camera {i}"
                driver = "unknown"
                
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "Card type" in line:
                            name = line.split(":", 1)[1].strip()
                        elif "Driver name" in line:
                            driver = line.split(":", 1)[1].strip()
                    
                    # Check if it's a real video capture device (not metadata)
                    cmd_caps = f"v4l2-ctl --device={device_path} --list-formats"
                    result_caps = subprocess.run(cmd_caps, shell=True, capture_output=True, text=True, timeout=2)
                    
                    # Skip if no video formats (might be metadata device)
                    if result_caps.returncode != 0 or "Invalid" in result_caps.stderr:
                        logger.debug("Skipping %s - not a capture device", device_path)
                        continue
                    
                    # Try to open with OpenCV to verify it's a valid camera
                    logger.debug("Testing %s with OpenCV...", device_path)
                    test_cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
                    if test_cap.isOpened():
                        # Try to read a frame to really verify
                        ret, frame = test_cap.read()
                        test_cap.release()
                        
                        if ret:
                            cameras.append({
                                "device": device_path,
                                "name": name,
                                "driver": driver,
                                "index": i
                            })
                            logger.info("Found working camera: %s at %s (driver: %s)", name, device_path, driver)
                        else:
                            logger.debug("Device %s opened but couldn't read frame", device_path)
                    else:
                        logger.debug("Device %s couldn't be opened with OpenCV", device_path)
                else:
                    logger.debug("v4l2-ctl failed for %s", device_path)
                    
            except Exception as e:
                logger.debug("Error checking %s: %s", device_path, e)
    
    # Method 2: Also check using v4l2-ctl --list-devices
    try:
        cmd = "v4l2-ctl --list-devices"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            logger.debug("v4l2-ctl --list-devices output:\n%s", result.stdout)
    except Exception as e:
        logger.debug("Couldn't run v4l2-ctl --list-devices: %s", e)
    
    if not cameras:
        logger.warning("No cameras detected! Check if cameras are connected and drivers are loaded.")
        logger.info("Try running 'v4l2-ctl --list-devices' in terminal to see available devices")
    else:
        logger.info("Detected %d working camera(s)", len(cameras))
    
    return cameras

# ==================== Camera Controller ====================
camera = None
available_cameras = []
current_camera_device = None

class ThreadSafeCameraController:
    def __init__(self, device="/dev/video0"):
        self.device_path = device
        self.cap = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.capture_thread = None
        self.streaming = False
        self.stop_event = threading.Event()
        self.camera_lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.stored_defaults = {}
        self.original_hardware_defaults = {}
        self.supported_resolutions = []
        self.controls_info = {}
        self.current_values = {}
        self.supports_manual_exposure = None
        self.manual_exposure_value = 1  # Default value for manual mode

        logger.info("Initializing camera controller for device %s", self.device_path)
        self._initialize_camera(apply_defaults=True, prefer_stored=True)
        self.start_streaming()

    def check_auto_exposure_support(self):
        """Check if camera supports auto_exposure control and what values it accepts"""
        if 'auto_exposure' not in self.controls_info:
            logger.warning("Camera %s does not have auto_exposure control", self.device_path)
            self.supports_manual_exposure = False
            return False
        
        # Try to get current auto_exposure value
        current_val = self.verify_control_value('auto_exposure')
        logger.info("Current auto_exposure value: %s", current_val)
        
        # Try different common values for manual mode
        manual_values = [1, 0, 3]  # 1=manual (common), 0=manual (some cams), 3=manual (V4L2 standard)
        
        for test_val in manual_values:
            cmd = f"v4l2-ctl --device={self.device_path} --set-ctrl=auto_exposure={test_val}"
            result = self._run_cmd(cmd, timeout=5)
            
            if result.returncode == 0:
                # Verify it was set
                actual = self.verify_control_value('auto_exposure')
                if actual == test_val:
                    logger.info("Camera supports auto_exposure=%d for manual mode", test_val)
                    self.manual_exposure_value = test_val
                    self.supports_manual_exposure = True
                    return True
        
        # If we get here, camera doesn't support manual exposure mode
        logger.warning("Camera %s does not support manual exposure mode", self.device_path)
        self.supports_manual_exposure = False
        return False

    def start_streaming(self):
        if not self.streaming:
            logger.info("Starting capture thread")
            self.stop_event.clear()
            self.streaming = True
            self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="capture-thread")
            self.capture_thread.start()

    def stop_streaming(self):
        if self.streaming:
            logger.info("Stopping capture thread")
            self.streaming = False
            self.stop_event.set()
            if self.capture_thread and self.capture_thread.is_alive():
                self.capture_thread.join(timeout=2)
                logger.debug("Capture thread joined")

    def _capture_loop(self):
        try:
            failures = 0
            while not self.stop_event.is_set():
                with self.camera_lock:
                    if not self.cap or not self.cap.isOpened():
                        time.sleep(0.05)
                        continue
                    ret, frame = self.cap.read()
                if ret and frame is not None:
                    ret_encode, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
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
                    logger.warning("Too many frame capture failures; reinitializing camera")
                    time.sleep(0.2)
                    self._initialize_camera(apply_defaults=False)
                    if self.stored_defaults:
                        self.apply_values(self.stored_defaults)
                    failures = 0

                time.sleep(0.033)
        except Exception:
            logger.exception("Unhandled exception in capture loop")

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

    def _run_cmd(self, cmd, timeout=10):
        logger.debug("Running: %s", cmd)
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        logger.debug("Exit=%s, stdout=%dB, stderr=%dB",
                     result.returncode, len(result.stdout or ""), len(result.stderr or ""))
        return result

    def _get_supported_resolutions(self):
        cmd = f"v4l2-ctl --device={self.device_path} --list-formats-ext"
        result = self._run_cmd(cmd, timeout=10)

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

        logger.info("Detected %d supported resolutions", len(resolutions))
        return sorted(resolutions, key=lambda x: (x[1], x[2]))

    def _set_camera_properties(self, width, height, fmt='MJPG'):
        logger.debug("Setting camera properties: fmt=%s, %dx%d", fmt, width, height)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fmt))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 15)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        time.sleep(0.05)

    def get_camera_controls(self):
        try:
            cmd = f"v4l2-ctl --device={self.device_path} --list-ctrls"
            result = self._run_cmd(cmd, timeout=10)

            if result.returncode != 0:
                logger.error("v4l2-ctl --list-ctrls failed: %s", result.stderr.strip())
                return {}

            controls = {}
            output = result.stdout

            for line in output.strip().split('\n'):
                if not line.strip() or 'Controls' in line or ':' not in line:
                    continue

                try:
                    name_part = line.split(':')[0].strip()
                    name = name_part.split()[0]

                    if '(int)' in line:
                        ctrl_type = 'int'
                    elif '(bool)' in line:
                        ctrl_type = 'bool'
                    elif '(menu)' in line:
                        ctrl_type = 'menu'
                    else:
                        continue

                    default_match = re.search(r'default=(-?\d+)', line)
                    current_match = re.search(r'value=(-?\d+)', line)
                    if not default_match or not current_match:
                        continue

                    default_val = int(default_match.group(1))
                    current_val = int(current_match.group(1))

                    if ctrl_type == 'int':
                        min_match = re.search(r'min=(-?\d+)', line)
                        max_match = re.search(r'max=(-?\d+)', line)
                        step_match = re.search(r'step=(-?\d+)', line)
                        if not all([min_match, max_match, step_match]):
                            continue
                        controls[name] = {
                            'type': ctrl_type,
                            'min': int(min_match.group(1)),
                            'max': int(max_match.group(1)),
                            'step': int(step_match.group(1)),
                            'default': default_val,
                            'current': current_val,
                        }

                    elif ctrl_type == 'bool':
                        controls[name] = {
                            'type': ctrl_type,
                            'min': 0,
                            'max': 1,
                            'step': 1,
                            'default': default_val,
                            'current': current_val,
                        }

                    elif ctrl_type == 'menu':
                        min_match = re.search(r'min=(-?\d+)', line)
                        max_match = re.search(r'max=(-?\d+)', line)
                        if not all([min_match, max_match]):
                            continue
                        controls[name] = {
                            'type': ctrl_type,
                            'min': int(min_match.group(1)),
                            'max': int(max_match.group(1)),
                            'step': 1,
                            'default': default_val,
                            'current': current_val,
                        }

                except Exception:
                    logger.exception("Error parsing control line '%s'", line)
                    continue

            logger.info("Discovered %d camera controls", len(controls))
            return controls

        except Exception:
            logger.exception("Control parse error")
            return {}

    def calculate_default_values(self):
        """Calculate default values - only force manual exposure if supported"""
        defaults = {}
        
        # First check if camera supports manual exposure
        if self.supports_manual_exposure is None:
            self.check_auto_exposure_support()
        
        for name, ctrl in self.controls_info.items():
            if ctrl['type'] == 'int':
                defaults[name] = (ctrl['min'] + ctrl['max']) // 2
            elif ctrl['type'] == 'bool':
                defaults[name] = ctrl['min']
            elif ctrl['type'] == 'menu':
                if name == 'auto_exposure' and self.supports_manual_exposure:
                    defaults[name] = getattr(self, 'manual_exposure_value', 1)
                else:
                    defaults[name] = ctrl['min']
        
        logger.info("Calculated defaults: %s", defaults)
        return defaults

    def verify_control_value(self, control_name):
        """Verify the actual value of a control from the device"""
        try:
            cmd = f"v4l2-ctl --device={self.device_path} --get-ctrl={control_name}"
            result = self._run_cmd(cmd, timeout=5)
            if result.returncode == 0:
                output = result.stdout.strip()
                if ':' in output:
                    raw = output.split(':', 1)[1].strip()
                    m = re.search(r'-?\d+', raw)
                    if m:
                        return int(m.group(0))
        except Exception:
            logger.exception("Error verifying %s", control_name)
        return None

    def ensure_manual_exposure_mode(self):
        """Ensure auto_exposure is set to manual mode if supported"""
        if self.supports_manual_exposure is None:
            # First time - check support
            self.check_auto_exposure_support()

        if not self.supports_manual_exposure:
            logger.debug("Camera doesn't support manual exposure - skipping")
            return True  # Return True to allow other controls to be set

        # Check if already in manual mode
        current_val = self.verify_control_value('auto_exposure')
        if current_val == getattr(self, 'manual_exposure_value', 1):
            logger.info("auto_exposure already in manual mode (%s)", current_val)
            return True

        # Try to set to manual mode
        max_retries = 3
        for attempt in range(max_retries):
            logger.info("Setting auto_exposure to manual mode (attempt %d/%d)", attempt + 1, max_retries)
            cmd = f"v4l2-ctl --device={self.device_path} --set-ctrl=auto_exposure={self.manual_exposure_value}"
            result = self._run_cmd(cmd, timeout=5)

            if result.returncode != 0:
                logger.warning("Failed to set auto_exposure: %s", result.stderr)
                time.sleep(0.2)
                continue

            time.sleep(0.2)

            # Verify it was set
            actual_val = self.verify_control_value('auto_exposure')
            if actual_val == self.manual_exposure_value:
                logger.info("Successfully set auto_exposure to manual mode")
                self.controls_info['auto_exposure']['current'] = self.manual_exposure_value
                return True

        logger.error("Failed to set auto_exposure to manual mode after %d attempts", max_retries)
        return False

    def set_control_value(self, control_name, value):
        """Set a single control value with proper validation"""
        try:
            # Check if this control requires manual exposure mode
            requires_manual = control_name in ['brightness', 'contrast', 'saturation', 'gain', 'exposure_time_absolute']

            if requires_manual and self.supports_manual_exposure:
                # Only try to set manual exposure if camera supports it
                if not self.ensure_manual_exposure_mode():
                    logger.warning("Could not enable manual exposure, but attempting to set %s anyway", control_name)

            # Validate bounds
            if control_name in self.controls_info:
                ctrl = self.controls_info[control_name]
                if value < ctrl['min'] or value > ctrl['max']:
                    logger.warning("%s value %s out of bounds [%s-%s]; clamping",
                                   control_name, value, ctrl['min'], ctrl['max'])
                    value = max(ctrl['min'], min(ctrl['max'], value))

            # Set the control
            cmd = f"v4l2-ctl --device={self.device_path} --set-ctrl={control_name}={value}"
            result = self._run_cmd(cmd, timeout=5)

            if result.returncode == 0:
                # Verify the value was actually set
                actual_value = self.verify_control_value(control_name)
                if actual_value is not None:
                    if control_name in self.controls_info:
                        self.controls_info[control_name]['current'] = actual_value
                    if actual_value != value:
                        logger.warning("Set %s to %s but device reports %s", control_name, value, actual_value)
                    return True
                else:
                    logger.warning("Could not verify %s after setting", control_name)
                    return True
            else:
                error_msg = (result.stderr or result.stdout or "").strip()
                logger.error("Failed to set %s=%s: %s", control_name, value, error_msg)
                return False

        except Exception:
            logger.exception("Error setting %s", control_name)
            return False

    def apply_values(self, values_dict):
        """Apply control values with better error handling"""
        if not values_dict:
            logger.info("apply_values called with empty values_dict")
            return

        logger.info("Applying %d control values", len(values_dict))
        failed_controls = []
        skipped_controls = []

        # Check camera's manual exposure support
        if self.supports_manual_exposure is None:
            self.check_auto_exposure_support()

        # Apply auto_exposure first if present and supported
        if 'auto_exposure' in values_dict:
            if self.supports_manual_exposure:
                if not self.set_control_value('auto_exposure', values_dict['auto_exposure']):
                    failed_controls.append('auto_exposure')
                time.sleep(0.3)
            else:
                logger.info("Skipping auto_exposure - not supported by this camera")
                skipped_controls.append('auto_exposure')

        # Apply other controls
        for name, val in values_dict.items():
            if name in ['auto_exposure', 'exposure_time_absolute']:
                continue
            
            # Skip if control doesn't exist on this camera
            if name not in self.controls_info:
                logger.debug("Control %s not available on this camera", name)
                skipped_controls.append(name)
                continue
                
            if not self.set_control_value(name, val):
                failed_controls.append(name)

        # Apply exposure_time_absolute last
        if 'exposure_time_absolute' in values_dict and 'exposure_time_absolute' in self.controls_info:
            if not self.set_control_value('exposure_time_absolute', values_dict['exposure_time_absolute']):
                failed_controls.append('exposure_time_absolute')

        # Refresh control info
        self.controls_info = self.get_camera_controls()
        self.current_values = self.get_all_current_values()

        if failed_controls:
            logger.warning("Failed to apply %d controls: %s", len(failed_controls), failed_controls)
        if skipped_controls:
            logger.info("Skipped %d unsupported controls: %s", len(skipped_controls), skipped_controls)
        if not failed_controls and not skipped_controls:
            logger.info("Applied all control values successfully")

    def reset_to_stored_defaults(self):
        """Reset controls with full device reinitialization and verification"""
        logger.info("=== RESET OPERATION STARTING ===")
        
        # Stop streaming
        self.stop_streaming()
        
        with self.camera_lock:
            # Close and reopen device completely
            if self.cap:
                logger.debug("Releasing VideoCapture for reset")
                self.cap.release()
                time.sleep(0.5)  # Give OS time to fully release device
            
            # Reopen device
            logger.debug("Reopening VideoCapture")
            self.cap = cv2.VideoCapture(self.device_path, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                raise RuntimeError(f"Could not reopen camera at {self.device_path}")
            
            # Refresh control info from hardware
            self.controls_info = self.get_camera_controls()
            
            # Prepare defaults to restore
            if not self.stored_defaults:
                self.stored_defaults = self.calculate_default_values()
            
            defaults_to_restore = self.stored_defaults.copy()
            
            # Only force manual exposure if camera supports it
            if self.supports_manual_exposure:
                defaults_to_restore['auto_exposure'] = self.manual_exposure_value
            
            logger.info("Restoring defaults: %s", defaults_to_restore)
            
            # Set resolution first
            if self.supported_resolutions:
                fmt, w, h = self.supported_resolutions[0]
                self._set_camera_properties(w, h, fmt)
            
            # Ensure manual exposure mode if supported
            if self.supports_manual_exposure:
                if not self.ensure_manual_exposure_mode():
                    logger.error("Failed to set manual exposure mode during reset")
            
            # Apply all stored defaults
            self.apply_values(defaults_to_restore)
            
            # Verify critical controls
            actual_auto = self.verify_control_value('auto_exposure')
            actual_brightness = self.verify_control_value('brightness')
            logger.info("After reset - auto_exposure: %s, brightness: %s", actual_auto, actual_brightness)
            
            # Clear frame queue
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                except:
                    break
        
        # Restart streaming
        self.start_streaming()
        time.sleep(0.5)
        
        logger.info("=== RESET OPERATION COMPLETED ===")

    def _initialize_camera(self, apply_defaults=True, prefer_stored=True):
        """Initialize camera with better error handling for different camera types"""
        with self.camera_lock:
            if self.cap:
                logger.debug("Releasing existing VideoCapture")
                self.cap.release()
                time.sleep(0.3)

            logger.debug("Opening VideoCapture on %s (V4L2 backend)", self.device_path)
            self.cap = cv2.VideoCapture(self.device_path, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                raise RuntimeError(f"Could not open camera at {self.device_path}")

            # Probe device
            try:
                self.supported_resolutions = self._get_supported_resolutions()
            except Exception as e:
                logger.warning("Could not get supported resolutions: %s", e)
                self.supported_resolutions = [('MJPG', 640, 480)]  # Fallback
                
            self.controls_info = self.get_camera_controls()
            
            # Check camera capabilities
            self.check_auto_exposure_support()

            # Preserve original hardware defaults
            if not self.original_hardware_defaults:
                for name, ctrl in self.controls_info.items():
                    self.original_hardware_defaults[name] = ctrl['default']
                logger.debug("Captured original hardware defaults for %d controls",
                             len(self.original_hardware_defaults))

            # Calculate defaults if needed
            if not self.stored_defaults:
                self.stored_defaults = self.calculate_default_values()
                logger.debug("Calculated defaults stored: %s", self.stored_defaults)

            # Apply defaults if requested
            if apply_defaults:
                if prefer_stored and self.stored_defaults:
                    logger.info("Applying STORED defaults on initialize")
                    self.apply_values(self.stored_defaults)
                else:
                    logger.info("Applying CALCULATED defaults on initialize")
                    calculated = self.calculate_default_values()
                    self.apply_values(calculated)

            # Set resolution
            if self.supported_resolutions:
                fmt, w, h = self.supported_resolutions[0]
                self._set_camera_properties(w, h, fmt)

            logger.info("Camera initialized successfully: %s", self.device_path)

    def get_all_current_values(self):
        return {k: v['current'] for k, v in self.controls_info.items()}

    def set_resolution(self, width, height, fmt='MJPG'):
        with self.camera_lock:
            try:
                self._set_camera_properties(width, height, fmt)
                while not self.frame_queue.empty():
                    self.frame_queue.get_nowait()
                logger.info("Resolution set to %dx%d (%s)", width, height, fmt)
                return True, f"Resolution set to {width}x{height} ({fmt})"
            except Exception as e:
                logger.exception("Failed to set resolution")
                return False, str(e)

    def cleanup(self):
        self.stop_streaming()
        with self.camera_lock:
            if self.cap:
                logger.debug("Releasing VideoCapture in cleanup()")
                self.cap.release()

# ==================== Camera Management Functions ====================
def initialize_camera(device_path=None):
    """Initialize camera with specified device or first available"""
    global camera, current_camera_device, available_cameras
    
    # Detect available cameras
    available_cameras = detect_available_cameras()
    
    if not available_cameras:
        logger.error("No cameras found")
        return False
    
    # Use specified device or first available
    if device_path:
        # Verify the device exists in available cameras
        if not any(cam['device'] == device_path for cam in available_cameras):
            logger.error("Specified device %s not found", device_path)
            device_path = available_cameras[0]['device']
    else:
        device_path = available_cameras[0]['device']
    
    try:
        # Clean up existing camera if any
        if camera:
            logger.info("Cleaning up existing camera")
            camera.cleanup()
            camera = None
            time.sleep(0.5)
        
        camera = ThreadSafeCameraController(device_path)
        current_camera_device = device_path
        logger.info("Camera initialized successfully: %s", device_path)
        return True
    except Exception:
        logger.exception("Camera initialization failed")
        return False

def switch_camera(new_device):
    """Switch to a different camera device"""
    global camera, current_camera_device
    
    if new_device == current_camera_device:
        logger.info("Already using camera %s", new_device)
        return True, "Already using this camera"
    
    logger.info("Switching from %s to %s", current_camera_device, new_device)
    
    # Clean up current camera
    if camera:
        camera.cleanup()
        camera = None
        time.sleep(0.5)
    
    # Initialize new camera
    if initialize_camera(new_device):
        return True, f"Switched to {new_device}"
    else:
        # Try to reinitialize previous camera
        logger.error("Failed to switch to %s, trying to restore previous camera", new_device)
        if initialize_camera(current_camera_device):
            return False, "Failed to switch camera, restored previous"
        else:
            return False, "Failed to switch camera"

# ==================== Routes ====================
@app.route('/')
def index():
    if not camera:
        return "Camera not initialized", 500

    w, h = camera.get_current_resolution()
    
    # Generate camera dropdown options
    camera_options = "\n".join(
        f'<option value="{cam["device"]}" {"selected" if cam["device"] == current_camera_device else ""}>{cam["name"]} ({cam["device"]})</option>'
        for cam in available_cameras
    )
    
    # Generate resolution options
    resolution_options = "\n".join(
        f'<option value="{fmt},{w},{h}">{fmt} - {w}x{h}</option>'
        for fmt, w, h in camera.supported_resolutions
    )

    # Split controls by type
    int_controls = []
    other_controls = []
    for name, ctrl in camera.controls_info.items():
        if ctrl['type'] == 'int':
            int_controls.append((name, ctrl))
        else:
            other_controls.append((name, ctrl))

    def render_controls(control_list):
        html = ""
        for name, ctrl in control_list:
            if ctrl['type'] == 'int':
                html += f'''
                <div class="control-group">
                    <label>{name}</label>
                    <input type="range" id="{name}" min="{ctrl['min']}" max="{ctrl['max']}" 
                        step="{ctrl['step']}" value="{ctrl['current']}" 
                        oninput="updateControl('{name}', this.value)" />
                    <span id="{name}-value">{ctrl['current']}</span>
                </div>
                '''
            elif ctrl['type'] == 'bool':
                checked = "checked" if ctrl['current'] == 1 else ""
                html += f'''
                <div class="control-group">
                    <label>
                        <input type="checkbox" id="{name}" {checked} 
                            onchange="updateControl('{name}', this.checked ? 1 : 0)" />
                        {name}
                    </label>
                </div>
                '''
            elif ctrl['type'] == 'menu':
                options = "".join(
                    f'<option value="{i}" {"selected" if i == ctrl["current"] else ""}>Option {i}</option>'
                    for i in range(ctrl['min'], ctrl['max'] + 1)
                )
                html += f'''
                <div class="control-group">
                    <label>{name}</label>
                    <select id="{name}" onchange="updateControl('{name}', this.value)">
                        {options}
                    </select>
                </div>
                '''
        return html

    html_int_controls = render_controls(int_controls)
    html_other_controls = render_controls(other_controls)

    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Camera Stream</title>
        {% raw %}
        <style>
            body { font-family: sans-serif; margin: 10px; }
            .flex-container {
                display: flex;
                gap: 30px;
                align-items: flex-start;
            }
            .video-container img {
                width: 800px;
                height: 600px;
                background: black;
                border: 1px solid #ccc;
            }
            .controls-wrapper {
                display: flex;
                flex-direction: row;
                gap: 20px;
                flex-wrap: nowrap;
            }
            .column {
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 10px;
            }
            .control-group {
                font-size: 14px;
            }
            .control-group input[type="range"],
            .control-group select {
                width: 100%;
            }
            .main-controls {
                margin: 15px 0;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            .main-controls button, .main-controls select {
                padding: 6px 12px;
            }
            .camera-selector {
                background-color: #e8f4fd;
                padding: 5px 10px;
                border: 1px solid #2196F3;
                border-radius: 4px;
            }
            .camera-selector label {
                font-weight: bold;
                margin-right: 5px;
            }
            .status-message {
                padding: 8px;
                margin: 10px 0;
                border-radius: 4px;
                display: none;
            }
            .status-success {
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            .status-error {
                background-color: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
        </style>
        {% endraw %}
    </head>
    <body>
        <h2>USB Camera Stream</h2>
        
        <div id="statusMessage" class="status-message"></div>
        
        <div class="main-controls">
            <div class="camera-selector">
                <label>Camera ({{ available_cameras|length }} detected):</label>
                <select id="cameraSelect" onchange="changeCamera()" {% if available_cameras|length <= 1 %}disabled{% endif %}>
                    {{ camera_options|safe }}
                </select>
            </div>
            
            <label>Resolution:</label>
            <select id="resolution" onchange="changeResolution()">
                {{ resolution_options|safe }}
            </select>
            
            <button class="reset-button" onclick="resetControls()">Reset to Defaults</button>
            <a href="/camera_status" target="_blank" class="status-link">View Camera Status</a>
            <button onclick="refreshCameras()" style="padding: 6px 12px;">Refresh Cameras</button>
        </div>
        
        <div class="flex-container">
            <div class="video-container">
                <img id="videoStream" src="/video_feed?t=0">
            </div>
            <div class="controls-wrapper">
                <div class="column">
                    {{ html_int_controls|safe }}
                </div>
                <div class="column">
                    {{ html_other_controls|safe }}
                </div>
            </div>
        </div>

        {% raw %}
        <script>
            function showStatus(message, isSuccess) {
                const statusDiv = document.getElementById("statusMessage");
                statusDiv.textContent = message;
                statusDiv.className = "status-message " + (isSuccess ? "status-success" : "status-error");
                statusDiv.style.display = "block";
                setTimeout(() => {
                    statusDiv.style.display = "none";
                }, 3000);
            }
            
            function refreshCameras() {
                showStatus("Scanning for cameras...", true);
                fetch("/refresh_cameras", {
                    method: "POST"
                }).then(resp => resp.json()).then(data => {
                    showStatus("Found " + data.count + " camera(s)", true);
                    setTimeout(() => location.reload(), 1000);
                }).catch(err => {
                    showStatus("Error scanning cameras", false);
                });
            }
            
            function changeCamera() {
                const device = document.getElementById("cameraSelect").value;
                showStatus("Switching camera...", true);
                
                fetch("/switch_camera", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ device: device })
                }).then(resp => resp.json()).then(data => {
                    if (data.success) {
                        showStatus(data.message, true);
                        // Reload the page to get new camera controls
                        setTimeout(() => location.reload(), 500);
                    } else {
                        showStatus(data.message, false);
                    }
                }).catch(err => {
                    showStatus("Error switching camera", false);
                });
            }
            
            function updateControl(controlName, value) {
                fetch("/set_control", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ control: controlName, value: parseInt(value) })
                }).then(resp => resp.json()).then(data => {
                    const valueSpan = document.getElementById(controlName + "-value");
                    if (valueSpan) valueSpan.textContent = value;
                });
            }

            function resetControls() {
                if (confirm("Reset all controls to stored defaults and reinitialize camera?")) {
                    fetch("/reset_controls", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" }
                    }).then(() => location.reload());
                }
            }

            function changeResolution() {
                const [fmt, w, h] = document.getElementById("resolution").value.split(",");
                fetch("/set_resolution", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ width: parseInt(w), height: parseInt(h), format: fmt })
                }).then(() => {
                    document.getElementById("videoStream").src = "/video_feed?t=" + new Date().getTime();
                });
            }
        </script>
        {% endraw %}
    </body>
    </html>
    ''', camera_options=camera_options,
         resolution_options=resolution_options,
         html_int_controls=html_int_controls,
         html_other_controls=html_other_controls,
         available_cameras=available_cameras)

@app.route('/refresh_cameras', methods=['POST'])
def refresh_cameras_route():
    """Rescan for available cameras"""
    global available_cameras
    
    logger.info("Refreshing camera list...")
    available_cameras = detect_available_cameras()
    
    return jsonify({
        "success": True,
        "count": len(available_cameras),
        "cameras": available_cameras
    })

@app.route('/switch_camera', methods=['POST'])
def switch_camera_route():
    """Route to switch between cameras"""
    data = request.get_json()
    device = data.get("device")
    
    if not device:
        return jsonify({"success": False, "message": "No device specified"}), 400
    
    success, message = switch_camera(device)
    return jsonify({"success": success, "message": message})

@app.route('/debug_cameras')
def debug_cameras():
    """Debug endpoint to show detailed camera detection info"""
    debug_info = {
        "detected_cameras": available_cameras,
        "current_camera": current_camera_device,
        "video_devices": []
    }
    
    # List all /dev/video* devices
    for i in range(20):
        device_path = f"/dev/video{i}"
        if os.path.exists(device_path):
            info = {"device": device_path, "exists": True}
            
            # Try v4l2-ctl info
            try:
                cmd = f"v4l2-ctl --device={device_path} --info"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
                info["v4l2_success"] = result.returncode == 0
                info["v4l2_output"] = result.stdout[:500] if result.stdout else "No output"
            except Exception as e:
                info["v4l2_error"] = str(e)
            
            # Try OpenCV
            try:
                test_cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
                info["opencv_opened"] = test_cap.isOpened()
                if test_cap.isOpened():
                    ret, frame = test_cap.read()
                    info["can_read_frame"] = ret
                    test_cap.release()
            except Exception as e:
                info["opencv_error"] = str(e)
            
            debug_info["video_devices"].append(info)
    
    return jsonify(debug_info)

@app.route('/get_cameras')
def get_cameras():
    """Get list of available cameras"""
    return jsonify({
        "cameras": available_cameras,
        "current": current_camera_device
    })

@app.route('/set_control', methods=['POST'])
def set_control():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    
    data = request.get_json()
    logger.info("Set control request: %s", data)
    control_name = data.get("control")
    value = data.get("value")
    
    if not control_name or value is None:
        return jsonify({"success": False, "message": "Missing control name or value"}), 400
    
    success = camera.set_control_value(control_name, int(value))
    
    if success:
        return jsonify({"success": True, "message": f"Set {control_name} = {value}"})
    else:
        return jsonify({"success": False, "message": f"Failed to set {control_name}"}), 500

@app.route('/reset_controls', methods=['POST'])
def reset_controls():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    try:
        camera.reset_to_stored_defaults()
        return jsonify({"success": True, "message": "Controls reset to stored defaults"})
    except Exception as e:
        logger.exception("Error resetting controls")
        return jsonify({"success": False, "message": f"Error resetting controls: {str(e)}"}), 500

@app.route('/camera_status')
def camera_status():
    """Display current camera settings in HTML format"""
    if not camera:
        return "Camera not initialized", 500
    
    w, h = camera.get_current_resolution()
    
    # Camera info section
    camera_info_html = f"""
    <div class="info-row">
        <span class="info-label">Current Device:</span>
        <span class="info-value">{current_camera_device}</span>
    </div>
    <div class="info-row">
        <span class="info-label">Available Cameras:</span>
        <span class="info-value">{len(available_cameras)}</span>
    </div>
    """
    
    # List all available cameras
    cameras_list_html = ""
    for cam in available_cameras:
        is_current = " ✓" if cam['device'] == current_camera_device else ""
        cameras_list_html += f"""
        <tr>
            <td>{cam['device']}</td>
            <td>{cam['name']}</td>
            <td>{"Active" if is_current else ""}</td>
        </tr>
        """
    
    controls_html = ""
    for name, ctrl in camera.controls_info.items():
        stored_default = camera.stored_defaults.get(name, "N/A")
        original_hw_default = camera.original_hardware_defaults.get(name, "N/A")
        controls_html += f"""
        <tr>
            <td><strong>{name}</strong></td>
            <td>{ctrl['type']}</td>
            <td>{ctrl['current']}</td>
            <td>{ctrl['min']}</td>
            <td>{ctrl['max']}</td>
            <td>{ctrl['step']}</td>
            <td>{original_hw_default}</td>
            <td>{stored_default}</td>
        </tr>
        """
    
    resolution_list = ""
    for fmt, res_w, res_h in camera.supported_resolutions:
        current_marker = " ✓" if (res_w == w and res_h == h) else ""
        resolution_list += f"<li>{fmt} - {res_w}x{res_h}{current_marker}</li>"
    
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Camera Status</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                table { border-collapse: collapse; width: 100%; margin: 20px 0; }
                th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                th { background-color: #f2f2f2; }
                .section { margin: 30px 0; }
                .current-resolution { background-color: #e8f5e8; padding: 10px; border-radius: 5px; }
                .info-row { margin: 5px 0; }
                .info-label { font-weight: bold; margin-right: 10px; }
                ul { list-style-type: none; padding: 0; }
                li { padding: 5px; margin: 2px 0; background-color: #f9f9f9; border-radius: 3px; }
            </style>
        </head>
        <body>
            <h1>Camera Status</h1>
            
            <div class="section">
                <h2>Camera Information</h2>
                {{ camera_info_html|safe }}
            </div>
            
            <div class="section">
                <h2>Available Cameras</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Device</th>
                            <th>Name</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {{ cameras_list_html|safe }}
                    </tbody>
                </table>
            </div>
            
            <div class="section">
                <h2>Current Resolution</h2>
                <div class="current-resolution">
                    <strong>{{ current_resolution }}</strong>
                </div>
            </div>
            
            <div class="section">
                <h2>Available Resolutions</h2>
                <ul>
                    {{ resolution_list|safe }}
                </ul>
            </div>
            
            <div class="section">
                <h2>Camera Controls</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Control Name</th>
                            <th>Type</th>
                            <th>Current Value</th>
                            <th>Min</th>
                            <th>Max</th>
                            <th>Step</th>
                            <th>Hardware Default</th>
                            <th>Stored Default</th>
                        </tr>
                    </thead>
                    <tbody>
                        {{ controls_html|safe }}
                    </tbody>
                </table>
            </div>
            
            <div class="section">
                <button onclick="window.close()" style="padding: 10px 15px; background-color: #6c757d; color: white; border: none; border-radius: 5px;">Close</button>
            </div>
        </body>
        </html>
    ''', 
    camera_info_html=camera_info_html,
    cameras_list_html=cameras_list_html,
    current_resolution=f"{w}x{h}",
    resolution_list=resolution_list,
    controls_html=controls_html)

@app.route('/camera_status_json')
def camera_status_json():
    """Return current camera settings as JSON"""
    if not camera:
        return jsonify({"error": "Camera not initialized"}), 500
    
    w, h = camera.get_current_resolution()
    status = {
        "device": camera.device_path,
        "current_resolution": {"width": w, "height": h},
        "supported_resolutions": [
            {"format": fmt, "width": W, "height": H} 
            for fmt, W, H in camera.supported_resolutions
        ],
        "controls": camera.controls_info,
        "stored_defaults": camera.stored_defaults,
        "original_hardware_defaults": camera.original_hardware_defaults,
        "available_cameras": available_cameras,
        "current_camera": current_camera_device,
        "timestamp": time.time()
    }
    return jsonify(status)

@app.route('/video_feed')
def video_feed():
    def generate():
        while True:
            if not camera:
                time.sleep(0.1)
                continue
            frame = camera.get_latest_frame()
            if frame:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.033)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/set_resolution', methods=['POST'])
def set_resolution():
    if not camera:
        return jsonify({"message": "Camera not initialized"}), 500
    data = request.get_json()
    width = int(data.get("width", 640))
    height = int(data.get("height", 480))
    fmt = data.get("format", "MJPG")
    logger.info("Set resolution request: %dx%d (%s)", width, height, fmt)
    success, msg = camera.set_resolution(width, height, fmt)
    return jsonify({"success": success, "message": msg})

# ==================== Signals & Main ====================
def cleanup_handler(signum, frame):
    logger.info("Received signal %s; cleaning up", signum)
    if camera:
        camera.cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_handler)
signal.signal(signal.SIGTERM, cleanup_handler)

if __name__ == '__main__':
    logger.info("="*60)
    logger.info("Camera server starting on http://0.0.0.0:5000")
    logger.info("="*60)
    
    # Detect available cameras with detailed logging
    logger.info("Scanning for available cameras...")
    available_cameras = detect_available_cameras()
    
    if not available_cameras:
        logger.critical("No cameras detected! Please check:")
        logger.critical("1. Camera is connected")
        logger.critical("2. Camera drivers are loaded (try: ls /dev/video*)")
        logger.critical("3. User has permission to access camera (try: sudo)")
        logger.critical("4. Try: v4l2-ctl --list-devices")
        
        # Try to create a dummy entry for testing
        if os.path.exists("/dev/video0"):
            logger.warning("Found /dev/video0 but couldn't open it - adding as fallback")
            available_cameras = [{
                "device": "/dev/video0",
                "name": "Fallback Camera 0",
                "driver": "unknown",
                "index": 0
            }]
        
    # Initialize with first available camera
    if not initialize_camera():
        logger.critical("Failed to initialize any camera")
        sys.exit(1)
    
    logger.info("="*60)
    logger.info("Server ready with %d camera(s):", len(available_cameras))
    for cam in available_cameras:
        logger.info("  - %s at %s", cam['name'], cam['device'])
    logger.info("Using camera: %s", current_camera_device)
    logger.info("="*60)
    logger.info("Access the web interface at: http://localhost:5000")
    logger.info("Debug camera info at: http://localhost:5000/debug_cameras")
    logger.info("="*60)
    
    app.run(host='0.0.0.0', port=5000, threaded=True)