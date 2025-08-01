import os
import threading
import time
import cv2


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
