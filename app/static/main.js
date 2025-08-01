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

