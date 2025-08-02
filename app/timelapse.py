import os
import threading
import time
import queue
import cv2


class TimeLapseCapturer:
    def __init__(self, camera, output_dir="timelapse"):
        self.camera = camera
        self.output_dir = output_dir
        self.interval = 5
        self.running = False
        self.thread = None
        self.frame_queue = self.camera.register_raw_frame_queue()
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
        next_capture = time.time()
        while self.running:
            try:
                frame = self.frame_queue.get(timeout=1)
            except queue.Empty:
                continue
            now = time.time()
            if now >= next_capture:
                filename = os.path.join(self.output_dir, f"frame_{count:05d}.jpg")
                cv2.imwrite(filename, frame)
                print(f"[TimeLapse] Captured: {filename}")
                count += 1
                next_capture = now + self.interval
