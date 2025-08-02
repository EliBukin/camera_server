from flask import Flask, Response, request, jsonify, render_template
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


@app.route("/")
def index():
    if not camera:
        return "Camera not initialized", 500

    w, h = camera.get_current_resolution()
    current_resolution = f"{w}×{h}"
    
    # Generate resolution options
    options_html = "\n".join(
        f'<option value="{fmt},{w},{h}">{fmt} - {w}x{h}</option>'
        for fmt, w, h in camera.supported_resolutions
    )

    # Generate camera options
    camera_options_html = "\n".join(
        f'<option value="{dev}" {"selected" if dev == camera.device_path else ""}>{name}</option>'
        for name, dev in available_cameras
    )

    # Categorize controls
    basic_control_names = ['brightness', 'contrast', 'saturation', 'sharpness']
    advanced_control_names = [
        'gamma', 'gain', 'white_balance_temperature', 'exposure_time_absolute', 
        'focus_absolute', 'backlight_compensation', 'power_line_frequency'
    ]
    
    basic_controls = []
    advanced_controls = []
    
    for name, ctrl in camera.controls_info.items():
        if name in basic_control_names:
            basic_controls.append((name, ctrl))
        elif name in advanced_control_names or name not in basic_control_names:
            advanced_controls.append((name, ctrl))
    
    # Sort to ensure consistent order
    basic_controls.sort(key=lambda x: basic_control_names.index(x[0]) if x[0] in basic_control_names else 999)
    
    # Get auto mode states
    auto_white_balance_enabled = camera.controls_info.get('white_balance_automatic', {}).get('current', 1) == 1
    auto_exposure_mode = camera.controls_info.get('auto_exposure', {}).get('current', 1)

    return render_template(
        "index.html",
        options_html=options_html,
        camera_options_html=camera_options_html,
        basic_controls=basic_controls,
        advanced_controls=advanced_controls,
        auto_white_balance_enabled=auto_white_balance_enabled,
        auto_exposure_mode=auto_exposure_mode,
        current_resolution=current_resolution,
        config=config,
    )


@app.route("/set_camera", methods=["POST"])
def set_camera():
    global available_cameras
    data = request.get_json() or {}
    device = data.get("device")
    if not device:
        return jsonify({"success": False, "message": "Missing device"}), 400

    available_cameras = discover_cameras()
    if not initialize_camera(device):
        return (
            jsonify({"success": False, "message": "Failed to initialize camera"}),
            500,
        )
    return jsonify({"success": True})


@app.route("/set_control", methods=["POST"])
def set_control():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    data = request.get_json()
    control_name = data.get("control")
    value = data.get("value")

    if not control_name or value is None:
        return (
            jsonify({"success": False, "message": "Missing control name or value"}),
            400,
        )

    success = camera.set_control_value(control_name, value)

    if success:
        return jsonify({"success": True, "message": f"Set {control_name} = {value}"})
    else:
        return (
            jsonify({"success": False, "message": f"Failed to set {control_name}"}),
            500,
        )


@app.route("/reset_controls", methods=["POST"])
def reset_controls():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        camera.reset_to_stored_defaults()
        return jsonify(
            {"success": True, "message": "Controls reset to stored defaults"}
        )
    except Exception as e:
        return (
            jsonify(
                {"success": False, "message": f"Error resetting controls: {str(e)}"}
            ),
            500,
        )


@app.route("/start_timelapse", methods=["POST"])
def start_timelapse():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    data = request.get_json()
    interval = data.get("interval", 5)
    try:
        camera.timelapse.start(interval)
        return jsonify(
            {
                "success": True,
                "message": f"Timelapse started with {interval}s interval.",
            }
        )
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/stop_timelapse", methods=["POST"])
def stop_timelapse():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        camera.timelapse.stop()
        return jsonify({"success": True, "message": "Timelapse stopped."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/start_recording", methods=["POST"])
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


@app.route("/stop_recording", methods=["POST"])
def stop_recording():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        output = camera.recorder.stop()
        return jsonify({"success": True, "message": f"Recording saved to {output}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/camera_status")
def camera_status():
    if not camera:
        return "Camera not initialized", 500

    w, h = camera.get_current_resolution()
    controls_html = ""
    for name, ctrl in camera.controls_info.items():
        stored_default = camera.stored_defaults.get(name, "N/A")
        original_hw_default = (
            camera.original_hardware_defaults.get(name, "N/A")
            if hasattr(camera, "original_hardware_defaults")
            else "N/A"
        )
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

    return render_template(
        "status.html",
        current_resolution=f"{w}x{h}",
        resolution_list=resolution_list,
        controls_html=controls_html,
    )


@app.route("/camera_status_json")
def camera_status_json():
    if not camera:
        return jsonify({"error": "Camera not initialized"}), 500

    w, h = camera.get_current_resolution()
    status = {
        "device": camera.device_path,
        "current_resolution": {"width": w, "height": h},
        "supported_resolutions": [
            {"format": fmt, "width": w, "height": h}
            for fmt, w, h in camera.supported_resolutions
        ],
        "controls": camera.controls_info,
        "stored_defaults": camera.stored_defaults,
        "original_hardware_defaults": camera.original_hardware_defaults,
        "timestamp": time.time(),
    }
    return jsonify(status)


@app.route("/video_feed")
def video_feed():
    def generate():
        while True:
            if not camera:
                time.sleep(0.1)
                continue
            frame = camera.get_frame_blocking()
            if frame:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/set_resolution", methods=["POST"])
def set_resolution():
    if not camera:
        return jsonify({"message": "Camera not initialized"}), 500
    data = request.get_json()
    width = data.get("width", 640)
    height = data.get("height", 480)
    fmt = data.get("format", "MJPG")
    success, msg = camera.set_resolution(width, height, fmt)
    return jsonify({"success": success, "message": msg})


@app.route("/capture_photo", methods=["POST"])
def capture_photo_route():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    data = request.get_json() or {}
    path = data.get("path")
    if not path:
        return jsonify({"success": False, "message": "Missing path"}), 400
    try:
        output = camera.capture_photo(path)
        return jsonify({"success": True, "message": f"Captured {output}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/save_config", methods=["POST"])
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

if __name__ == "__main__":
    print("🚀 Camera server starting at http://0.0.0.0:5000")
    if not initialize_camera():
        print("Camera failed to initialize.")
        sys.exit(1)
    app.run(host="0.0.0.0", port=5000, threaded=True)
