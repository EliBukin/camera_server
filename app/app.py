from flask import Flask, Response, request, jsonify, render_template, url_for
import cv2
import time
import subprocess
import threading
import queue
import signal
import sys
import re
import os

# Allow this module to be executed directly as a script by adjusting
# the import paths when no parent package is detected.
if __package__ is None or __package__ == "":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..")))
    from utils.config import load_config, persist_config
    from camera.controller import ThreadSafeCameraController
else:
    from .utils.config import load_config, persist_config
    from .camera.controller import ThreadSafeCameraController

app = Flask(__name__)
camera = None
available_cameras = []
config = load_config()


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
                html += f"""
                <div class=\"control-group\">
                    <label>{name}</label>
                    <input type=\"range\" id=\"{name}\" min=\"{ctrl['min']}\" max=\"{ctrl['max']}\"
                        step=\"{ctrl['step']}\" value=\"{ctrl['current']}\"
                        oninput=\"updateControl('{name}', this.value)\" />
                    <span id=\"{name}-value\">{ctrl['current']}</span>
                </div>
                """
            elif ctrl['type'] == 'bool':
                checked = "checked" if ctrl['current'] == 1 else ""
                html += f"""
                <div class=\"control-group\">
                    <label>
                        <input type=\"checkbox\" id=\"{name}\" {checked}
                            onchange=\"updateControl('{name}', this.checked ? 1 : 0)\" />
                        {name}
                    </label>
                </div>
                """
            elif ctrl['type'] == 'menu':
                options = "".join(
                    f'<option value="{i}" {"selected" if i == ctrl["current"] else ""}>Option {i}</option>'
                    for i in range(ctrl['min'], ctrl['max'] + 1)
                )
                html += f"""
                <div class=\"control-group\">
                    <label>{name}</label>
                    <select id=\"{name}\" onchange=\"updateControl('{name}', this.value)\">
                        {options}
                    </select>
                </div>
                """
        return html

    html_int_controls = render_controls(int_controls)
    html_other_controls = render_controls(other_controls)

    return render_template(
        'index.html',
        options_html=options_html,
        camera_options_html=camera_options_html,
        html_int_controls=html_int_controls,
        html_other_controls=html_other_controls,
        config=config
    )


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
        current_marker = " \u2713" if (res_w == w and res_h == h) else ""
        resolution_list += f"<li>{fmt} - {res_w}x{res_h}{current_marker}</li>"

    return render_template(
        'status.html',
        current_resolution=f"{w}x{h}",
        resolution_list=resolution_list,
        controls_html=controls_html
    )


@app.route('/camera_status_json')
def camera_status_json():
    if not camera:
        return jsonify({"error": "Camera not initialized"}), 500

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
        "controls": camera.controls_info,
        "stored_defaults": camera.stored_defaults,
        "original_hardware_defaults": camera.original_hardware_defaults,
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
    if camera:
        config["controls"] = camera.get_all_current_values()
        w, h = camera.get_current_resolution()
        fmt = getattr(camera, "current_format", "MJPG")
        config["resolution"] = {"width": w, "height": h, "format": fmt}
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


def run_server():
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host='0.0.0.0', port=5000, threaded=True)


if __name__ == '__main__':
    run_server()
