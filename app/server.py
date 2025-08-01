from flask import Flask, Response, request, jsonify, render_template_string
import os
import signal
import sys
import time

from camera import ThreadSafeCameraController, discover_cameras
from config import load_config, persist_config

app = Flask(__name__)
camera = None
available_cameras = []
config = load_config()


def initialize_camera(device=None):
    """Initialize the global camera. If device is None, use first detected."""
    global camera, available_cameras
    if device is None:
        if not available_cameras:
            available_cameras = discover_cameras()
        if not available_cameras:
            print("No cameras found")
            return False
        device = available_cameras[0][1]

    if camera:
        camera.cleanup()

    try:
        camera = ThreadSafeCameraController(
            device,
            timelapse_dir=config.get("image_output", "timelapse"),
            video_dir=config.get("video_output", "videos"),
        )
        return True
    except Exception as e:
        print(f"Camera init failed: {e}")
        camera = None
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

    camera_options_html = "\n".join(
        f'<option value="{dev}" {"selected" if dev == camera.device_path else ""}>{name}</option>'
        for name, dev in available_cameras
    )

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
            .video-container {
                width: 800px;
                height: 600px;
                background: black;
                border: 1px solid #ccc;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .video-container img {
                width: 100%;
                height: 100%;
                object-fit: contain;
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
            <label>Camera:</label>
            <select id="camera-select" onchange="changeCamera()">
                {{ camera_options_html|safe }}
            </select>
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
                    <div class="control-group" style="display: flex; gap: 10px;margin-top: 10px;">
                        <button onclick="startRecording()">Start Recording</button>
                        <button onclick="stopRecording()">Stop Recording</button>
                    </div>
                    <div class="control-group">
                        <label for="image-output">Image Output:</label>
                        <input type="text" id="image-output" value="{{ config['image_output'] }}">
                    </div>
                    <div class="control-group">
                        <label for="video-output">Video Output:</label>
                        <input type="text" id="video-output" value="{{ config['video_output'] }}">
                    </div>
                    <div class="control-group" style="margin-top: 10px;">
                        <button onclick="saveConfig()">Save Config</button>
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

            function changeCamera() {
                const dev = document.getElementById("camera-select").value;
                fetch("/set_camera", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ device: dev })
                }).then(() => location.reload());
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

            function saveConfig() {
                const img = document.getElementById("image-output").value;
                const vid = document.getElementById("video-output").value;
                fetch("/save_config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ image_output: img, video_output: vid })
                }).then(resp => resp.json())
                  .then(() => alert("Config saved"));
            }
        </script>
        {% endraw %}
    </body>
    </html>
    ''', options_html=options_html,
         camera_options_html=camera_options_html,
         html_int_controls=html_int_controls,
         html_other_controls=html_other_controls,
         config=config)


@app.route('/set_camera', methods=['POST'])
def set_camera():
    global available_cameras
    data = request.get_json() or {}
    device = data.get('device')
    if not device:
        return jsonify({"success": False, "message": "Missing device"}), 400

    available_cameras = discover_cameras()
    if not initialize_camera(device):
        return jsonify({"success": False, "message": "Failed to initialize camera"}), 500
    return jsonify({"success": True})


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
    if not camera:
        return "Camera not initialized", 500

    w, h = camera.get_current_resolution()
    controls_html = ""
    for name, ctrl in camera.controls_info.items():
        stored_default = camera.stored_defaults.get(name, "N/A")
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
    if not camera:
        return jsonify({"error": "Camera not initialized"}), 500

    w, h = camera.get_current_resolution()
    status = {
        "device": camera.device_path,
        "current_resolution": {"width": w, "height": h},
        "supported_resolutions": [
            {"format": fmt, "width": w, "height": h} for fmt, w, h in camera.supported_resolutions
        ],
        "controls": camera.controls_info,
        "stored_defaults": camera.stored_defaults,
        "original_hardware_defaults": camera.original_hardware_defaults,
        "timestamp": time.time(),
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


@app.route('/save_config', methods=['POST'])
def save_config_route():
    global config
    data = request.get_json() or {}
    img = data.get("image_output")
    vid = data.get("video_output")
    if img:
        config["image_output"] = img
    if vid:
        config["video_output"] = vid
    os.makedirs(config["image_output"], exist_ok=True)
    os.makedirs(config["video_output"], exist_ok=True)
    persist_config(config)
    if camera:
        camera.timelapse.output_dir = config["image_output"]
        camera.recorder.output_dir = config["video_output"]
    return jsonify({"success": True})


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
