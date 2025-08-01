# USB Camera Server

A Python Flask web application that provides real-time streaming and control of USB cameras through a web interface. Features live MJPEG streaming, camera control adjustments, resolution switching, and timelapse capture functionality.

## 🚀 Features

- **Real-time MJPEG Streaming**: Live camera feed accessible via web browser
- **Camera Control Interface**: Adjust exposure, brightness, contrast, and other camera settings
- **Resolution Management**: Switch between supported camera resolutions dynamically  
- **Timelapse Capture**: Automated interval-based photo capture
- **Thread-Safe Operations**: Concurrent camera access and streaming
- **Auto-Detection**: Automatically detects supported resolutions and camera controls
- **Smart Defaults**: Calculates sensible default values for camera controls
- **REST API**: JSON endpoints for programmatic control
- **Status Monitoring**: Real-time camera status and settings view

## 📋 Requirements

### System Dependencies
- Linux-based system (tested on Ubuntu/Debian)
- USB Video Class (UVC) compatible camera
- `v4l2-ctl` (Video4Linux utilities)

### Python Dependencies
- Python 3.6+
- Flask
- OpenCV (cv2)
- NumPy (included with OpenCV)

## 🔧 Installation

### 1. Install System Dependencies

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install v4l-utils python3-pip
```

**CentOS/RHEL/Fedora:**
```bash
sudo yum install v4l-utils python3-pip
# or
sudo dnf install v4l-utils python3-pip
```

### 2. Install Python Dependencies

```bash
pip3 install flask opencv-python
```

### 3. Clone and Setup

```bash
git clone https://github.com/yourusername/usb-camera-server.git
cd usb-camera-server
```

### 4. Verify Camera Connection

```bash
# List connected cameras
v4l2-ctl --list-devices

# Test your camera (replace /dev/video1 with your device)
v4l2-ctl --device=/dev/video1 --list-formats-ext
```

## 🎯 Usage

### Basic Usage

1. **Start the server:**
   ```bash
   python3 app/server.py
   ```

2. **Access the web interface:**
   Open your browser to `http://localhost:5000`

3. **View camera stream and controls:**
   - Live video feed on the left
   - Camera controls (sliders, checkboxes, dropdowns) on the right
   - Resolution selector and reset button at the top

### Multiple Cameras

The server automatically detects all available cameras using `v4l2-ctl`. A drop-down menu on the main page lets you switch between them by name. The first detected device is used by default when the application starts.

### Timelapse Capture

1. Set desired interval in seconds
2. Click "Start Timelapse" 
3. Images are saved to `./timelapse/` directory
4. Click "Stop Timelapse" to end capture

### Video Recording

1. Click "Start Recording" to begin capturing video
2. The recorded file is saved under `./videos/` directory
3. Click "Stop Recording" to end capture

## 📡 API Endpoints

### Web Interface
- `GET /` - Main camera interface
- `GET /video_feed` - MJPEG video stream
- `GET /camera_status` - HTML status page
- `GET /camera_status_json` - JSON status data

### Camera Control
- `POST /set_control` - Set individual camera control
  ```json
  {
    "control": "brightness", 
    "value": 128
  }
  ```

- `POST /reset_controls` - Reset all controls to defaults

### Resolution Management  
- `POST /set_resolution` - Change camera resolution
  ```json
  {
    "width": 1920,
    "height": 1080, 
    "format": "MJPG"
  }
  ```

### Timelapse Control
- `POST /start_timelapse` - Start timelapse capture
  ```json
  {
    "interval": 5
  }
  ```

- `POST /stop_timelapse` - Stop timelapse capture
- `POST /start_recording` - Begin video recording
- `POST /stop_recording` - Stop video recording

## ⚙️ Configuration

### Camera Controls

The application automatically detects and categorizes camera controls:

- **Integer Controls**: Brightness, contrast, saturation, etc. (displayed as sliders)
- **Boolean Controls**: Auto-focus, auto-exposure, etc. (displayed as checkboxes)  
- **Menu Controls**: White balance, exposure modes, etc. (displayed as dropdowns)

### Default Values

The application calculates intelligent defaults for camera controls:
- **Integer controls**: Set to middle value between min/max
- **Boolean controls**: Set to minimum value (usually off/false)
- **Menu controls**: Set to first option (with special handling for auto_exposure)

### Resolution Support

Supported resolutions are auto-detected using `v4l2-ctl`. The application will:
1. Query all available formats and resolutions
2. Set the first available resolution on startup
3. Allow switching between any supported resolution

## 🔍 Troubleshooting

### Common Issues

**Camera not found:**
```bash
# Check if camera is detected
lsusb | grep -i camera
v4l2-ctl --list-devices
```

**Permission denied:**
```bash
# Add user to video group
sudo usermod -a -G video $USER
# Log out and back in, or:
newgrp video
```

**v4l2-ctl not found:**
```bash
# Install Video4Linux utilities
sudo apt install v4l-utils
```

**OpenCV installation issues:**
```bash
# Alternative OpenCV installation
pip3 install opencv-python-headless
```

### Camera Control Issues

Some cameras may have limited or non-standard controls. The application handles this by:
- Validating control values against min/max bounds
- Skipping unsupported controls
- Providing fallback defaults for problematic controls

### Performance Optimization

For better performance:
- Use MJPEG format when available (reduces CPU usage)
- Lower resolution for slower systems
- Adjust frame rate in `_set_camera_properties()` method

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with Flask and OpenCV
- Uses Video4Linux utilities for camera control
- Inspired by the need for simple USB camera control and streaming

## 🔗 Related Projects

- [OpenCV Documentation](https://docs.opencv.org/)
- [Flask Documentation](https://flask.palletsprojects.com/)
- [Video4Linux Documentation](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/v4l2.html)

---

**Note**: This application is designed for Linux systems with UVC-compatible USB cameras. For other platforms or camera types, modifications may be required.