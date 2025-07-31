from flask import Blueprint, Response, request, jsonify, render_template
import time

from .camera import (
    camera,
    available_cameras,
    discover_cameras,
    initialize_camera,
)

bp = Blueprint('web', __name__)

@bp.route('/')
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
    return render_template(
        "index.html",
        options_html=options_html,
        camera_options_html=camera_options_html,
        html_int_controls=html_int_controls,
        html_other_controls=html_other_controls,
    )

# NEW: Route to switch cameras
@bp.route('/set_camera', methods=['POST'])
def set_camera():
    """Switch active camera device"""
    global available_cameras
    data = request.get_json() or {}
    device = data.get('device')
    if not device:
        return jsonify({"success": False, "message": "Missing device"}), 400

    # refresh camera list to keep names up to date
    available_cameras = discover_cameras()

    if not initialize_camera(device):
        return jsonify({"success": False, "message": "Failed to initialize camera"}), 500
    return jsonify({"success": True})

# NEW: Route to set individual control values
@bp.route('/set_control', methods=['POST'])
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
@bp.route('/reset_controls', methods=['POST'])
def reset_controls():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500
    
    try:
        camera.reset_to_stored_defaults()
        return jsonify({"success": True, "message": "Controls reset to stored defaults"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error resetting controls: {str(e)}"}), 500

@bp.route('/start_timelapse', methods=['POST'])
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

@bp.route('/stop_timelapse', methods=['POST'])
def stop_timelapse():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        camera.timelapse.stop()
        return jsonify({"success": True, "message": "Timelapse stopped."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route('/start_recording', methods=['POST'])
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

@bp.route('/stop_recording', methods=['POST'])
def stop_recording():
    if not camera:
        return jsonify({"success": False, "message": "Camera not initialized"}), 500

    try:
        output = camera.recorder.stop()
        return jsonify({"success": True, "message": f"Recording saved to {output}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route('/camera_status')
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
    
    return render_template(
        "status.html",
        current_resolution=f"{w}x{h}",
        resolution_list=resolution_list,
        controls_html=controls_html,
    )


@bp.route('/camera_status_json')
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

@bp.route('/video_feed')
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

@bp.route('/set_resolution', methods=['POST'])
def set_resolution():
    if not camera:
        return jsonify({"message": "Camera not initialized"}), 500
    data = request.get_json()
    width = data.get("width", 640)
    height = data.get("height", 480)
    fmt = data.get("format", "MJPG")
    success, msg = camera.set_resolution(width, height, fmt)
    return jsonify({"success": success, "message": msg})

