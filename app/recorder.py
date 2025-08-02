import os
import threading
import time
import queue
import cv2


class VideoRecorder:
    def __init__(self, camera, output_dir="videos"):
        self.camera = camera
        self.output_dir = output_dir
        self.recording = False
        self.thread = None
        self.writer = None
        self.output_file = None
        self.frame_queue = self.camera.register_raw_frame_queue()
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
            try:
                frame = self.frame_queue.get(timeout=1)
            except queue.Empty:
                continue
            if self.writer:
                self.writer.write(frame)
