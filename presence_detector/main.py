import base64
import json
import os
import threading
import time
import uuid
from io import BytesIO

os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"

import cv2
import mediapipe as mp
import numpy as np
import requests

FIREBASE_BASE_URL = "https://smart-energy-4f0a2-default-rtdb.asia-southeast1.firebasedatabase.app"

FIREBASE_RELAY_URL      = f"{FIREBASE_BASE_URL}/device_status/relay_on.json"
FIREBASE_FAN_URL        = f"{FIREBASE_BASE_URL}/device_status/fan_on.json"
FIREBASE_PRESENCE_URL   = f"{FIREBASE_BASE_URL}/device_status/presence_detected.json"
FIREBASE_SUGGESTION_URL = f"{FIREBASE_BASE_URL}/suggestions/presence.json"
FIREBASE_LIGHT_URL      = f"{FIREBASE_BASE_URL}/device_status/room_is_dark.json"
FIREBASE_CAMERAS_URL    = f"{FIREBASE_BASE_URL}/cameras.json"

TARIFF_PER_KWH      = 7.0
LIGHT_WATTAGE       = 60
TIMEOUT_SECONDS     = 5.0
FRAME_SEND_INTERVAL = 2.0
CAMERA_POLL_SECONDS = 30
STAGGER_SECONDS     = 2.0
JPEG_QUALITY        = 60
SNAPSHOT_TIMEOUT    = 5
MAX_SNAPSHOT_DIM    = 640

POSE_MODEL_PATH          = "pose_landmarker_lite.task"
POSE_MAX_PERSONS         = 5
POSE_MIN_DETECTION_CONF  = 0.5
POSE_MIN_PRESENCE_CONF   = 0.5
POSE_MIN_VISIBLE_POINTS  = 5
POSE_VISIBILITY_THRESH   = 0.5

DEMO_PRESENCE_ON_SECONDS  = 15
DEMO_PRESENCE_OFF_SECONDS = 10

cameras_lock = threading.Lock()
active_camera_ids: set = set()
stop_event    = threading.Event()

relay_is_on         = False
last_detection_time = 0.0
relay_on_no_presence_since: float | None = None
last_pushed_message = None
state_lock = threading.Lock()

BaseOptions          = mp.tasks.BaseOptions
PoseLandmarker       = mp.tasks.vision.PoseLandmarker
PoseLandmarkerOptions = mp.tasks.vision.PoseLandmarkerOptions
VisionRunningMode    = mp.tasks.vision.RunningMode


def firebase_get(url: str, default=None):
    try:
        response = requests.get(url, timeout=4)
        value = response.json()
        return value if value is not None else default
    except Exception:
        return default


def firebase_put(url: str, data):
    try:
        requests.put(url, json=data, timeout=6)
    except Exception as error:
        print(f"  [Firebase PUT error] {url}: {error}")


def firebase_patch(url: str, data: dict):
    try:
        requests.patch(url, json=data, timeout=8)
    except Exception as error:
        print(f"  [Firebase PATCH error] {url}: {error}")


def firebase_delete(url: str):
    try:
        requests.delete(url, timeout=4)
    except Exception:
        pass


def fetch_cameras() -> dict:
    raw = firebase_get(FIREBASE_CAMERAS_URL, default={})
    if not isinstance(raw, dict):
        return {}
    result = {}
    for camera_id, camera_data in raw.items():
        if not isinstance(camera_data, dict):
            continue
        appliances = []
        if isinstance(camera_data.get("appliances"), dict):
            appliances = [
                key for key, val in camera_data["appliances"].items() if val
            ]
        elif isinstance(camera_data.get("appliance"), str) and camera_data["appliance"] != "none":
            appliances = [camera_data["appliance"]]

        result[camera_id] = {
            "name":       camera_data.get("name", camera_id),
            "url":        camera_data.get("url", ""),
            "appliances": appliances,
        }
    return result


def fetch_frame_http(base_url: str) -> np.ndarray | None:
    shot_url = base_url.rstrip("/") + "/shot.jpg"
    try:
        response = requests.get(shot_url, timeout=SNAPSHOT_TIMEOUT, stream=False)
        if response.status_code == 200:
            image_array = np.frombuffer(response.content, dtype=np.uint8)
            frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            return frame
    except Exception:
        pass
    return None


def fetch_frame_rtsp(capture_holder: list) -> np.ndarray | None:
    capture: cv2.VideoCapture = capture_holder[0]
    if capture is None or not capture.isOpened():
        return None
    try:
        capture.grab()
        success, frame = capture.retrieve()
        if success:
            return frame
    except Exception:
        pass
    return None


def open_video_capture(url: str) -> cv2.VideoCapture | None:
    source = int(url) if url.isdigit() else url
    capture = cv2.VideoCapture(source)
    if capture.isOpened():
        return capture
    return None


def resize_frame(frame: np.ndarray, max_dimension: int = MAX_SNAPSHOT_DIM) -> np.ndarray:
    height, width = frame.shape[:2]
    if width <= max_dimension:
        return frame
    scale = max_dimension / width
    return cv2.resize(frame, (max_dimension, int(height * scale)), interpolation=cv2.INTER_AREA)


def frame_to_base64(frame: np.ndarray) -> str:
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    _, buffer = cv2.imencode(".jpg", frame, encode_param)
    return base64.b64encode(buffer.tobytes()).decode("utf-8")


def is_demo_url(url: str) -> bool:
    return url.lower().startswith("demo://")


def get_demo_scene_type(url: str) -> str:
    return url.replace("demo://", "").strip().lower()


def _draw_person(frame, px, py, sway, skin=(180, 160, 140), body=(120, 100, 80)):
    head_y = py - 30
    cv2.circle(frame, (px + sway, head_y), 12, skin, -1)
    cv2.line(frame, (px + sway, head_y + 12), (px + sway, py + 10), body, 3)
    cv2.line(frame, (px + sway, py - 10), (px + sway - 18, py + 5), body, 2)
    cv2.line(frame, (px + sway, py - 10), (px + sway + 18, py + 5), body, 2)
    cv2.line(frame, (px + sway, py + 10), (px + sway - 12, py + 40), body, 2)
    cv2.line(frame, (px + sway, py + 10), (px + sway + 12, py + 40), body, 2)


def render_demo_frame(scene_type: str, person_present: bool, frame_idx: int) -> np.ndarray:
    W, H = 640, 480
    frame = np.zeros((H, W, 3), dtype=np.uint8)

    if scene_type == "classroom":
        frame[:] = (45, 40, 35)
        cv2.rectangle(frame, (0, 0), (W, 80), (55, 50, 42), -1)
        cv2.rectangle(frame, (0, H - 100), (W, H), (60, 55, 48), -1)
        cv2.rectangle(frame, (50, 15), (350, 70), (200, 200, 200), -1)
        cv2.rectangle(frame, (50, 15), (350, 70), (180, 180, 180), 2)
        cv2.putText(frame, "WHITEBOARD", (120, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 2)
        cv2.rectangle(frame, (400, 20), (590, 65), (30, 80, 30), -1)
        cv2.putText(frame, "EXIT", (470, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (50, 200, 50), 2)
        for dx, dy in [(80,200),(280,200),(480,200),(80,320),(280,320),(480,320)]:
            cv2.rectangle(frame, (dx-40, dy), (dx+40, dy+30), (90, 75, 60), -1)
            cv2.rectangle(frame, (dx-40, dy), (dx+40, dy+30), (70, 58, 45), 2)
            cv2.rectangle(frame, (dx-15, dy+30), (dx+15, dy+55), (80, 70, 55), -1)
        for lx in [160, 320, 480]:
            cv2.circle(frame, (lx, 5), 12, (180, 200, 220), -1)
            cv2.circle(frame, (lx, 5), 8, (220, 240, 255), -1)
        if person_present:
            for i, (px, py) in enumerate([(100,170),(300,170),(490,170),(100,290),(300,290)]):
                _draw_person(frame, px, py, int(3 * np.sin(frame_idx * 0.1 + i * 1.5)))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.rectangle(frame, (0, H-28), (250, H), (0, 0, 0), -1)
        cv2.putText(frame, f"CAM-01 CLASSROOM  {ts}", (8, H-8), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200,200,200), 1)
        blink = (0,0,255) if frame_idx % 10 < 7 else (80,80,80)
        cv2.putText(frame, "REC", (W-45, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, blink, 1)
        cv2.circle(frame, (W-55, 16), 4, blink, -1)

    elif scene_type == "office":
        frame[:] = (50, 45, 40)
        cv2.rectangle(frame, (0, 0), (W, 60), (60, 55, 48), -1)
        cv2.rectangle(frame, (0, H-80), (W, H), (65, 58, 50), -1)
        for dx, dy, dw, dh in [(60,180,200,60),(320,180,200,60),(60,330,200,60),(320,330,200,60)]:
            cv2.rectangle(frame, (dx, dy), (dx+dw, dy+dh), (100, 85, 70), -1)
            cv2.rectangle(frame, (dx, dy), (dx+dw, dy+dh), (80, 68, 55), 2)
        for mx, my, mw, mh in [(110,155,60,30),(370,155,60,30),(110,305,60,30),(370,305,60,30)]:
            cv2.rectangle(frame, (mx, my), (mx+mw, my+mh), (40, 40, 45), -1)
            cv2.rectangle(frame, (mx+3, my+3), (mx+mw-3, my+mh-3), (60, 80, 100), -1)
            cv2.line(frame, (mx+mw//2, my+mh), (mx+mw//2, my+mh+8), (80, 80, 80), 2)
        cv2.rectangle(frame, (540, 100), (620, 400), (45, 75, 45), -1)
        cv2.ellipse(frame, (580, 95), (35, 25), 0, 0, 360, (50, 120, 50), -1)
        cv2.rectangle(frame, (500, 10), (600, 50), (20, 20, 20), -1)
        cv2.putText(frame, "AC", (530, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 200, 255), 2)
        for lx in [160, 420]:
            cv2.circle(frame, (lx, 5), 14, (180, 200, 220), -1)
            cv2.circle(frame, (lx, 5), 9, (220, 240, 255), -1)
        if person_present:
            for i, (px, py) in enumerate([(140,150),(400,150),(140,300)]):
                _draw_person(frame, px, py, int(2*np.sin(frame_idx*0.08+i*2.0)), (180,160,140), (130,110,90))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.rectangle(frame, (0, H-28), (250, H), (0, 0, 0), -1)
        cv2.putText(frame, f"CAM-02 OFFICE  {ts}", (8, H-8), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200,200,200), 1)
        blink = (0,0,255) if frame_idx % 10 < 7 else (80,80,80)
        cv2.putText(frame, "REC", (W-45, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, blink, 1)
        cv2.circle(frame, (W-55, 16), 4, blink, -1)
    else:
        frame[:] = (40, 40, 40)
        cv2.putText(frame, f"DEMO: {scene_type}", (50, H//2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200,200,200), 2)

    return frame


def annotate_demo_detections(frame, scene_type, camera_name, person_present):
    if person_present:
        boxes = {"classroom": [(62,130,138,210),(262,130,338,210),(452,130,528,210),(62,250,138,330),(262,250,338,330)],
                 "office":    [(102,112,178,185),(362,112,438,185),(102,262,178,335)]}
        for i, (x1,y1,x2,y2) in enumerate(boxes.get(scene_type, [(160,120,480,360)])):
            conf = round(0.82 + 0.03 * (i % 3), 2)
            cv2.rectangle(frame, (x1,y1), (x2,y2), (57,255,20), 2)
            cv2.putText(frame, f"Person {conf:.0%}", (x1, max(y1-8,0)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (57,255,20), 2)

    cv2.rectangle(frame, (0, 0), (frame.shape[1], 30), (0, 0, 0), -1)
    status_text = "PERSON DETECTED" if person_present else "No Person"
    status_color = (57, 255, 20) if person_present else (180, 180, 180)
    cv2.putText(frame, f"{camera_name}  |  {status_text}", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
    return frame


def demo_camera_worker(camera_id: str, stagger_delay: float):
    print(f"[Demo Camera {camera_id}] Starting in {stagger_delay:.1f} s ...")
    time.sleep(stagger_delay)

    def get_camera_config():
        return fetch_cameras().get(camera_id, {})

    config = get_camera_config()
    scene_type = get_demo_scene_type(config.get("url", ""))
    cycle_total = DEMO_PRESENCE_ON_SECONDS + DEMO_PRESENCE_OFF_SECONDS
    frame_idx = 0
    appliance_state_on = False
    last_person_detection_time = 0.0

    print(f"[Demo Camera {camera_id}] scene='{scene_type}' cycle={DEMO_PRESENCE_ON_SECONDS}s on / {DEMO_PRESENCE_OFF_SECONDS}s off")

    while not stop_event.is_set():
        loop_start = time.time()
        config = get_camera_config()
        if not config:
            print(f"[Demo Camera {camera_id}] Removed. Stopping.")
            with cameras_lock:
                active_camera_ids.discard(camera_id)
            break

        appliances = config.get("appliances", [])
        camera_name = config.get("name", camera_id)
        scene_type = get_demo_scene_type(config.get("url", ""))

        person_present = (time.time() % cycle_total) < DEMO_PRESENCE_ON_SECONDS
        frame = render_demo_frame(scene_type, person_present, frame_idx)
        frame = annotate_demo_detections(frame, scene_type, camera_name, person_present)
        frame_idx += 1

        firebase_patch(f"{FIREBASE_BASE_URL}/cameras/{camera_id}.json", {
            "snapshot_b64":    frame_to_base64(frame),
            "person_detected": person_present,
            "last_updated_ms": int(time.time() * 1000),
            "status":          "ok",
        })

        current_time = time.time()
        if person_present:
            last_person_detection_time = current_time
            firebase_put(FIREBASE_PRESENCE_URL, True)
            
            for appliance in appliances:
                if appliance == "relay":
                    if fetch_room_is_dark():
                        set_appliance("relay", True)
                        clear_suggestion()
                    else:
                        push_suggestion("Presence detected but room is bright enough. Saving energy.", "info")
                elif appliance in APPLIANCE_URLS:
                    set_appliance(appliance, True)
            appliance_state_on = True
        else:
            if (current_time - last_person_detection_time) > TIMEOUT_SECONDS and appliance_state_on:
                firebase_put(FIREBASE_PRESENCE_URL, False)
                for appliance in appliances:
                    if appliance in APPLIANCE_URLS:
                        set_appliance(appliance, False)
                appliance_state_on = False

        print(f"[Demo {camera_id}] {scene_type} {'PERSON' if person_present else 'empty'} "
              f"appliances={appliances} cycle={time.time()-loop_start:.1f}s")
        time.sleep(max(0.5, FRAME_SEND_INTERVAL - (time.time() - loop_start)))

    print(f"[Demo Camera {camera_id}] Stopped.")


APPLIANCE_URLS = {
    "relay": FIREBASE_RELAY_URL,
    "fan":   FIREBASE_FAN_URL,
}


def set_appliance(appliance: str, state: bool):
    global relay_is_on
    url = APPLIANCE_URLS.get(appliance)
    if url:
        firebase_put(url, state)
        if appliance == "relay":
            with state_lock:
                relay_is_on = state
        print(f"  [{appliance.upper()}] → {'ON' if state else 'OFF'}")


def fetch_room_is_dark() -> bool:
    value = firebase_get(FIREBASE_LIGHT_URL, default=True)
    return bool(value)


def push_suggestion(message: str, suggestion_type: str = "warning"):
    global last_pushed_message
    with state_lock:
        if last_pushed_message == message:
            return
        last_pushed_message = message
        no_presence_since = relay_on_no_presence_since

    idle_seconds   = (time.time() - no_presence_since) if no_presence_since else 0
    idle_minutes   = max(1, int(idle_seconds / 60))
    wasted_watt_hours  = (LIGHT_WATTAGE * idle_seconds) / 3600.0
    wasted_cost = (wasted_watt_hours / 1000.0) * TARIFF_PER_KWH

    firebase_put(FIREBASE_SUGGESTION_URL, {
        "message":       message,
        "type":          suggestion_type,
        "idle_minutes":  idle_minutes,
        "wasted_cost_rs": round(wasted_cost, 4),
        "timestamp":     int(time.time() * 1000),
    })
    print(f"  [Suggestion → Firebase] {suggestion_type}: {message}")


def clear_suggestion():
    global last_pushed_message
    with state_lock:
        last_pushed_message = None
    firebase_delete(FIREBASE_SUGGESTION_URL)


def create_pose_detector() -> PoseLandmarker:
    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=POSE_MODEL_PATH),
        running_mode=VisionRunningMode.IMAGE,
        num_poses=POSE_MAX_PERSONS,
        min_pose_detection_confidence=POSE_MIN_DETECTION_CONF,
        min_pose_presence_confidence=POSE_MIN_PRESENCE_CONF,
    )
    return PoseLandmarker.create_from_options(options)


def detect_persons(detector: PoseLandmarker, frame: np.ndarray) -> list[dict]:
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    result = detector.detect(mp_image)

    detections = []
    height, width = frame.shape[:2]

    for pose_landmarks in result.pose_landmarks:
        visible_landmarks = [
            lm for lm in pose_landmarks
            if lm.visibility > POSE_VISIBILITY_THRESH
        ]

        if len(visible_landmarks) < POSE_MIN_VISIBLE_POINTS:
            continue

        x_coords = [int(lm.x * width) for lm in visible_landmarks]
        y_coords = [int(lm.y * height) for lm in visible_landmarks]

        padding_x = int(0.12 * (max(x_coords) - min(x_coords) + 1))
        padding_y = int(0.18 * (max(y_coords) - min(y_coords) + 1))

        x1 = max(0, min(x_coords) - padding_x)
        y1 = max(0, min(y_coords) - padding_y)
        x2 = min(width, max(x_coords) + padding_x)
        y2 = min(height, max(y_coords) + padding_y)

        avg_visibility = sum(lm.visibility for lm in visible_landmarks) / len(visible_landmarks)

        detections.append({
            "bbox": (x1, y1, x2, y2),
            "confidence": round(avg_visibility, 2),
            "visible_points": len(visible_landmarks),
        })

    return detections


def camera_worker(camera_id: str, stagger_delay: float):
    print(f"[Camera {camera_id}] Starting in {stagger_delay:.1f} s ...")
    time.sleep(stagger_delay)

    detector = create_pose_detector()
    print(f"[Camera {camera_id}] MediaPipe Pose detector created.")

    camera_url_base = ""
    is_http = False
    capture_holder = [None]
    last_person_detection_time = 0.0
    appliance_state_on  = False

    def get_camera_config():
        cameras = fetch_cameras()
        return cameras.get(camera_id, {})

    config = get_camera_config()
    camera_url_base = config.get("url", "")
    is_http = "shot.jpg" in camera_url_base or (
        not camera_url_base.startswith("rtsp") and not camera_url_base.endswith("/video") and not camera_url_base.isdigit()
    )

    if not is_http:
        stream_url = camera_url_base if (camera_url_base.isdigit() or camera_url_base.startswith("rtsp")) else (camera_url_base.rstrip("/") + "/video" if not camera_url_base.endswith("/video") else camera_url_base)
        print(f"[Camera {camera_id}] Opening MJPEG/RTSP/Webcam stream: {stream_url}")
        capture_holder[0] = open_video_capture(stream_url)
        if capture_holder[0] is None:
            print(f"[Camera {camera_id}] WARNING: Could not open stream. Retrying in 10s ...")

    print(f"[Camera {camera_id}] Running (MediaPipe Pose) — appliances={config.get('appliances', [])}")

    consecutive_failures = 0
    MAX_BACKOFF       = 30

    while not stop_event.is_set():
        loop_start = time.time()

        config = get_camera_config()

        if not config:
            print(f"[Camera {camera_id}] Removed from Firebase. Stopping thread.")
            with cameras_lock:
                active_camera_ids.discard(camera_id)
            break

        appliances = config.get("appliances", [])
        camera_name  = config.get("name", camera_id)

        new_url = config.get("url", "")
        if new_url != camera_url_base:
            camera_url_base = new_url
            is_http = "shot.jpg" in camera_url_base or (
                not camera_url_base.startswith("rtsp") and not camera_url_base.endswith("/video") and not camera_url_base.isdigit()
            )
            if not is_http:
                if capture_holder[0]:
                    capture_holder[0].release()
                stream_url = camera_url_base if (camera_url_base.isdigit() or camera_url_base.startswith("rtsp")) else (camera_url_base.rstrip("/") + "/video" if not camera_url_base.endswith("/video") else camera_url_base)
                capture_holder[0] = open_video_capture(stream_url)
            consecutive_failures = 0

        frame = None
        if is_http:
            frame = fetch_frame_http(camera_url_base)
            if frame is None and capture_holder[0] is None and consecutive_failures < 3:
                stream_url = camera_url_base if (camera_url_base.isdigit() or camera_url_base.startswith("rtsp")) else (camera_url_base.rstrip("/") + "/video" if not camera_url_base.endswith("/video") else camera_url_base)
                capture_holder[0] = open_video_capture(stream_url)
            if frame is None and capture_holder[0] is not None:
                frame = fetch_frame_rtsp(capture_holder)
        else:
            if capture_holder[0] is None and consecutive_failures < 3:
                stream_url = camera_url_base if (camera_url_base.isdigit() or camera_url_base.startswith("rtsp")) else (camera_url_base.rstrip("/") + "/video" if not camera_url_base.endswith("/video") else camera_url_base)
                capture_holder[0] = open_video_capture(stream_url)
            if capture_holder[0] is not None:
                frame = fetch_frame_rtsp(capture_holder)

        if frame is None:
            consecutive_failures += 1
            backoff = min(FRAME_SEND_INTERVAL * consecutive_failures, MAX_BACKOFF)
            if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                print(f"[Camera {camera_id}] No frame (attempt {consecutive_failures}), "
                      f"retry in {backoff:.0f}s")
            if get_camera_config():
                firebase_put(f"{FIREBASE_BASE_URL}/cameras/{camera_id}/status.json", "no_signal")
            if capture_holder[0]:
                capture_holder[0].release()
                capture_holder[0] = None
            time.sleep(backoff)
            continue

        consecutive_failures = 0

        frame = resize_frame(frame)

        person_detected = False
        detections = detect_persons(detector, frame)

        for detection in detections:
            person_detected = True
            x1, y1, x2, y2 = detection["bbox"]
            confidence = detection["confidence"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (57, 255, 20), 2)
            cv2.putText(frame, f"Person {confidence:.0%}", (x1, max(y1 - 8, 0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (57, 255, 20), 2)

        cv2.rectangle(frame, (0, 0), (frame.shape[1], 30), (0, 0, 0), -1)
        status_text = "PERSON DETECTED" if person_detected else "No Person"
        status_color = (57, 255, 20) if person_detected else (180, 180, 180)
        cv2.putText(frame, f"{camera_name}  |  {status_text}",
                    (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)

        base64_snapshot = frame_to_base64(frame)
        now_milliseconds = int(time.time() * 1000)
        firebase_patch(f"{FIREBASE_BASE_URL}/cameras/{camera_id}.json", {
            "snapshot_b64":    base64_snapshot,
            "person_detected": person_detected,
            "last_updated_ms": now_milliseconds,
            "status":          "ok",
        })

        current_time = time.time()
        if person_detected:
            last_person_detection_time = current_time
            firebase_put(FIREBASE_PRESENCE_URL, True)

            for appliance in appliances:
                if appliance == "relay":
                    if fetch_room_is_dark():
                        set_appliance("relay", True)
                        clear_suggestion()
                    else:
                        push_suggestion(
                            "Presence detected but room is bright enough. Saving energy.",
                            "info",
                        )
                elif appliance in APPLIANCE_URLS:
                    set_appliance(appliance, True)

            appliance_state_on = True
        else:
            time_since_last_seen = current_time - last_person_detection_time
            if time_since_last_seen > TIMEOUT_SECONDS and appliance_state_on:
                firebase_put(FIREBASE_PRESENCE_URL, False)
                for appliance in appliances:
                    if appliance in APPLIANCE_URLS:
                        set_appliance(appliance, False)
                appliance_state_on = False

        print(f"[Camera {camera_id}] ({'PERSON' if person_detected else 'empty'}) "
              f"appliances={appliances} cycle={time.time()-loop_start:.1f}s")

        elapsed = time.time() - loop_start
        sleep_time = max(0.5, FRAME_SEND_INTERVAL - elapsed)
        time.sleep(sleep_time)

    detector.close()
    if capture_holder[0]:
        capture_holder[0].release()
    print(f"[Camera {camera_id}] Stopped.")


def camera_manager():
    while not stop_event.is_set():
        cameras = fetch_cameras()

        with cameras_lock:
            new_ids = set(cameras.keys()) - active_camera_ids
            for index, camera_id in enumerate(sorted(new_ids)):
                if not cameras[camera_id].get('url'):
                    print(f"[Manager] Skipping {camera_id} — no URL, cleaning up.")
                    firebase_delete(f"{FIREBASE_BASE_URL}/cameras/{camera_id}.json")
                    continue
                delay = index * STAGGER_SECONDS
                print(f"[Manager] New camera detected: {cameras[camera_id]['name']} (stagger={delay:.0f}s)")
                is_demo = is_demo_url(cameras[camera_id].get('url', ''))
                thread = threading.Thread(
                    target=demo_camera_worker if is_demo else camera_worker,
                    args=(camera_id, delay),
                    daemon=True,
                    name=f"cam-{camera_id[:8]}",
                )
                thread.start()
                active_camera_ids.add(camera_id)

        time.sleep(CAMERA_POLL_SECONDS)


def main():
    print("=" * 55)
    print("  PRESENCE DETECTOR — MediaPipe Pose (Lite)")
    print("  Fast CPU inference • No GPU required")
    print("  Detecting up to", POSE_MAX_PERSONS, "persons per frame")
    print("=" * 55)

    if not os.path.exists(POSE_MODEL_PATH):
        print(f"\nERROR: Model file '{POSE_MODEL_PATH}' not found.")
        print("Download it with:")
        print("  python -c \"import urllib.request; urllib.request.urlretrieve("
              "'https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
              "pose_landmarker_lite/float16/1/pose_landmarker_lite.task', "
              f"'{POSE_MODEL_PATH}')\"")
        return

    print("\nFetching camera list from Firebase ...")
    cameras = fetch_cameras()

    if not cameras:
        print(
            "No cameras found in Firebase (/cameras).\n"
            "Add a camera via the Flutter app and it will be picked up within "
            f"{CAMERA_POLL_SECONDS} seconds.\n"
            "Waiting ..."
        )
    else:
        print(f"Found {len(cameras)} camera(s): {[v['name'] for v in cameras.values()]}")
        with cameras_lock:
            for index, (camera_id, config) in enumerate(sorted(cameras.items())):
                delay = index * STAGGER_SECONDS
                thread = threading.Thread(
                    target=camera_worker,
                    args=(camera_id, delay),
                    daemon=True,
                    name=f"cam-{camera_id[:8]}",
                )
                thread.start()
                active_camera_ids.add(camera_id)

    manager_thread = threading.Thread(
        target=camera_manager, daemon=True, name="cam-manager"
    )
    manager_thread.start()

    print("Presence detector running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down ...")
        stop_event.set()
        time.sleep(2)
        print("Done.")


if __name__ == "__main__":
    main()
