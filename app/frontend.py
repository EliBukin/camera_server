from flask import render_template_string
from backend import app, camera, available_cameras, config

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
                    <div class="control-group" style="display: flex; gap: 10px; margin-top: 10px;">
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
