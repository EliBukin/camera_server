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

app = Flask(__name__)
camera = None

class ThreadSafeCameraController:
    def __init__(self, device="/dev/video1"):
        self.device_path = device
        self.cap = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.capture_thread = None
        self.streaming = False
        self.camera_lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.stored_defaults = {}  # NEW: Store calculated defaults
        self.original_hardware_defaults = {}  # NEW: Store original hardware defaults before override

        # Initialize camera (this now includes setting defaults)
        self._initialize_camera()

        # Start streaming
        self.start_streaming()
        self.timelapse = TimeLapseCapturer(self)
        self.recorder = VideoRecorder(self)

    def start_streaming(self):
        if not self.streaming:
            self.streaming = True
            self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
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
        """Get supported resolutions using v4l2-ctl, no fallbacks"""
        cmd = f"v4l2-ctl --device={self.device_path} --list-formats-ext"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            raise RuntimeError(f"v4l2-ctl command failed: {result.stderr}")

        resolutions = []
        fmt = None

        for line in result.stdout.splitlines():
            line = line.strip()
            # Match format line: [0]: 'MJPG' (Motion-JPEG, compressed)
            m_fmt = re.match(r"\[\d+\]: '(\w+)'", line)
            # Match size line: Size: Discrete 3840x2160
            m_size = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)

            if m_fmt:
                fmt = m_fmt.group(1)
            elif m_size and fmt:
                w, h = map(int, m_size.groups())
                resolutions.append((fmt, w, h))

        if not resolutions:
            raise RuntimeError("No supported resolutions found")

        return sorted(resolutions, key=lambda x: (x[1], x[2]))

    def _set_camera_properties(self, width, height, fmt='MJPG'):
        """Set camera properties - only called after resolution detection"""
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fmt))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 15)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def get_camera_controls(self):
        """Get camera controls with type-specific parsing"""
        try:
            cmd = f"v4l2-ctl --device={self.device_path} --list-ctrls"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"v4l2-ctl failed: {result.stderr}")
                return {}
            
            controls = {}
            output = result.stdout
            
            for line in output.strip().split('\n'):
                # Skip header lines and empty lines
                if not line.strip() or 'Controls' in line or not ':' in line:
                    continue
                    
                try:
                    # Parse control name - get the first word after stripping whitespace
                    name_part = line.split(':')[0].strip()
                    name = name_part.split()[0]  # Get the FIRST word (control name)
                    
                    # Parse control type
                    if '(int)' in line:
                        ctrl_type = 'int'
                    elif '(bool)' in line:
                        ctrl_type = 'bool'
                    elif '(menu)' in line:
                        ctrl_type = 'menu'
                    else:
                        continue  # Skip unknown types
                    
                    # Parse values based on control type
                    default_match = re.search(r'default=(-?\d+)', line)
                    current_match = re.search(r'value=(-?\d+)', line)
                    
                    if not default_match or not current_match:
                        continue  # Skip if missing required values
                    
                    default_val = int(default_match.group(1))
                    current_val = int(current_match.group(1))
                    
                    if ctrl_type == 'int':
                        # Int controls have min, max, step
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
                        # Bool controls: only default and current
                        controls[name] = {
                            'type': ctrl_type,
                            'min': 0,
                            'max': 1,
                            'step': 1,
                            'default': default_val,
                            'current': current_val,
                        }
                        
                    elif ctrl_type == 'menu':
                        # Menu controls have min, max but no step
                        min_match = re.search(r'min=(-?\d+)', line)
                        max_match = re.search(r'max=(-?\d+)', line)
                        
                        if not all([min_match, max_match]):
                            continue
                        
                        controls[name] = {
                            'type': ctrl_type,
                            'min': int(min_match.group(1)),
                            'max': int(max_match.group(1)),
                            'step': 1,  # Menus always step by 1
                            'default': default_val,
                            'current': current_val,
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
        """Calculate default values based on control types with better logic"""
        defaults = {}

        for name, ctrl in self.controls_info.items():
            if ctrl['type'] == 'int':
                # Use middle value between min and max
                defaults[name] = (ctrl['min'] + ctrl['max']) // 2
            elif ctrl['type'] == 'bool':
                # Use minimum value (usually 0/False)
                defaults[name] = ctrl['min']  
            elif ctrl['type'] == 'menu':
                # For menu controls, use minimum value (first option)
                # Exception: for auto_exposure, prefer manual mode if available
                if name == 'auto_exposure' and ctrl['max'] >= 1:
                    defaults[name] = 1  # Manual mode
                else:
                    defaults[name] = ctrl['min']

        return defaults

    def set_control_value(self, control_name, value):
        """Set a single control value using v4l2-ctl with validation"""
        try:
            # Validate value is within bounds
            if control_name in self.controls_info:
                ctrl = self.controls_info[control_name]
                if value < ctrl['min'] or value > ctrl['max']:
                    print(f"WARNING: {control_name} value {value} is out of bounds [{ctrl['min']}-{ctrl['max']}], skipping")
                    return False

            cmd = f"v4l2-ctl --device={self.device_path} --set-ctrl={control_name}={value}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                print(f"Set {control_name} = {value}")
                # Update the current value in controls_info
                if control_name in self.controls_info:
                    self.controls_info[control_name]['current'] = value
                return True
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                print(f"Failed to set {control_name}: {error_msg}")
                return False
        except Exception as e:
            print(f"Error setting {control_name}: {e}")
            return False

    def set_default_values(self):
        """Set all controls to calculated default values with dependency handling"""
        print("Setting camera controls to default values...")

        default_values = self.calculate_default_values()
        failed_controls = []

        # First pass: Set non-dependent controls
        for control_name, value in default_values.items():
            # Skip exposure controls in first pass - they have dependencies
            if control_name in ['auto_exposure', 'exposure_time_absolute']:
                continue
            success = self.set_control_value(control_name, value)
            if not success:
                failed_controls.append(control_name)

        # Second pass: Handle exposure controls with dependencies
        # Set auto_exposure first, then exposure_time_absolute
        if 'auto_exposure' in default_values:
            # For auto_exposure, use manual mode (value=1) instead of calculated default
            manual_mode = 1  # Usually 1 = Manual Mode
            success = self.set_control_value('auto_exposure', manual_mode)
            if success:
                # Update our stored default to the working value
                self.stored_defaults['auto_exposure'] = manual_mode
                # Now try to set exposure_time_absolute
                if 'exposure_time_absolute' in default_values:
                    success = self.set_control_value('exposure_time_absolute', default_values['exposure_time_absolute'])
                    if not success:
                        failed_controls.append('exposure_time_absolute')
            else:
                failed_controls.append('auto_exposure')

        # Update current values after setting defaults
        self.controls_info = self.get_camera_controls()

        # Update the default values in controls_info to reflect calculated defaults
        # This INTENTIONALLY overwrites broken hardware defaults (like -8193, 57343) 
        # with sensible calculated ones for actual use
        for control_name, calculated_default in default_values.items():
            if control_name in self.controls_info:
                # Use stored default (which might have been updated for compatibility)
                working_default = self.stored_defaults.get(control_name, calculated_default)
                self.controls_info[control_name]['default'] = working_default

        self.current_values = self.get_all_current_values()

        if failed_controls:
            print(f"WARNING: Failed to set {len(failed_controls)} controls: {failed_controls}")
        print(f"Applied defaults to {len(default_values) - len(failed_controls)} of {len(default_values)} controls")

    def reset_to_stored_defaults(self):
        """NEW: Reset controls to stored defaults and reinitialize camera"""
        print("Resetting controls to stored defaults...")
        print(f"Using stored default values: {self.stored_defaults}")
        
        # Apply stored defaults
        for control_name, value in self.stored_defaults.items():
            self.set_control_value(control_name, value)
        
        # Reinitialize camera
        print("Reinitializing camera after reset...")
        self._initialize_camera()
        print("Reset to stored defaults completed")

    def _initialize_camera(self):
        """Initialize camera and set default control values"""
        with self.camera_lock:
            if self.cap:
                self.cap.release()
                time.sleep(0.3)

            self.cap = cv2.VideoCapture(self.device_path)
            if not self.cap.isOpened():
                raise RuntimeError(f"Could not open camera at {self.device_path}")

            # Get supported resolutions first
            self.supported_resolutions = self._get_supported_resolutions()

            # Get camera controls info
            self.controls_info = self.get_camera_controls()
            
            # Preserve original hardware defaults before any modification (only on first init)
            if not hasattr(self, 'original_hardware_defaults') or not self.original_hardware_defaults:
                self.original_hardware_defaults = {}
                for name, ctrl in self.controls_info.items():
                    self.original_hardware_defaults[name] = ctrl['default']
                print(f"Original hardware defaults captured for {len(self.original_hardware_defaults)} controls")

            # Calculate and store defaults (NEW)
            calculated_defaults = self.calculate_default_values()
            if not hasattr(self, 'stored_defaults') or not self.stored_defaults:
                self.stored_defaults = calculated_defaults.copy()
                print(f"Stored calculated defaults: {self.stored_defaults}")

            # Set all controls to calculated defaults
            self.set_default_values()

            # Set resolution using first available
            fmt, w, h = self.supported_resolutions[0]
            self._set_camera_properties(w, h, fmt)

    def get_all_current_values(self):
        return {k: v['current'] for k, v in self.controls_info.items()}

    def set_resolution(self, width, height, fmt='MJPG'):
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

class TimeLapseCapturer:
    def __init__(self, camera, output_dir="timelapse"):
        self.camera = camera
        self.output_dir = output_dir
        self.interval = 5
        self.running = False
        self.thread = None
        os.makedirs(self.output_dir, exist_ok=True)

    def start(self, interval):
        self.interval = interval
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.capture_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()

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

class VideoRecorder:
    def __init__(self, camera, output_dir="videos"):
        self.camera = camera
        self.output_dir = output_dir
        self.recording = False
        self.thread = None
        self.writer = None
        self.output_file = None
        os.makedirs(self.output_dir, exist_ok=True)

    def start(self, filename=None, fps=15):
        if self.recording:
            return
        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self.output_dir, f"record_{timestamp}.avi")
        w, h = self.camera.get_current_resolution()
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        with self.camera.camera_lock:
            self.writer = cv2.VideoWriter(filename, fourcc, fps, (w, h))
        self.output_file = filename
        self.recording = True
        self.thread = threading.Thread(target=self.record_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.recording = False
        if self.thread:
            self.thread.join()
        if self.writer:
            self.writer.release()
            self.writer = None
        return self.output_file

    def record_loop(self):
        while self.recording:
            with self.camera.camera_lock:
                ret, frame = self.camera.cap.read()
            if ret and frame is not None and self.writer:
                self.writer.write(frame)
            else:
                time.sleep(0.05)

# Initialize camera
def initialize_camera():
    global camera
    try:
        camera = ThreadSafeCameraController()
        return True
    except Exception as e:
        print(f"Camera init failed: {e}")
        return False

# Initialize camera
def initialize_camera():
    global camera
    try:
        camera = ThreadSafeCameraController()
        return True
    except Exception as e:
        print(f"Camera init failed: {e}")
        return False

@app.route('/')
def index():
    if not camera:
        return "Camera not initialized", 500

    w, h = camera.get_current_resolution()
    options_html = "\n".join(
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
            }
            .main-controls button, .main-controls select {
                margin-right: 10px;
                padding: 6px 12px;
            }
        </style>
        {% endraw %}
    </head>
    <body>
        <h2>USB Camera Stream</h2>
        <div class="main-controls">
            <label>Resolution:</label>
            <select id="resolution" onchange="changeResolution()">
                {{ options_html|safe }}
            </select>
            <button class="reset-button" onclick="resetControls()">Reset to Defaults</button>
            <a href="/camera_status" target="_blank" class="status-link">View Camera Status</a>
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
                <div class="column">
                    <div class="control-group">
                        <label for="timelapse-interval">Interval (sec):</label>
                        <input type="number" id="timelapse-interval" value="5" min="1">
                    </div>
                    <div class="control-group" style="display: flex; gap: 10px;">
                        <button onclick="startTimelapse()">Start Timelapse</button>
                        <button onclick="stopTimelapse()">Stop Timelapse</button>
                    </div>
                    <div class="control-group" style="display: flex; gap: 10px; margin-top: 10px;">
                        <button onclick="startRecording()">Start Recording</button>
                        <button onclick="stopRecording()">Stop Recording</button>
                    </div>

                </div>
            </div>
        </div>

        {% raw %}
        <script>
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

            function startTimelapse() {
                const interval = parseInt(document.getElementById("timelapse-interval").value);
                fetch("/start_timelapse", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ interval: interval })
                }).then(resp => resp.json())
                  .then(data => alert(data.message));
            }
            function stopTimelapse() {
                fetch("/stop_timelapse", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" }
                }).then(resp => resp.json())
                  .then(data => alert(data.message));
            }

            function startRecording() {
                fetch("/start_recording", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" }
                }).then(resp => resp.json())
                  .then(data => alert(data.message));
            }
            function stopRecording() {
                fetch("/stop_recording", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" }
                }).then(resp => resp.json())
                  .then(data => alert(data.message));
            }


        </script>
        {% endraw %}
    </body>
    </html>
    ''', options_html=options_html,
         html_int_controls=html_int_controls,
         html_other_controls=html_other_controls)


# NEW: Route to set individual control values
@app.route('/set_control', methods=['POST'])
def set_control():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    
    data = request.get_json()
    control_name = data.get("control")
    value = data.get("value")
    
    if not control_name or value is None:
        return jsonify({"success": False, "message": "Missing control name or value"}), 400
    
    success = camera.set_control_value(control_name, value)
    
    if success:
        return jsonify({"success": True, "message": f"Set {control_name} = {value}"})
    else:
        return jsonify({"success": False, "message": f"Failed to set {control_name}"}), 500

# NEW: Route to reset controls to stored defaults
@app.route('/reset_controls', methods=['POST'])
def reset_controls():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    
    try:
        camera.reset_to_stored_defaults()
        return jsonify({"success": True, "message": "Controls reset to stored defaults"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error resetting controls: {str(e)}"}), 500

@app.route('/start_timelapse', methods=['POST'])
def start_timelapse():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    data = request.get_json()
    interval = data.get("interval", 5)
    try:
        camera.timelapse.start(interval)
        return jsonify({"success": True, "message": f"Timelapse started with {interval}s interval."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/stop_timelapse', methods=['POST'])
def stop_timelapse():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        camera.timelapse.stop()
        return jsonify({"success": True, "message": "Timelapse stopped."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/start_recording', methods=['POST'])
def start_recording():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    try:
        camera.recorder.start(filename)
        return jsonify({"success": True, "message": "Recording started."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/stop_recording', methods=['POST'])
def stop_recording():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        output = camera.recorder.stop()
        return jsonify({"success": True, "message": f"Recording saved to {output}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/camera_status')
def camera_status():
    """Display current camera settings in HTML format"""
    if not camera:
        return "Camera not initialized", 500
    
    # Get current resolution
    w, h = camera.get_current_resolution()
    
    # Generate HTML table for controls
    controls_html = ""
    for name, ctrl in camera.controls_info.items():
        stored_default = camera.stored_defaults.get(name, "N/A")
        # Show original hardware default (before override) vs calculated stored default
        original_hw_default = camera.original_hardware_defaults.get(name, "N/A") if hasattr(camera, 'original_hardware_defaults') else "N/A"
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
    
    # Generate resolution options
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
                ul { list-style-type: none; padding: 0; }
                li { padding: 5px; margin: 2px 0; background-color: #f9f9f9; border-radius: 3px; }
            </style>
        </head>
        <body>
            <h1>Camera Status</h1>
            
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
    current_resolution=f"{w}x{h}",
    resolution_list=resolution_list,
    controls_html=controls_html)

@app.route('/camera_status_json')
def camera_status_json():
    """Return current camera settings as JSON"""
    if not camera:
        return jsonify({"error": "Camera not initialized"}), 500
    
    # Get current resolution
    w, h = camera.get_current_resolution()
    
    status = {
        "device": camera.device_path,
        "current_resolution": {
            "width": w,
            "height": h
        },
        "supported_resolutions": [
            {"format": fmt, "width": w, "height": h} 
            for fmt, w, h in camera.supported_resolutions
        ],
        "controls": camera.controls_info,  # Contains current overridden defaults
        "stored_defaults": camera.stored_defaults,  # Contains calculated defaults
        "original_hardware_defaults": camera.original_hardware_defaults,  # Contains original broken defaults
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
    width = data.get("width", 640)
    height = data.get("height", 480)
    fmt = data.get("format", "MJPG")
    success, msg = camera.set_resolution(width, height, fmt)
    return jsonify({"success": success, "message": msg})

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

if __name__ == '__main__':
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host='0.0.0.0', port=5000, threaded=True)