from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import threading

import queue
import json
import time
import os
import re

from PySide6 import QtCore, QtGui, QtWidgets
import serial
from serial.serialutil import SerialException

# --- OPTIONAL Goose theme import (safe) ---
try:
    import goose_theme
except Exception:
    goose_theme = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
THEMES_DIR = os.path.join(ASSETS_DIR, "themes")


# --- Camera deps (optional) ---
try:
    import cv2
    import numpy as np
    from sklearn.cluster import DBSCAN
except Exception:
    cv2 = None
    np = None
    DBSCAN = None

AI_PLAYER_ID = 1
MAX_HUMAN_PLAYERS = 3
MAX_PLAYERS = AI_PLAYER_ID + MAX_HUMAN_PLAYERS

PLAYER_COLORS: dict[int, str] = {
    1: "#ef4444",  # AI - red
    2: "#f59e0b",  # Human 1 - amber
    3: "#22c55e",  # Human 2 - green
    4: "#3b82f6",  # Human 3 - blue
}


def player_display_name(player_id: int) -> str:
    pid = int(player_id)
    if pid == AI_PLAYER_ID:
        return "AI"
    if 2 <= pid <= MAX_PLAYERS:
        return f"Human {pid - 1}"
    return "Player"


def player_display_tag(player_id: int) -> str:
    pid = int(player_id)
    if pid >= 1:
        return f"{player_display_name(pid)} P{pid}"
    return "Player ?"


# =============================
# Theme system (Leuven is "main")
# =============================
@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    assets_dir: str  # assets/themes/<key>


def available_themes() -> list[Theme]:
    out = [
        Theme("leuven", "Leuven", os.path.join(THEMES_DIR, "leuven")),
    ]
    if goose_theme is not None:
        out.append(Theme(goose_theme.THEME_KEY, goose_theme.THEME_LABEL, goose_theme.theme_assets_dir(BASE_DIR)))
    return out


def theme_by_key(key: str) -> Theme:
    for t in available_themes():
        if t.key == key:
            return t
    return available_themes()[0]


# =============================
# Config
# =============================
@dataclass
class SerialConfig:
    port: str = "COM7"
    baudrate: int = 115200


# =============================
# Camera config
# =============================
ESP32_IP = "192.168.157.211"
STREAM_URL = f"http://{ESP32_IP}/stream"
SHOW_CAMERA_PREVIEW = True


@dataclass
class CameraConfig:
    stream_url: str = STREAM_URL
    show_preview_window: bool = SHOW_CAMERA_PREVIEW


    blur_size: Tuple[int, int] = (5, 5)
    stable_frames: int = 2
    rearm_no_dice_s: float = 0.3
    max_ready_age_s: float = 5
    min_area: float = 60#90
    max_area: float = 300
    min_circularity: float = 0.5
    min_inertia_ratio: float = 0.3
    dbscan_eps: float = 180.0



 


# =============================
# Game Rules Engine (mirrors ESP32 C rules)
# =============================
class RuleEngine:
    def __init__(self, total_players: int = 2):
        self.total_players = max(2, min(MAX_PLAYERS, int(total_players)))
        self.positions: Dict[int, int] = {i: 1 for i in range(1, self.total_players + 1)}
        self.active_player: int = 1
        self.turns_to_skip: Dict[int, int] = {i: 0 for i in range(1, self.total_players + 1)}
        self.move_backwards_flag: Dict[int, bool] = {i: False for i in range(1, self.total_players + 1)}

        self.last_moved_player: Optional[int] = None
        self.last_dice: Optional[int] = None
        self.last_old_pos: Optional[int] = None
        self.last_landed_pos: Optional[int] = None
        self.last_final_pos: Optional[int] = None
        self.last_repeat_turn: bool = False
        self.last_was_skip_turn: bool = False

        self._last_applied_signature: Optional[Tuple[int, int, int, int, int, int, int]] = None
        self._initialized_from_pos: bool = False

    def _sig_from_pos(self, d: dict) -> Optional[Tuple[int, int, int, int, int, int, int]]:
        try:
            p1 = int(d.get("p1"))
            p2 = int(d.get("p2"))
            p3 = int(d.get("p3", 1))
            p4 = int(d.get("p4", 1))
            active = int(d.get("active", 0))
            lp = int(d.get("last_player", 0))
            dice = int(d.get("dice", 0))
            return (p1, p2, p3, p4, active, lp, dice)
        except Exception:
            return None

    def _sync_from_pos(self, d: dict) -> None:
        for i in range(1, self.total_players + 1):
            key = f"p{i}"
            if d.get(key) is not None:
                self.positions[i] = max(1, min(36, int(d[key])))
        if d.get("active") is not None:
            try:
                self.active_player = int(d["active"])
            except Exception:
                pass
        self._initialized_from_pos = True

    def _process_special_tile(self, player: int, landed_pos: int):
        final_pos = landed_pos
        repeat = False
        evt = None

        if landed_pos == 4:
            self.turns_to_skip[player] = 1
            evt = {"player": player, "tile": landed_pos, "name": "Tile 4 – Skip 1 turn",
                   "msg": "Next time you must skip 1 turn."}
        elif landed_pos == 22:
            self.turns_to_skip[player] = 1
            evt = {"player": player, "tile": landed_pos, "name": "Tile 22 – Skip 1 turn",
                   "msg": "Next time you must skip 1 turn."}
        elif landed_pos == 28:
            self.turns_to_skip[player] = 2
            evt = {"player": player, "tile": landed_pos, "name": "Tile 28 – Skip 2 turns",
                   "msg": "Next time you must skip 2 turns."}
        elif landed_pos == 6:
            repeat = True
            evt = {"player": player, "tile": landed_pos, "name": "Tile 6 – Roll again",
                   "msg": "Roll again!"}
        elif landed_pos == 26:
            repeat = True
            evt = {"player": player, "tile": landed_pos, "name": "Tile 26 – Roll again",
                   "msg": "Roll again!"}
        elif landed_pos == 10:
            final_pos = 12
            evt = {"player": player, "tile": landed_pos, "name": "Tile 10 – Move to 12",
                   "msg": "Shortcut: move to tile 12."}
        elif landed_pos == 14:
            final_pos = 1
            evt = {"player": player, "tile": landed_pos, "name": "Tile 14 – Back to start",
                   "msg": "Go back to tile 1."}
        elif landed_pos == 19:
            final_pos = 24
            evt = {"player": player, "tile": landed_pos, "name": "Tile 19 – Move to 24",
                   "msg": "Move to tile 24."}
        elif landed_pos == 31:
            final_pos = 32
            evt = {"player": player, "tile": landed_pos, "name": "Tile 31 – Move +1",
                   "msg": "Move 1 space forward (to tile 32)."}
        elif landed_pos == 35:
            final_pos = 9
            evt = {"player": player, "tile": landed_pos, "name": "Tile 35 – Back to 9",
                   "msg": "Go back to tile 9."}
        elif landed_pos == 16:
            repeat = True
            self.move_backwards_flag[player] = True
            evt = {"player": player, "tile": landed_pos, "name": "Tile 16 – Roll again + backwards next",
                   "msg": "Roll again, but your NEXT move will be backwards!"}

        return final_pos, repeat, evt

    def apply_move(self, player: int, dice: int):
        player = int(player)
        dice = int(dice)

        self.last_moved_player = player
        self.last_dice = dice
        self.last_repeat_turn = False
        self.last_was_skip_turn = False

        old_pos = int(self.positions.get(player, 1))
        self.last_old_pos = old_pos

        if self.turns_to_skip.get(player, 0) > 0:
            self.turns_to_skip[player] -= 1
            self.last_was_skip_turn = True
            landed = old_pos
            final = old_pos
            repeat = False
            evt = None
        else:
            if self.move_backwards_flag.get(player, False):
                landed = max(1, old_pos - dice)
                self.move_backwards_flag[player] = False
            else:
                landed = min(36, old_pos + dice)

            final, repeat, evt = self._process_special_tile(player, landed)

        self.positions[player] = final
        self.last_landed_pos = landed
        self.last_final_pos = final
        self.last_repeat_turn = bool(repeat)

        if repeat:
            self.active_player = player
        else:
            self.active_player = (player % self.total_players) + 1

        return evt

    def on_pos(self, d: dict) -> Optional[dict]:
        sig = self._sig_from_pos(d)
        if sig is None or sig == self._last_applied_signature:
            return None

        lp = int(d.get("last_player") or 0)
        dice = int(d.get("dice") or 0)

        if lp < 1 or lp > self.total_players:
            self._sync_from_pos(d)
            self._last_applied_signature = sig
            return None

        if not self._initialized_from_pos:
            self._sync_from_pos(d)
            self._last_applied_signature = sig
            return None

        evt = None
        if dice <= 0:
            if self.turns_to_skip.get(lp, 0) > 0:
                evt = self.apply_move(lp, dice)
            else:
                self._sync_from_pos(d)
                self._last_applied_signature = sig
                return None
        else:
            evt = self.apply_move(lp, dice)

        self._sync_from_pos(d)
        self._last_applied_signature = sig
        return evt


# =============================
# Serial worker (background thread)
# =============================
class SerialWorker(threading.Thread):
    def __init__(self, cfg: SerialConfig, q: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = q
        self._running = True
        self.ser = None
        self.tx_q: queue.Queue[str] = queue.Queue()

    def stop(self):
        self._running = False
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

    def send_line(self, line: str):
        self.tx_q.put(line.strip())

    def _drain_tx(self):
        if not self.ser or not self.ser.is_open:
            return
        try:
            while True:
                line = self.tx_q.get_nowait()
                payload = (line + "\n").encode("utf-8")
                self.ser.write(payload)
                self.ser.flush()
                self.q.put(("log", f"→ Sent: {line}"))
        except queue.Empty:
            return
        except Exception as e:
            self.q.put(("log", f"TX error: {e}"))

    def run(self):
        self.q.put(("log", f"Opening serial port {self.cfg.port} @ {self.cfg.baudrate}..."))
        try:
            self.ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=0.2)
        except SerialException as e:
            self.q.put(("error", f"Could not open serial port: {e}"))
            return

        self.q.put(("log", "Connected to ESP32. Waiting for data."))

        while self._running:
            self._drain_tx()

            try:
                line_bytes = self.ser.readline()
            except SerialException as e:
                self.q.put(("error", f"Serial read error: {e}"))
                break

            if not line_bytes:
                continue

            try:
                line = line_bytes.decode("utf-8", errors="ignore").strip()
            except UnicodeDecodeError:
                continue

            if not line:
                continue

            if line.startswith("WIN:"):
                json_str = line[4:]
                try:
                    data = json.loads(json_str)
                    winner = data.get("winner")
                    if winner is not None:
                        self.q.put(("win", int(winner)))
                        self.q.put(("log", f"WIN received: {player_display_tag(int(winner))}"))
                except json.JSONDecodeError:
                    self.q.put(("log", f"WIN JSON error: {json_str}"))
                continue

            if line.startswith("EVT:"):
                json_str = line[4:]
                try:
                    data = json.loads(json_str)
                    self.q.put(("evt", data))
                    p = data.get("player")
                    t = data.get("tile")
                    n = data.get("name")
                    self.q.put(("log", f"EVT: {player_display_tag(int(p) if p is not None else 0)} landed on tile {t} ({n})"))
                except json.JSONDecodeError:
                    self.q.put(("log", f"EVT JSON error: {json_str}"))
                continue

            if line.startswith("POS:"):
                json_str = line[4:]
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    self.q.put(("log", f"POS JSON error: {json_str}"))
                    continue

                self.q.put(("pos", data))

                active = data.get("active")
                dice = data.get("dice")
                last_player = data.get("last_player")

                pos_fields = []
                for i in range(1, MAX_PLAYERS + 1):
                    key = f"p{i}"
                    if key in data:
                        pos_fields.append(f"{key}={data.get(key)}")
                pos_part = ", ".join(pos_fields) if pos_fields else "no player fields"
                log_line = f"POS: {pos_part}, active={active}"
                if dice is not None:
                    log_line += f", dice={dice}"
                if last_player is not None:
                    log_line += f", last_player={last_player}"
                self.q.put(("log", log_line))
                continue

            if line.startswith("CMD:VOICE_CONFIRM_DICE"):
                self.q.put(("voice_confirm", None))
                self.q.put(("log", "CMD: voice confirm dice requested."))
                continue

            if line.startswith("QUIZ:"):
                json_str = line[5:]
                try:
                    data = json.loads(json_str)
                    self.q.put(("quiz", data))
                    self.q.put(("log", f"QUIZ: {json_str}"))
                except json.JSONDecodeError:
                    self.q.put(("log", f"QUIZ JSON error: {json_str}"))
                continue
            else:
                self.q.put(("log", f"LOG: {line}"))

        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.q.put(("log", "Serial connection closed."))


# =============================
# Dice camera worker (background thread)
# =============================
class DiceCameraWorker(threading.Thread):
    def __init__(self, cfg: CameraConfig, q: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = q
        self._running = True

        self._lock = threading.Lock()
        self._last_candidate: Optional[int] = None
        self._stable_count: int = 0
        self._ready_value: Optional[int] = None
        self._ready_since: float = 0.0
        self._armed: bool = True
        self._no_dice_since: Optional[float] = None
        self._preview_quit: bool = False

    def stop(self):
        self._running = False

    def consume(self):
        with self._lock:
            self._armed = False
            self._no_dice_since = None
            self._ready_value = None
            self._stable_count = 0
            self._last_candidate = None

    def get_ready_value(self) -> Optional[int]:
        now = time.time()
        with self._lock:
            if not self._armed:
                return None
            if self._ready_value is None:
                return None
            if (now - self._ready_since) > self.cfg.max_ready_age_s:
                return None
            return int(self._ready_value)

    def _create_blob_detector(self):
        params = cv2.SimpleBlobDetector_Params()
        params.filterByArea = True
        params.minArea = float(self.cfg.min_area)
        params.maxArea = float(self.cfg.max_area)
        params.filterByCircularity = True
        params.minCircularity = float(self.cfg.min_circularity)
        params.filterByInertia = True
        params.minInertiaRatio = float(self.cfg.min_inertia_ratio)
        params.filterByColor = True
        params.blobColor = 0
        return cv2.SimpleBlobDetector_create(params)




    def _process_frame(self, frame, detector) -> Optional[int]:
 
        if frame is None:
            return None
        
        #greyscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        #blur the image slightly to remove noise and make dots clearer
        gray = cv2.GaussianBlur(gray, self.cfg.blur_size, 0)
        
        #detect all the dark circular blobs
        keypoints = detector.detect(gray)

        if not keypoints:
            return None
        
        #extract the (x, y) positions of all detected dots
        pts = np.array([kp.pt for kp in keypoints])
        
        #step 2: blur the image
        #gaussian blur smooths the image and removes small imperfections which helps
        #the blob detector focus on real dots and not noise
        #(5, 5) is the blur kernel size (bigger = more blur)
        try:
            clustering = DBSCAN(eps=self.cfg.dbscan_eps, min_samples=1).fit(pts)
            labels = clustering.labels_
        except Exception:
            return None
        
        #now we analyze each group of dots to figure out which is the top face
        cluster_info = []
        
        for cluster_id in np.unique(labels):
            #skip noise points
            if cluster_id == -1:
                continue
            
            #get all the dots that belong to this cluster
            cluster_mask = labels == cluster_id
            cluster_points = pts[cluster_mask]
            num_dots = int(np.sum(cluster_mask))
            
            #feature 1: calculate average Y position
            #the top face of the dice is higher in the image (smaller Y value)
            #Y=0 is the top of the image, Y increases going down so smaller Y = higher up
            avg_y = np.mean(cluster_points[:, 1])
            
            #feature 2:calculate the size of this cluster
            #the top face is usually larger because it is closer to the camera
            x_coords = cluster_points[:, 0]
            y_coords = cluster_points[:, 1]
            
            x_span = np.max(x_coords) - np.min(x_coords)
            y_span = np.max(y_coords) - np.min(y_coords)
            area = x_span * y_span
            
            #save all the information about this cluster
            cluster_info.append({
                'num_dots': num_dots,
                'avg_y': avg_y,
                'area': area
            })

        if not cluster_info:
            return None
        
        #get lists of all Y values and areas for normalization
        y_values = [c['avg_y'] for c in cluster_info]
        areas = [c['area'] for c in cluster_info]
        
        y_min = min(y_values)
        y_max = max(y_values)
        area_min = min(areas)
        area_max = max(areas)
        
        #calculate a score for each cluster to determine which is the top face
        for cluster in cluster_info:

            #normalize Y position to a 0-1 scale. we flip it so that higher in image (smaller Y) = higher score
            if y_max > y_min:
                norm_y = 1.0 - ((cluster['avg_y'] - y_min) / (y_max - y_min))
            else:
                norm_y = 1.0  #all clusters are at the same height
            
            #normalize area to a 0-1 scale
            #larger area = higher score
            if area_max > area_min:
                norm_area = (cluster['area'] - area_min) / (area_max - area_min)
            else:
                norm_area = 1.0  #all clusters are the same size

            #calculate final score as weighted average
            #60% based on position 40% based on size
            cluster['score'] = 0.6 * norm_y + 0.4 * norm_area

        #sort clusters by score (highest first)
        cluster_info.sort(key=lambda x: x['score'], reverse=True)
        
        #take the cluster with the highest score (top face)
        top_face = cluster_info[0]
        v = top_face['num_dots']

        if 1 <= v <= 6:
            return int(v)

        return None
        


    def run(self):
        if cv2 is None or np is None or DBSCAN is None:
            self.q.put(("log", "Camera disabled: missing deps (opencv-python / numpy / scikit-learn)."))
            self.q.put(("cam", {"armed": False, "ready": False, "value": None}))
            return

        url = self.cfg.stream_url
        self.q.put(("log", f"Camera connecting: {url}"))
        cap = cv2.VideoCapture(url)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not cap.isOpened():
            self.q.put(("log", "Camera: could not open stream (check ESP32_IP / stream URL)."))
            self.q.put(("cam", {"armed": False, "ready": False, "value": None}))
            return

        detector = self._create_blob_detector()

        if self.cfg.show_preview_window:
            cv2.namedWindow("ESP32 Dice", cv2.WINDOW_NORMAL)

        last_emit = 0.0

        while self._running and not self._preview_quit:
            ret, frame = cap.read()
            if not ret:
                self.q.put(("log", "Camera frame lost, reconnecting..."))
                time.sleep(0.5)
                cap.open(url)
                continue

            candidate = self._process_frame(frame, detector)
            now = time.time()

            with self._lock:
                if not self._armed:
                    if candidate is None:
                        if self._no_dice_since is None:
                            self._no_dice_since = now
                        elif (now - self._no_dice_since) >= self.cfg.rearm_no_dice_s:
                            self._armed = True
                            self._no_dice_since = None
                            self._stable_count = 0
                            self._last_candidate = None
                            self._ready_value = None
                    else:
                        self._no_dice_since = None

                if self._armed:
                    if candidate is None:
                        self._last_candidate = None
                        self._stable_count = 0
                        self._ready_value = None
                    else:
                        if candidate == self._last_candidate:
                            self._stable_count += 1
                        else:
                            self._last_candidate = candidate
                            self._stable_count = 1
                        if self._stable_count >= self.cfg.stable_frames:
                            self._ready_value = candidate
                            self._ready_since = now

                armed = self._armed
                ready_val = self._ready_value if armed else None
                ready = ready_val is not None

            if (now - last_emit) >= 0.15:
                self.q.put(("cam", {"armed": armed, "ready": ready, "value": ready_val}))
                last_emit = now

            if self.cfg.show_preview_window:
                cv2.imshow("ESP32 Dice", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    self._preview_quit = True

        cap.release()
        if self.cfg.show_preview_window:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass





class TutorialDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to GAME4!")
        self.setModal(True)
        self.resize(480, 280)
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a; font-size:14px;}"
            "QCheckBox{color:#0f172a; font-size:14px; margin-top: 10px;}"
            "QPushButton#PrimaryButton{background:#0ea5e9;color:white;border:none;"
            "padding:8px 14px;border-radius:10px;font-size:14px; font-weight:bold;}"
            "QPushButton#PrimaryButton:hover{background:#0284c7}"
        )
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("First time playing?")
        title.setAlignment(QtCore.Qt.AlignCenter)
        font = title.font()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)

        body = QtWidgets.QLabel(
            "Here is a quick overview:\n\n"
            "• Roll the physical dice under the camera.\n"
            "• When the dice is still, confirm by pressing SPACE or saying 'confirm dice'.\n"
            "• Special tiles (skips, roll again, teleports) are handled automatically.\n"
            "• The first player to reach tile 36 wins!"
        )
        body.setWordWrap(True)

        self.checkbox = QtWidgets.QCheckBox("Do not show this tutorial again. (You can always enable it again at the start screen)")

        btn = QtWidgets.QPushButton("Let's Play!")
        btn.setObjectName("PrimaryButton")
        btn.clicked.connect(self.accept)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addSpacing(10)
        layout.addWidget(body)
        layout.addSpacing(10)
        layout.addWidget(self.checkbox, alignment=QtCore.Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(btn, alignment=QtCore.Qt.AlignCenter)

# =============================
# Win dialog
# =============================
class WinDialog(QtWidgets.QDialog):
    def __init__(self, winner_index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Winner!")
        self.setModal(True)
        self.resize(420, 220)
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a}"
            "QPushButton#PrimaryButton{background:#0ea5e9;color:white;border:none;"
            "padding:8px 14px;border-radius:10px;font-size:14px;}"
            "QPushButton#PrimaryButton:hover{background:#0284c7}"
        )
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel(f"🏆 {player_display_tag(winner_index)} wins!")
        title.setAlignment(QtCore.Qt.AlignCenter)
        font = title.font()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)

        subtitle = QtWidgets.QLabel(
            "The physical board has a winner.\n"
            "The companion app shows the win state."
        )
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setWordWrap(True)

        btn = QtWidgets.QPushButton("OK")
        btn.setObjectName("PrimaryButton")
        btn.clicked.connect(self.accept)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(btn, alignment=QtCore.Qt.AlignCenter)


# =============================
# Special tile popup (from File 2 — auto closes after 3 seconds)
# =============================
class SpecialTilePopup(QtWidgets.QDialog):
    def __init__(self, player: int, tile: int, name: str, msg: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Special Tile")
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.resize(520, 220)
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a}"
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        header = QtWidgets.QLabel(f"⭐ {player_display_tag(player)} landed on tile {tile}")
        f = header.font()
        f.setPointSize(14)
        f.setBold(True)
        header.setFont(f)

        title = QtWidgets.QLabel(name)
        ft = title.font()
        ft.setPointSize(18)
        ft.setBold(True)
        title.setFont(ft)

        body = QtWidgets.QLabel(msg)
        body.setWordWrap(True)
        body.setStyleSheet("QLabel{font-size:14px;}")

        hint = QtWidgets.QLabel("This popup closes automatically.")
        hint.setStyleSheet("QLabel{font-size:12px;color:#64748b;}")

        layout.addWidget(header)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch(1)
        layout.addWidget(hint, alignment=QtCore.Qt.AlignRight)


# =============================
# Voice chat widgets
# =============================
ASSISTANT_EMOTION_EMOJI: dict[str, str] = {
    "neutral": "🙂",
    "idle": "🙂",
    "microchip_ai": "🤖",
    "happy": "😄",
    "laughing": "😄",
    "funny": "😄",
    "sad": "😔",
    "angry": "😠",
    "thinking": "🤔",
    "confused": "😕",
    "surprised": "😮",
    "sleepy": "😴",
    "embarrassed": "😳",
    "love": "😊",
    "fear": "😟",
    "disgust": "😖",
    "blink": "😉",
}


def assistant_emotion_key(emotion: str | None) -> str:
    return (emotion or "neutral").strip().lower().replace(" ", "_").replace("-", "_")


def assistant_emoji_for_emotion(emotion: str | None) -> str:
    key = assistant_emotion_key(emotion)
    return ASSISTANT_EMOTION_EMOJI.get(key, ASSISTANT_EMOTION_EMOJI["neutral"])


def assistant_emotion_label(emotion: str | None) -> str:
    key = assistant_emotion_key(emotion)
    return key.replace("_", " ") or "neutral"


class ChatMessageRow(QtWidgets.QWidget):
    def __init__(self, role: str, message: str, emotion: str | None = None, parent=None):
        super().__init__(parent)
        self.role = (role or "system").lower()
        self.emotion = (emotion or "neutral").strip().lower()
        self.setObjectName("ChatMessageRow")
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        if self.role == "system":
            self.bubble = QtWidgets.QLabel(message)
            self.bubble.setObjectName("ChatBubbleSystem")
            self.bubble.setTextFormat(QtCore.Qt.PlainText)
            self.bubble.setWordWrap(True)
            self.bubble.setAlignment(QtCore.Qt.AlignCenter)
            self.bubble.setMinimumHeight(28)
            self.bubble.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Minimum)
            row.addStretch(1)
            row.addWidget(self.bubble)
            row.addStretch(1)
            return

        is_user = self.role == "user"
        avatar_text = "You" if is_user else assistant_emoji_for_emotion(self.emotion)
        title_text = "You" if is_user else f"AI assistant · {assistant_emotion_label(self.emotion)}"

        self.avatar = QtWidgets.QLabel(avatar_text)
        self.avatar.setObjectName("ChatAvatarUser" if is_user else "ChatAvatarAssistant")
        self.avatar.setAlignment(QtCore.Qt.AlignCenter)
        self.avatar.setFixedSize(34, 34)

        self.meta = QtWidgets.QLabel(title_text)
        self.meta.setObjectName("ChatMeta")

        self.bubble = QtWidgets.QLabel(message)
        self.bubble.setObjectName("ChatBubbleUser" if is_user else "ChatBubbleAssistant")
        self.bubble.setTextFormat(QtCore.Qt.PlainText)
        self.bubble.setWordWrap(True)
        self.bubble.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.bubble.setMinimumHeight(34)
        self.bubble.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Minimum)

        col = QtWidgets.QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(self.meta, alignment=QtCore.Qt.AlignRight if is_user else QtCore.Qt.AlignLeft)
        col.addWidget(self.bubble, alignment=QtCore.Qt.AlignRight if is_user else QtCore.Qt.AlignLeft)

        if is_user:
            row.addStretch(1)
            row.addLayout(col)
            row.addWidget(self.avatar, alignment=QtCore.Qt.AlignTop)
        else:
            row.addWidget(self.avatar, alignment=QtCore.Qt.AlignTop)
            row.addLayout(col)
            row.addStretch(1)

    def set_bubble_max_width(self, width: int):
        width = max(180, int(width))
        self.bubble.setMaximumWidth(width)
        self.bubble.updateGeometry()


class ChatFeed(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatFeed")
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        self._content = QtWidgets.QWidget()
        self._content.setObjectName("ChatFeedContent")
        self._layout = QtWidgets.QVBoxLayout(self._content)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(10)
        self._layout.addStretch(1)
        self.setWidget(self._content)
        self._rows: list[ChatMessageRow] = []

    def append_message(self, role: str, message: str, emotion: str | None = None):
        row = ChatMessageRow(role, message, emotion, self._content)
        self._rows.append(row)
        self._layout.insertWidget(max(0, self._layout.count() - 1), row)
        self._sync_bubble_widths()
        QtCore.QTimer.singleShot(0, self._scroll_to_bottom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_bubble_widths()

    def _sync_bubble_widths(self):
        max_width = int(max(220, self.viewport().width() * 0.72))
        for row in self._rows:
            row.set_bubble_max_width(max_width)

    def _scroll_to_bottom(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


# =============================
# Interactive Spotlight Tutorial Overlay
# =============================

class TutorialOverlay(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget, steps: list, settings: QtCore.QSettings):
        super().__init__(parent)
        self.steps = steps
        self.settings = settings
        self.step_idx = 0


        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addStretch(1)

        self.panel = QtWidgets.QFrame()
        self.panel.setStyleSheet(
            "QFrame { background: white; border-radius: 12px; border: 1px solid #e2e8f0; }"
            "QLabel { color: #0f172a; font-size: 16px; }"
            "QCheckBox { color: #0f172a; font-size: 14px; }"
            "QPushButton { padding: 8px 16px; border-radius: 8px; font-weight: bold; font-size: 14px; }"
            "QPushButton#NextBtn { background: #0ea5e9; color: white; border: none; }"
            "QPushButton#NextBtn:hover { background: #0284c7; }"
            "QPushButton#PrevBtn { background: transparent; color: #64748b; border: 1px solid #cbd5e1; }"
            "QPushButton#PrevBtn:hover { background: #f1f5f9; }"
        )
        panel_layout = QtWidgets.QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(20, 20, 20, 20)

        self.lbl_text = QtWidgets.QLabel("")
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setAlignment(QtCore.Qt.AlignCenter)
        
        self.chk_skip = QtWidgets.QCheckBox("Do not ask this again")
        self.chk_skip.setChecked(True)
        self.chk_skip.setVisible(False)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton("← Back")
        self.btn_prev.setObjectName("PrevBtn")
        self.btn_next = QtWidgets.QPushButton("Next →")
        self.btn_next.setObjectName("NextBtn")
        
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next.clicked.connect(self._next)

        btn_row.addWidget(self.btn_prev)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_next)

        panel_layout.addWidget(self.lbl_text)
        panel_layout.addSpacing(15)
        panel_layout.addWidget(self.chk_skip, alignment=QtCore.Qt.AlignCenter)
        panel_layout.addSpacing(10)
        panel_layout.addLayout(btn_row)

        h_layout = QtWidgets.QHBoxLayout()
        h_layout.addStretch(1)
        h_layout.addWidget(self.panel, stretch=2)
        h_layout.addStretch(1)

        layout.addLayout(h_layout)
        layout.addSpacing(30)

        self._update_ui()

    def _update_ui(self):
        if not self.steps:
            return
            
        step_data = self.steps[self.step_idx]
        self.lbl_text.setText(step_data["text"])
        
        self.btn_prev.setVisible(self.step_idx > 0)

        is_last_step = self.step_idx == len(self.steps) - 1

        self.chk_skip.setVisible(is_last_step)
        
        if is_last_step:
            self.btn_next.setText("Finish ✓")
        else:
            self.btn_next.setText("Next →")

        self.update()

    def _prev(self):
        if self.step_idx > 0:
            self.step_idx -= 1
            self._update_ui()

    def _next(self):
        if self.step_idx < len(self.steps) - 1:
            self.step_idx += 1
            self._update_ui()
        else:
            if self.chk_skip.isChecked():
                self.settings.setValue("show_tutorial", False)
            self.hide()
            self.deleteLater()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        path = QtGui.QPainterPath()
        path.addRect(QtCore.QRectF(self.rect()))
        
        if self.steps and 0 <= self.step_idx < len(self.steps):
            target_widget = self.steps[self.step_idx]["widget"]
            if target_widget:
                top_left = target_widget.mapToGlobal(QtCore.QPoint(0, 0))
                local_top_left = self.mapFromGlobal(top_left)
                target_rect = QtCore.QRect(local_top_left, target_widget.size())
                cutout = QtGui.QPainterPath()
                cutout.addRoundedRect(QtCore.QRectF(target_rect).adjusted(-8, -8, 8, 8), 12, 12)

                path = path.subtracted(cutout)

        painter.fillPath(path, QtGui.QColor(0, 0, 0, 190))

# =============================
# Animated gradient background
# =============================
class AnimatedGradientWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(20)

    def _tick(self):
        self._offset = (self._offset + 0.003) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect()

        t = self._offset
        x1 = 0.5 + 0.5 * __import__("math").sin(t * 2 * 3.14159)
        y1 = 0.5 + 0.5 * __import__("math").cos(t * 2 * 3.14159)

        gradient = QtGui.QLinearGradient(
            rect.width() * x1,
            rect.height() * y1,
            rect.width() * (1 - x1),
            rect.height() * (1 - y1),
        )
        gradient.setColorAt(0.0, QtGui.QColor("#FF00C8"))
        gradient.setColorAt(0.5, QtGui.QColor("#7700FF"))
        gradient.setColorAt(1.0, QtGui.QColor("#32005E"))
        painter.fillRect(rect, gradient)

# =============================
# Start screen
# =============================
class StartScreen(AnimatedGradientWidget):
    start_clicked = QtCore.Signal(str, int)  # theme_key, human_players

    def __init__(self, serial_cfg: SerialConfig):
        super().__init__()
        self.serial_cfg = serial_cfg
        self.setObjectName("StartScreen")
        
        # Initialize Settings
        self.settings = QtCore.QSettings("MyCompany", "GAME4Companion")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        title = QtWidgets.QLabel("GAME4 Companion")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setObjectName("Title")

        subtitle = QtWidgets.QLabel(
            "Live dashboard for the physical board\n"
            "AI + 1~3 human players • ESP32 • LED strip"
        )
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setObjectName("Subtitle")

        card = QtWidgets.QGroupBox("Connection & game")
        card.setObjectName("Card")
        form = QtWidgets.QFormLayout(card)

        players_label = QtWidgets.QLabel("AI + selectable human players")
        form.addRow("Players:", players_label)

        self.human_count_combo = QtWidgets.QComboBox()
        self.human_count_combo.addItem("1 Human (2 total)", userData=1)
        self.human_count_combo.addItem("2 Humans (3 total)", userData=2)
        self.human_count_combo.addItem("3 Humans (4 total)", userData=3)
        self.human_count_combo.setCurrentIndex(0)
        form.addRow("Human count:", self.human_count_combo)

        port_label = QtWidgets.QLabel(f"{self.serial_cfg.port} @ {self.serial_cfg.baudrate}")
        form.addRow("Serial port:", port_label)

        self.theme_combo = QtWidgets.QComboBox()
        self._themes = available_themes()
        for t in self._themes:
            self.theme_combo.addItem(t.label, userData=t.key)
        self.theme_combo.setCurrentIndex(0)

        theme_label = QtWidgets.QLabel("Theme:")
        theme_label.setObjectName("FormLabel")
        form.addRow(theme_label, self.theme_combo)

        self.status_label = QtWidgets.QLabel("Not connected to ESP32 yet.")
        self.status_label.setObjectName("Info")
        self.status_label.setWordWrap(True)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        start_btn = QtWidgets.QPushButton("Start")
        start_btn.setObjectName("PrimaryButton")
        start_btn.setFixedHeight(48)
        start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(start_btn)
        btn_row.addStretch(1)

        bottom_row = QtWidgets.QHBoxLayout()
        
        self.tutorial_checkbox = QtWidgets.QCheckBox("Enable Interactive Tutorial")
        self.tutorial_checkbox.setObjectName("TutorialCheck")
        self.tutorial_checkbox.setCursor(QtCore.Qt.PointingHandCursor)
        
        show_tut = self.settings.value("show_tutorial", True, type=bool)
        self.tutorial_checkbox.setChecked(show_tut)
        self.tutorial_checkbox.toggled.connect(self._on_tut_toggled)

        rules_btn = QtWidgets.QPushButton("How it works")
        rules_btn.setObjectName("GhostButton")
        rules_btn.setFixedHeight(40)
        rules_btn.clicked.connect(self._show_rules)
        
        bottom_row.addWidget(self.tutorial_checkbox)
        bottom_row.addStretch(1)
        bottom_row.addWidget(rules_btn)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(card)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)
        layout.addLayout(bottom_row)
        layout.addStretch(2)


        self.setStyleSheet("""
        StartScreen {
            background-color: #1F3D2B;
            color: #F8F7F3;
            font-family: 'Segoe UI';
        }

        QLabel {
            background: transparent;
            color: #F8F7F3;
        }

        QLabel#Info, QLabel#FormLabel, QLabel#Subtitle {
            background: transparent;
            color: #E6E0D6;
        }

        QLabel#Title {
            background: transparent;
            color: #F8F7F3;
            font-size: 20px;
            font-weight: 700;
        }

        QGroupBox#Card {
            background-color: #2E5A3F;
            border: 1px solid #3F6B4F;
            border-radius: 12px;
            margin-top: 16px;
            padding-top: 10px;
        }

        QGroupBox#Card::title {
            subcontrol-origin: margin;
            left: 14px;
            top: 2px;
            padding: 0 6px;
            color: #D6D3C9;
        }

        QGroupBox#Card::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
            color: #F8F7F3;
            background: transparent;
        }

        QComboBox {
            background-color: #214631;
            color: #F8F7F3;
            border: 1px solid #D8C27A;
            border-radius: 10px;
            padding: 8px 12px;
        }

        QCheckBox {
            background: transparent;
            color: #F8F7F3;
            spacing: 10px;
        }

        QCheckBox::indicator {
            width: 18px;
            height: 18px;
            border-radius: 5px;
            border: 2px solid #E4C580;
            background: transparent;
        }

        QCheckBox::indicator:checked {
            background: #E4C580;
        }

        QPushButton#PrimaryButton {
            background-color: #E4C580;
            color: #1F3D2B;
            border: none;
            border-radius: 10px;
            font-weight: 700;
            padding: 8px 18px;
        }

        QPushButton#PrimaryButton:hover {
            background-color: #EBD293;
        }

        QPushButton#GhostButton {
            background-color: #2E5A3F;
            color: #F8F7F3;
            border: 1px solid #6C8A66;
            border-radius: 8px;
            padding: 8px 14px;
        }

        QPushButton#GhostButton:hover {
            background-color: #365F46;
        }
        """)




    def _on_tut_toggled(self, checked):
        self.settings.setValue("show_tutorial", checked)

    def _on_start(self):
        key = self.theme_combo.currentData()
        if not key:
            key = "leuven"
        human_players = self.human_count_combo.currentData()
        if human_players is None:
            human_players = 1
        self.start_clicked.emit(str(key), int(human_players))

    def set_status(self, text: str):
        self.status_label.setText(text)

    def _show_rules(self):
        msg = (
            "Companion app for your physical board:\n\n"
            "• Player 1 is AI, and you can choose 1 to 3 human players.\n"
            "• The current player rolls a physical dice under the camera.\n"
            "• When the dice lies still in view, confirm by SPACE or voice command.\n"
            "• The app sends DICE:1–6 to the ESP32 which updates pawn positions.\n"
            "• Special tiles trigger rules (skip turns, roll again, teleport, etc.).\n\n"
            "Note:\n"
            "• Skip turns are handled automatically by the board.\n"
            "• Themes only change visuals."
        )
        QtWidgets.QMessageBox.information(self, "How it works", msg)

# =============================
# Tile meta (Leuven stays here)
# =============================
@dataclass(frozen=True)
class TileMeta:
    name: str
    rule: str
    short_text: str


def build_tile_metas_leuven() -> dict[int, TileMeta]:
    return {
        1:  TileMeta("Station Leuven (Leuven Station)", "Start", ""),
        2:  TileMeta("Maria Therasiastraat", "—", ""),
        3:  TileMeta("Bogaardenstraat", "—", ""),
        4:  TileMeta("Ladeuzeplein (Ladeuze Square)", "Skip one turn",
                     "Ge gaat op cafe en drinkt een heerlijk pilsje, sla een beurt over"),
        5:  TileMeta("Leopold Vanderkelenstraat", "—", ""),
        6:  TileMeta("M-Museum Leuven", "Throw again",
                     "Je hebt veel kennis opgedaan in het museum, gooi nog eens."),
        7:  TileMeta("Bondgenotenlaan", "—", ""),
        8:  TileMeta("Rector de Somerplein", "—", ""),
        9:  TileMeta("Sint-pieterskerk", "—", ""),
        10: TileMeta("Brusselsestraat", "Move places forward",
                    "Ga verder naar de kruidtuin"),
        11: TileMeta("Kapucijnenvoer", "—", ""),
        12: TileMeta("Kruidtuin", "—", ""),
        13: TileMeta("Minderbroederstraat", "—", ""),
        14: TileMeta("Oude markt", "Go back to the beginning",
                     "You drank too much and got lost. Suddenly you ended up at the station! Go back to the beginning"),
        15: TileMeta("Naamsestraat", "—", ""),
        16: TileMeta("Groot Begijnhof", "Throw again and go that amount back",
                     "You entered a priviliged room, the security is chasing you. Throw again and go that amount of places back"),
        17: TileMeta("Tervuursevest", "—", ""),
        18: TileMeta("Koning boudewijnlaan", "—", ""),
        19: TileMeta("Celestijnenlaan", "—", "You fell in the Dijle and and went with the stream and ended up in stadium of OHLeuven"),
        20: TileMeta("Kasteel van Arenberg", "—",""),
        21: TileMeta("Willem de Croylaan", "—", ""),
        22: TileMeta("Biblioteek 2Bergen", "Skip one turn",
                     "You entered the library and wanted to read about the places you go next to. Skip one turn"),
        23: TileMeta("Kardinaal Mercierlaan", "—", ""),
        24: TileMeta("Stadium OHL", "—", ""),
        25: TileMeta("Schapenstraat", "—", ""),
        26: TileMeta("STUK Kunstencentrum (STUK Arts Center)", "Roll again",
                     "The concert gives you energy — roll again!"),
        27: TileMeta("Parkstraat", "—", ""),
        28: TileMeta("Downtown Jack", "Skip two turns",
                     "You lost track of time, skip 2 turns"),
        29: TileMeta("Van Het Sestichstraat", "—", ""),
        30: TileMeta("Weldadigheidstraat", "—", ""),
        31: TileMeta("Sportoase Leuven", "Move 1 space forward",
                     "You feel energized, go 1 place forward"),
        32: TileMeta("Henri Regasstraat", "—", ""),
        33: TileMeta("Brabanconnestraat", "—", ""),
        34: TileMeta("Tiensestraat", "—", ""),
        35: TileMeta("Cinema ZED", "Go back to square 9",
                     "You got scared of the latest horror movie, go back the the church to pray. (Sint pieterskerk)"),
        36: TileMeta("Groep T Leuven (Group T Leuven)", "Finish!",
                     "Congratulations, you reached the end!"),
    }


# =============================
# Board geometry (36 tiles loop)
# =============================
@dataclass(frozen=True)
class BoardLayout:
    tile_size: int = 52
    gap: int = 4
    margin: int = 8
    top_count: int = 10
    right_count: int = 8
    bottom_count: int = 10
    left_count: int = 8


def tile_positions(layout: BoardLayout) -> dict[int, QtCore.QPointF]:
    ts, g, m = layout.tile_size, layout.gap, layout.margin
    step = ts + g
    pos: dict[int, QtCore.QPointF] = {}

    y_top = m
    for i in range(layout.top_count):
        n = 1 + i
        x = m + i * step
        pos[n] = QtCore.QPointF(x, y_top)

    x_right = m + (layout.top_count - 1) * step
    for j in range(layout.right_count):
        n = 11 + j
        y = y_top + (j + 1) * step
        pos[n] = QtCore.QPointF(x_right, y)

    y_bottom = y_top + (layout.right_count + 1) * step
    for i in range(layout.bottom_count):
        n = 19 + i
        x = x_right - i * step
        pos[n] = QtCore.QPointF(x, y_bottom)

    x_left = m
    for j in range(layout.left_count):
        n = 29 + j
        y = y_bottom - (j + 1) * step
        pos[n] = QtCore.QPointF(x_left, y)

    return pos


# =============================
# Graphics items
# =============================
class TileItem(QtWidgets.QGraphicsObject):
    def __init__(self, tile_id: int, meta: TileMeta, pixmap: QtGui.QPixmap, size: int):
        super().__init__()
        self.tile_id = tile_id
        self.meta = meta
        self.size = size
        self._pix = pixmap
        self.setAcceptHoverEvents(True)
        self._hover = False

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, self.size, self.size)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        r = self.boundingRect()

        if not self._pix.isNull():
            painter.drawPixmap(r.toRect(), self._pix)
        else:
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(230, 236, 245))
            painter.drawRoundedRect(r, 12, 12)

        pen = QtGui.QPen(QtGui.QColor(90, 110, 140) if self._hover else QtGui.QColor(170, 185, 205))
        pen.setWidth(2 if self._hover else 1)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        badge = QtCore.QRectF(6, 6, 22, 18)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(255, 255, 255, 220))
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(QtGui.QPen(QtGui.QColor(20, 30, 45)))
        f = painter.font()
        f.setPointSize(9)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(badge, QtCore.Qt.AlignCenter, str(self.tile_id))

        ribbon_h = 22
        ribbon = QtCore.QRectF(0, self.size - ribbon_h, self.size, ribbon_h)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 120))
        painter.drawRoundedRect(ribbon.adjusted(4, 0, -4, -4), 10, 10)

        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255)))
        f2 = painter.font()
        f2.setPointSize(8)
        f2.setBold(True)
        painter.setFont(f2)
        name = self.meta.name
        fm = QtGui.QFontMetrics(f2)
        name = fm.elidedText(name, QtCore.Qt.ElideRight, int(self.size - 12))
        painter.drawText(ribbon.adjusted(6, 0, -6, -4),
                         QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, name)

        if self.meta.rule and self.meta.rule != "—":
            tag = QtCore.QRectF(6, self.size - ribbon_h - 20, self.size - 12, 16)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(255, 210, 90, 200))
            painter.drawRoundedRect(tag, 8, 8)
            painter.setPen(QtGui.QPen(QtGui.QColor(40, 30, 10)))
            f3 = painter.font()
            f3.setPointSize(7)
            f3.setBold(True)
            painter.setFont(f3)
            rule = QtGui.QFontMetrics(f3).elidedText(self.meta.rule, QtCore.Qt.ElideRight, int(tag.width() - 8))
            painter.drawText(tag.adjusted(6, 0, -6, 0),
                             QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, rule)

    def hoverEnterEvent(self, event):
        self._hover = True
        tip = f"#{self.tile_id}  {self.meta.name}"
        if self.meta.rule and self.meta.rule != "—":
            tip += f"\nRule: {self.meta.rule}"
        if self.meta.short_text:
            tip += f"\n\n{self.meta.short_text}"
        self.setToolTip(tip)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)


class PawnItem(QtWidgets.QGraphicsPixmapItem):
    def __init__(self, player_id: int, pix: QtGui.QPixmap, size: int):
        super().__init__(pix)
        self.player_id = player_id
        if not pix.isNull():
            self.setPixmap(pix.scaled(size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        else:
            fallback = QtGui.QPixmap(size, size)
            fallback.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(fallback)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            color = QtGui.QColor(PLAYER_COLORS.get(player_id, "#0ea5e9"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#0f172a"), 2))
            painter.setBrush(QtGui.QBrush(color))
            painter.drawEllipse(2, 2, size - 4, size - 4)
            painter.end()
            self.setPixmap(fallback)
        self.setZValue(100)


class _AnimProxy(QtCore.QObject):
    def __init__(self, item: QtWidgets.QGraphicsItem):
        super().__init__()
        self._item = item

    def getPos(self) -> QtCore.QPointF:
        return self._item.pos()

    def setPos(self, p: QtCore.QPointF):
        self._item.setPos(p)

    pos = QtCore.Property(QtCore.QPointF, fget=getPos, fset=setPos)


# =============================
# Board view widget (theme-aware — from File 1)
# =============================
class BoardView(QtWidgets.QGraphicsView):
    def __init__(self, parent=None, theme: Theme | None = None, total_players: int = 2):
        super().__init__(parent)
        self.theme = theme or theme_by_key("leuven")
        self.total_players = max(2, min(MAX_PLAYERS, int(total_players)))
        self.assets_dir = self.theme.assets_dir

        self.layout_cfg = BoardLayout(tile_size=300, gap=2, margin=1)

        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._goose_overlay = None

        self._apply_theme_background()

        if self.theme.key == "leuven":
            self.metas = build_tile_metas_leuven()
        else:
            d = {}
            if goose_theme is not None and self.theme.key == goose_theme.THEME_KEY:
                for k, (n, r, s) in goose_theme.build_tile_metas_generic().items():
                    d[k] = TileMeta(n, r, s)
            else:
                for i in range(1, 37):
                    d[i] = TileMeta(f"Tile {i}", "—", "")
            self.metas = d

        self.tile_pos = tile_positions(self.layout_cfg)
        self.tile_items: dict[int, TileItem] = {}
        self.pawns: dict[int, PawnItem] = {}
        self._anim_groups: list[QtCore.QAbstractAnimation] = []

        self._build_board()
        self._build_center_panel()
        self._build_pawns()

        if goose_theme is not None and self.theme.key == goose_theme.THEME_KEY:
            self._install_goose_overlay()

        self._fit_scene()

    def _theme_path(self, *parts) -> str:
        return os.path.join(self.assets_dir, *parts)

    def _apply_theme_background(self):
        bg_path = self._theme_path("goose-fishing.gif")
        if os.path.exists(bg_path):
            self._bg_movie = QtGui.QMovie(bg_path)
            self._bg_movie.frameChanged.connect(self._on_bg_frame)
            self._bg_movie.start()
            return
        else:
            if self.theme.key == "goose":
                self.setBackgroundBrush(QtGui.QColor("#F6F1E9"))
            else:
                self.setBackgroundBrush(QtGui.QColor("#F6F1E9"))

    def _on_bg_frame(self):
        pm = self._bg_movie.currentPixmap()
        if not pm.isNull():
            scene_rect = self.scene.sceneRect()
            if not scene_rect.isEmpty():
                pm = pm.scaled(int(scene_rect.width()), int(scene_rect.height()),
                               QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
            self.setBackgroundBrush(QtGui.QBrush(pm))
    
    def _install_goose_overlay(self):
        gif_path = self._theme_path("goose.gif")
        if not os.path.exists(gif_path):
            return

        if self._goose_overlay is not None:
            try:
                self._goose_overlay.stop()
            except Exception:
                pass
            self._goose_overlay = None

        tile_items_list = list(self.tile_items.values())
        self._goose_overlay = goose_theme.GooseOverlay(
            self,
            lambda: goose_theme.inner_rect_from_tile_items(tile_items_list, pad=10)
        )
        self._goose_overlay.start(
            gif_path=gif_path,
            size_a=int(self.layout_cfg.tile_size * 0.23), #goose size
            size_b=int(self.layout_cfg.tile_size * 0.2),
        )

    def set_center_image(self, pix: QtGui.QPixmap | None):
        r = self._center_target_rect
        if pix is None or pix.isNull():
            self.center_pix.setPixmap(QtGui.QPixmap())
            self.center_pix.setPos(r.x(), r.y())
            return

        scaled = pix.scaled(int(r.width()), int(r.height()),
                            QtCore.Qt.KeepAspectRatio,
                            QtCore.Qt.SmoothTransformation)
        self.center_pix.setPixmap(scaled)
        x = r.x() + (r.width() - scaled.width()) / 2
        y = r.y() + (r.height() - scaled.height()) / 2
        self.center_pix.setPos(x, y)

    def _center_panel_rect(self) -> QtCore.QRectF:
        ts, g, m = self.layout_cfg.tile_size, self.layout_cfg.gap, self.layout_cfg.margin
        board_w = m * 2 + self.layout_cfg.top_count * ts + (self.layout_cfg.top_count - 1) * g
        board_h = m * 2 + (self.layout_cfg.right_count + 2) * ts + (self.layout_cfg.right_count + 1) * g

        w = ts * 1.5
        h = ts * 1.5

        x = (board_w - w) / 2
        y = (board_h - h) / 2
        return QtCore.QRectF(x, y, w, h)

    def _build_center_panel(self):
        r = self._center_panel_rect()
        self._center_target_rect = r

        self.center_bg = QtWidgets.QGraphicsRectItem(r)
        self.center_bg.setBrush(QtCore.Qt.NoBrush)
        self.center_bg.setPen(QtCore.Qt.NoPen)
        self.center_bg.setZValue(10)
        self.scene.addItem(self.center_bg)

        self.center_pix = QtWidgets.QGraphicsPixmapItem()
        self.center_pix.setZValue(15)
        self.scene.addItem(self.center_pix)

        center_path = self._theme_path("center.png")
        if os.path.exists(center_path):
            self.set_center_image(QtGui.QPixmap(center_path))
        else:
            self.set_center_image(None)

    def _fit_scene(self):
        rect = self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        self.scene.setSceneRect(rect)
        self.fitInView(rect, QtCore.Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_scene()
        if self._goose_overlay is not None:
            self._goose_overlay.clamp_into_allowed()

    def _load_tile_pix(self, tile_id: int) -> QtGui.QPixmap:
        p_num = self._theme_path("tiles", f"{tile_id:02d}.png")
        if os.path.exists(p_num):
            return QtGui.QPixmap(p_num)
        p_single = self._theme_path("tiles", "tile.png")
        if os.path.exists(p_single):
            return QtGui.QPixmap(p_single)
        return QtGui.QPixmap()

    def _load_pawn_pix(self, player_id: int) -> QtGui.QPixmap:
        p = self._theme_path("pawns", f"p{player_id}.png")
        if os.path.exists(p):
            return QtGui.QPixmap(p)
        shared = os.path.join(ASSETS_DIR, "pawns", f"p{player_id}.png")
        if os.path.exists(shared):
            return QtGui.QPixmap(shared)
        return QtGui.QPixmap()

    def _build_board(self):
        size = self.layout_cfg.tile_size
        for tile_id in range(1, 37):
            meta = self.metas.get(tile_id, TileMeta(f"Tile {tile_id}", "—", ""))
            pix = self._load_tile_pix(tile_id)
            item = TileItem(tile_id, meta, pix, size)
            p = self.tile_pos[tile_id]
            item.setPos(p)
            self.scene.addItem(item)
            self.tile_items[tile_id] = item

 

    def _build_pawns(self):
        pawn_size = int(self.layout_cfg.tile_size * 0.42)

        offsets = {
            1: QtCore.QPointF(6, 6),
            2: QtCore.QPointF(self.layout_cfg.tile_size - pawn_size - 6, 6),
            3: QtCore.QPointF(6, self.layout_cfg.tile_size - pawn_size - 6),
            4: QtCore.QPointF(
                self.layout_cfg.tile_size - pawn_size - 6,
                self.layout_cfg.tile_size - pawn_size - 6
            ),
        }
        for pid in range(1, self.total_players + 1):
            pix = self._load_pawn_pix(pid)

            if pix.isNull():
                pm = QtGui.QPixmap(pawn_size, pawn_size)
                pm.fill(QtCore.Qt.transparent)

                painter = QtGui.QPainter(pm)
                painter.setRenderHint(QtGui.QPainter.Antialiasing)

                fill = QtGui.QColor("#ff3b30") if pid == 1 else QtGui.QColor("#00c7ff")

                painter.setBrush(fill)
                painter.setPen(QtGui.QPen(QtGui.QColor("white"), 3))
                painter.drawEllipse(3, 3, pawn_size - 6, pawn_size - 6)
                painter.end()

                pix = pm

            pawn = PawnItem(pid, pix, pawn_size)
            pawn.setOffset(0, 0)
            pawn.setPos(self.tile_pos[1] + offsets[pid])
            self.scene.addItem(pawn)
            self.pawns[pid] = pawn

        self._pawn_offsets = offsets



    def tile_anchor_for_player(self, tile_id: int, player_id: int) -> QtCore.QPointF:
        base = self.tile_pos.get(tile_id, self.tile_pos[1])
        return base + self._pawn_offsets.get(player_id, QtCore.QPointF(6, 6))

    def animate_move(self, player_id: int, from_tile: int, to_tile: int, duration_ms: int = 450):
        pawn = self.pawns.get(player_id)
        if pawn is None:
            return

        start = self.tile_anchor_for_player(from_tile, player_id)
        end = self.tile_anchor_for_player(to_tile, player_id)

        proxy = _AnimProxy(pawn)
        anim = QtCore.QPropertyAnimation(proxy, b"pos", self)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(duration_ms)
        anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)

        self._anim_groups.append(anim)
        anim.finished.connect(lambda: self._cleanup_anim(anim))
        anim.start()

    def _cleanup_anim(self, anim):
        try:
            self._anim_groups.remove(anim)
        except ValueError:
            pass

    def sync_from_state(self, positions: dict[int, int], previous: dict[int, int] | None = None,
                        last_player: int | None = None):
        if last_player is not None and previous is not None:
            old_t = int(previous.get(last_player, 1) or 1)
            new_t = int(positions.get(last_player, 1) or 1)
            old_t = max(1, min(36, old_t))
            new_t = max(1, min(36, new_t))
            if old_t != new_t:
                self.animate_move(last_player, old_t, new_t)

        for pid, pawn in self.pawns.items():
            t = int(positions.get(pid, 1) or 1)
            t = max(1, min(36, t))
            pawn.setPos(self.tile_anchor_for_player(t, pid))

    def flash_tile(self, tile_id: int, times: int = 3):
        item = self.tile_items.get(tile_id)
        if not item:
            return
        original = item.opacity()

        def step(k=0):
            if k >= times * 2:
                item.setOpacity(original)
                return
            item.setOpacity(0.4 if (k % 2 == 0) else 1.0)
            QtCore.QTimer.singleShot(120, lambda: step(k + 1))

        step(0)

    def closeEvent(self, event):
        if self._goose_overlay is not None:
            try:
                self._goose_overlay.stop()
            except Exception:
                pass
        super().closeEvent(event)

    def __del__(self):
        try:
            if self._goose_overlay is not None:
                self._goose_overlay.stop()
        except Exception:
            pass

# =============================
# Game dashboard screen (File 1 layout + File 2 logic)
# =============================
class GameScreen(QtWidgets.QWidget):
    back_to_start = QtCore.Signal()

    def __init__(self, theme: Theme, total_players: int = 2):
        super().__init__()
        self.setObjectName("GameScreen")
        self.theme = theme
        self.total_players = max(2, min(MAX_PLAYERS, int(total_players)))

        self.positions = {i: 1 for i in range(1, self.total_players + 1)}
        self.previous_positions = {i: 1 for i in range(1, self.total_players + 1)}
        self.active_player = 1
        self.last_dice: Optional[int] = None
        self.last_player: Optional[int] = None

        # From File 2: rule engine derived state
        self.skips: Dict[int, int] = {i: 0 for i in range(1, self.total_players + 1)}
        self.backflags: Dict[int, bool] = {i: False for i in range(1, self.total_players + 1)}

        self._last_action_base = "Waiting for data…"
        self._next_prompt = ""

        self._rule_action_override: Optional[str] = None
        self._rule_special_override: Optional[str] = None
        self._winner_dialog: Optional[WinDialog] = None

        # Special tile names for quick lookup
        self.special_tiles = {
            4:  "Ladeuzeplein",
            5: "Quiz Challenge",
            6:  "M-Museum Leuven",
            9:  "Sint-Pieters Church",
            14: "Oude Markt",
            16: "Groot Begijnhof",
            20: "Kasteel van Arenberg",
            22: "Bibliotheek 2Bergen",
            26: "STUK Arts Centre",
            28: "Downtown Jack",
            31: "Sportoase Leuven",
            35: "Cinema ZED",
            36: "Group T Leuven",
        }

        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()

        back_btn = QtWidgets.QPushButton("← Back to start")
        back_btn.setObjectName("GhostButton")
        back_btn.setFixedHeight(36)
        back_btn.clicked.connect(self.back_to_start.emit)

        self.dev_toggle_btn = QtWidgets.QPushButton("Hide dev panel")
        self.dev_toggle_btn.setObjectName("GhostButton")
        self.dev_toggle_btn.setFixedHeight(36)
        self.dev_toggle_btn.clicked.connect(self.toggle_dev_panel)

        self.info_card = QtWidgets.QGroupBox("Current turn")
        self.info_card.setObjectName("Card")
        self.info_card.setMaximumHeight(118)
        info_layout = QtWidgets.QGridLayout(self.info_card)
        info_layout.setContentsMargins(14, 10, 14, 12)
        info_layout.setHorizontalSpacing(18)
        info_layout.setVerticalSpacing(4)

        lbl_turn_cap = QtWidgets.QLabel("Active player")
        lbl_dice_cap = QtWidgets.QLabel("Last roll")
        lbl_special_cap = QtWidgets.QLabel("Special tile")
        lbl_action_cap = QtWidgets.QLabel("Last action")

        self.lbl_turn = QtWidgets.QLabel(player_display_tag(1))
        self.lbl_dice = QtWidgets.QLabel("—")
        self.lbl_special = QtWidgets.QLabel("—")
        self.lbl_action = QtWidgets.QLabel("Waiting for data…")
        self.lbl_action.setWordWrap(True)

        for cap in (lbl_turn_cap, lbl_dice_cap, lbl_special_cap, lbl_action_cap):
            cap.setObjectName("Info")

        info_layout.addWidget(lbl_turn_cap, 0, 0)
        info_layout.addWidget(lbl_dice_cap, 0, 1)
        info_layout.addWidget(lbl_special_cap, 0, 2)
        info_layout.addWidget(lbl_action_cap, 0, 3)
        info_layout.addWidget(self.lbl_turn, 1, 0)
        info_layout.addWidget(self.lbl_dice, 1, 1)
        info_layout.addWidget(self.lbl_special, 1, 2)
        info_layout.addWidget(self.lbl_action, 1, 3)
        info_layout.setColumnStretch(3, 1)

        self.players_card = QtWidgets.QGroupBox("Players")
        self.players_card.setObjectName("Card")
        self.players_card.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        players_layout = QtWidgets.QGridLayout(self.players_card)
        players_layout.setContentsMargins(10, 12, 10, 10)
        players_layout.setSpacing(10)

        self.player_widgets: Dict[int, Dict[str, Any]] = {}
        for i in range(1, self.total_players + 1):
            frame = QtWidgets.QGroupBox(player_display_tag(i))
            frame.setObjectName("PlayerCard")
            v = QtWidgets.QVBoxLayout(frame)
            v.setContentsMargins(10, 16, 10, 10)
            v.setSpacing(5)

            lbl_pos = QtWidgets.QLabel("Tile: 1")
            lbl_pos.setObjectName("BodyLabel")

            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 36)
            bar.setValue(1)
            bar.setFormat("%v / 36")
            bar.setStyleSheet(
                "QProgressBar{border:1px solid #e2e8f0;border-radius:8px;height:20px;text-align:center;background:#ffffff;}"
                f"QProgressBar::chunk{{background:{PLAYER_COLORS.get(i, '#0ea5e9')};border-radius:8px;}}"
            )

            lbl_status = QtWidgets.QLabel("Status: playing")
            lbl_status.setObjectName("Info")

            v.addWidget(lbl_pos)
            v.addWidget(bar)
            v.addWidget(lbl_status)

            row = (i - 1) // 2
            col = (i - 1) % 2
            players_layout.addWidget(frame, row, col)

            self.player_widgets[i] = {"frame": frame, "pos": lbl_pos, "bar": bar, "status": lbl_status}

        self.meta_card = QtWidgets.QGroupBox("Connection")
        self.meta_card.setObjectName("Card")
        self.meta_card.setMinimumHeight(130)
        self.meta_card.setMaximumHeight(160)
        meta_layout = QtWidgets.QVBoxLayout(self.meta_card)
        meta_layout.setContentsMargins(12, 14, 12, 12)
        meta_layout.setSpacing(4)
        meta_label = QtWidgets.QLabel("Serial / camera")
        meta_label.setObjectName("Info")
        self.lbl_serial_status = QtWidgets.QLabel("Setting up connection…")
        self.lbl_serial_status.setWordWrap(True)
        meta_layout.addWidget(meta_label)
        meta_layout.addWidget(self.lbl_serial_status)

        self.log_card = QtWidgets.QGroupBox("Game log")
        self.log_card.setObjectName("Card")
        log_layout = QtWidgets.QVBoxLayout(self.log_card)
        log_layout.setContentsMargins(10, 14, 10, 10)
        self.log = QtWidgets.QTextEdit()
        self.log.setMaximumHeight(220)
        self.log.setReadOnly(True)
        self.log.setObjectName("TerminalLog")
        log_layout.addWidget(self.log)

        self.chat_card = QtWidgets.QGroupBox("Voice Chat")
        self.chat_card.setObjectName("ChatCard")
        self.chat_card.setMinimumHeight(260)
        self.chat_card.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        chat_layout = QtWidgets.QVBoxLayout(self.chat_card)
        chat_layout.setContentsMargins(12, 18, 12, 12)
        chat_layout.setSpacing(10)

        chat_header = QtWidgets.QFrame()
        chat_header.setObjectName("ChatHeader")
        chat_header_layout = QtWidgets.QHBoxLayout(chat_header)
        chat_header_layout.setContentsMargins(10, 8, 10, 8)
        chat_header_layout.setSpacing(8)

        self.chat_assistant_avatar = QtWidgets.QLabel(assistant_emoji_for_emotion("neutral"))
        self.chat_assistant_avatar.setObjectName("ChatAvatarAssistant")
        self.chat_assistant_avatar.setAlignment(QtCore.Qt.AlignCenter)
        self.chat_assistant_avatar.setFixedSize(34, 34)

        chat_title_wrap = QtWidgets.QVBoxLayout()
        chat_title_wrap.setContentsMargins(0, 0, 0, 0)
        chat_title_wrap.setSpacing(1)
        chat_title = QtWidgets.QLabel("AI assistant")
        chat_title.setObjectName("ChatHeaderTitle")
        self.chat_emotion_label = QtWidgets.QLabel("neutral")
        self.chat_emotion_label.setObjectName("ChatEmotionPill")
        chat_title_wrap.addWidget(chat_title)
        chat_title_wrap.addWidget(self.chat_emotion_label, alignment=QtCore.Qt.AlignLeft)

        chat_header_layout.addWidget(self.chat_assistant_avatar)
        chat_header_layout.addLayout(chat_title_wrap)
        chat_header_layout.addStretch(1)

        self.chat_view = ChatFeed()
        self.chat_view.setMinimumHeight(210)
        self.chat_view.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)

        chat_layout.addWidget(chat_header)
        chat_layout.addWidget(self.chat_view, stretch=1)
        self.chat_card.setVisible(False)

        self.dev_panel_wrap = QtWidgets.QWidget()
        dev_panel_layout = QtWidgets.QVBoxLayout(self.dev_panel_wrap)
        dev_panel_layout.setContentsMargins(0, 0, 0, 0)
        dev_panel_layout.setSpacing(10)
        dev_panel_layout.addWidget(self.meta_card)
        dev_panel_layout.addWidget(self.log_card)

        top_row = QtWidgets.QHBoxLayout()
        top_row.addWidget(self.info_card, stretch=1)

        # Board view with theme
        self.board = BoardView(self, theme=self.theme, total_players=self.total_players)
        self.board.setMinimumSize(520, 420)

        side = QtWidgets.QVBoxLayout()
        side.setSpacing(10)
        side.addWidget(self.players_card)
        side.addWidget(self.dev_panel_wrap)
        side.addWidget(self.chat_card, stretch=1)
        side.addStretch(0)
        self.meta_card.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)

        side_wrap = QtWidgets.QWidget()
        side_wrap.setLayout(side)
        side_wrap.setMinimumWidth(340)
        side_wrap.setMaximumWidth(480)

        middle_row = QtWidgets.QHBoxLayout()
        middle_row.setSpacing(10)
        middle_row.addWidget(self.board, stretch=10)
        middle_row.addWidget(side_wrap, stretch=3)

        header.addWidget(self.info_card, stretch=1)
        header.addWidget(self.dev_toggle_btn, alignment=QtCore.Qt.AlignTop)
        header.addWidget(back_btn, alignment=QtCore.Qt.AlignTop)

        root.addLayout(header)
        root.addLayout(middle_row, 1)

    def toggle_dev_panel(self):
        visible = self.dev_panel_wrap.isVisible()
        self.dev_panel_wrap.setVisible(not visible)
        self.chat_card.setVisible(visible)
        self.dev_toggle_btn.setText("Show dev panel" if visible else "Hide dev panel")

    def set_serial_status(self, text: str):
        self.lbl_serial_status.setText(text)

    def append_log(self, text: str):
        self.log.append(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def set_assistant_emotion(self, emotion: str):
        label = assistant_emotion_label(emotion)
        self.chat_assistant_avatar.setText(assistant_emoji_for_emotion(label))
        self.chat_emotion_label.setText(label)

    def append_chat_message(self, role: str, message: str, emotion: str = ""):
        role = (role or "system").lower()
        message = (message or "").strip()
        if not message:
            return
        self.chat_view.append_message(role, message, emotion)

    def set_next_prompt(self, text: str):
        self._next_prompt = text or ""
        self._apply_action_label()

    def _set_last_action_base(self, text: str):
        self._last_action_base = text or ""
        self._apply_action_label()

    def _apply_action_label(self):
        base = self._last_action_base or ""
        prompt = self._next_prompt or ""
        if prompt and base:
            self.lbl_action.setText(base + "\n" + prompt)
        elif prompt:
            self.lbl_action.setText(prompt)
        else:
            self.lbl_action.setText(base)

    def update_from_pos(self, data: dict):
        for i in range(1, self.total_players + 1):
            key = f"p{i}"
            pv = data.get(key)
            if pv is not None:
                self.previous_positions[i] = self.positions.get(i, 1)
                self.positions[i] = int(pv)
        active = data.get("active")
        dice = data.get("dice")
        last_player = data.get("last_player")

        if active is not None:
            self.active_player = int(active)
        if dice is not None:
            self.last_dice = int(dice)
        if last_player is not None:
            self.last_player = int(last_player)

        self._refresh_players()
        self._refresh_header()

        # Sync board visuals + animation
        self.board.sync_from_state(self.positions, previous=self.previous_positions,
                                   last_player=self.last_player)

    # From File 2: full rule engine integration
    def apply_rule_engine(self, eng: RuleEngine):
        self.skips = dict(eng.turns_to_skip)
        self.backflags = dict(eng.move_backwards_flag)

        self._rule_action_override = None
        self._rule_special_override = None

        if (
            self.last_player not in (None, 0)
            and self.last_dice is not None
            and eng.last_moved_player == self.last_player
            and eng.last_dice == self.last_dice
        ):
            p = eng.last_moved_player
            dice = eng.last_dice
            old = eng.last_old_pos
            landed = eng.last_landed_pos
            final = eng.last_final_pos

            if eng.last_was_skip_turn or (dice == 0):
                action = f"{player_display_tag(p)} skipped a turn."
            else:
                if landed != final:
                    action = f"{player_display_tag(p)} rolled {dice} and went from tile {old} to {landed} → rule sends to tile {final}."
                else:
                    action = f"{player_display_tag(p)} rolled {dice} and moved from tile {old} to {final}."

            if eng.last_repeat_turn:
                action += " Roll again!"
            if landed == 16:
                action += " (Next move: BACKWARDS)"

            self._rule_action_override = action

            if landed in self.special_tiles:
                self._rule_special_override = f"{self.special_tiles[landed]} (tile {landed})"
            else:
                self._rule_special_override = "—"

        self._refresh_players()
        self._refresh_header()

    def _refresh_players(self):
        for i in range(1, self.total_players + 1):
            w = self.player_widgets[i]
            pos = max(1, min(36, int(self.positions.get(i, 1) or 1)))
            w["pos"].setText(f"Tile: {pos}")
            w["bar"].setValue(pos)

            # From File 2: show skip/backwards status
            if self.skips.get(i, 0) > 0:
                w["status"].setText(f"Status: skipping ({self.skips[i]} turn(s) left)")
            elif self.backflags.get(i, False):
                w["status"].setText("Status: next move BACKWARDS")
            else:
                w["status"].setText("Status: playing")

            if i == self.active_player:
                accent = PLAYER_COLORS.get(i, "#0ea5e9")
                w["frame"].setStyleSheet("""
                    QGroupBox { border: 3px solid """ + accent + """; border-radius: 14px; margin-top: 18px; }
                    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0f172a; }
                """)
            else:
                accent = PLAYER_COLORS.get(i, "#e2e8f0")
                w["frame"].setStyleSheet("""
                    QGroupBox { border: 1px solid """ + accent + """; border-radius: 14px; margin-top: 18px; }
                    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0f172a; }
                """)

    def _refresh_header(self):
        self.lbl_turn.setText(player_display_tag(self.active_player))
        self.lbl_special.setText("—")

        if self.last_player in (None, 0):
            self.lbl_dice.setText("—")
            self._set_last_action_base("Waiting for data…")
            self.lbl_special.setText("—")
            return

        if self.last_dice is not None:
            if int(self.last_dice) == 0:
                self.lbl_dice.setText("—")
                self._set_last_action_base(f"{player_display_tag(self.last_player)} skipped a turn.")
                self.lbl_special.setText("—")
            else:
                self.lbl_dice.setText(str(self.last_dice))
                old_pos = self.previous_positions.get(self.last_player)
                new_pos = self.positions.get(self.last_player)
                if old_pos is not None and new_pos is not None:
                    self._set_last_action_base(
                        f"{player_display_tag(self.last_player)} rolled {self.last_dice} and moved from tile {old_pos} to {new_pos}."
                    )
                else:
                    self._set_last_action_base(f"{player_display_tag(self.last_player)} rolled {self.last_dice}.")

                if new_pos in self.special_tiles:
                    self.lbl_special.setText(f"{self.special_tiles[new_pos]} (tile {new_pos})")
                    self.board.flash_tile(int(new_pos))
                else:
                    self.lbl_special.setText("—")
        else:
            self.lbl_dice.setText("—")
            self._set_last_action_base("Waiting for data…")
            self.lbl_special.setText("—")

        # Rule engine overrides (richer text from File 2)
        if self._rule_action_override:
            self._set_last_action_base(self._rule_action_override)
        if self._rule_special_override is not None:
            self.lbl_special.setText(self._rule_special_override)

    def show_winner(self, winner: int):
        w = self.player_widgets.get(winner)
        if w:
            w["status"].setText("Status: WINNER 🏆")
        if self._winner_dialog is not None:
            try:
                self._winner_dialog.close()
            except Exception:
                pass
            self._winner_dialog = None

        dlg = WinDialog(winner, self)
        self._winner_dialog = dlg
        dlg.setModal(False)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        QtCore.QTimer.singleShot(1800, dlg.close)


# =============================
# Main window (shell — File 1 theme system + File 2 game logic)
# =============================
class Shell(QtWidgets.QMainWindow):
    def __init__(self, serial_cfg: SerialConfig):
        super().__init__()
        self.setWindowTitle("GAME4 Companion – Live Dashboard")
        self.resize(1000, 700)

        self.serial_cfg = serial_cfg
        self.msg_queue: queue.Queue = queue.Queue()
        self.worker: SerialWorker | None = None
        self.cam_cfg = CameraConfig()
        self.cam_worker: DiceCameraWorker | None = None

        self.human_players = 1
        self.total_players = AI_PLAYER_ID + self.human_players

        # Rule engine (from File 2)
        self.rules_engine = RuleEngine(self.total_players)

        self._serial_status_line = "Not connected."
        self._cam_status_line = "Camera: —"
        self._cam_ready = False
        self._cam_value: int | None = None
        self._cam_armed = True

        # Robust dice lock (from File 2)
        self._dice_lock = False
        self._expected_last_player: int | None = None
        self._expected_dice_value: int | None = None
        self._last_pos_sig: Optional[Tuple[int, int, int, int, int, int, int]] = None
        self._lock_sent_pos_sig: Optional[Tuple[int, int, int, int, int, int, int]] = None
        self._lock_set_at = 0.0
        self._lock_timeout_s = 10.0

        # Quiz gate state (from File 2)
        self._quiz_pending = False
        self._quiz_player: int | None = None

        # Special tile popup (from File 2)
        self._evt_popup: Optional[SpecialTilePopup] = None
        self._display_role: Optional[str] = None
        self._assistant_emotion = "neutral"

        self.current_theme: Theme = theme_by_key("leuven")

        self.stack = QtWidgets.QStackedWidget()
        self.setCentralWidget(self.stack)

        self.start_screen = StartScreen(self.serial_cfg)
        self.game_screen = GameScreen(self.current_theme, self.total_players)

        self.stack.addWidget(self.start_screen)
        self.stack.addWidget(self.game_screen)

        self.start_screen.start_clicked.connect(self.start_game)
        self.game_screen.back_to_start.connect(self.stop_and_back)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll_queue)
        self.timer.start(80)

        self.space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self)
        self.space_shortcut.setContext(QtCore.Qt.ApplicationShortcut)
        self.space_shortcut.activated.connect(self._on_space)

        self._build_menu()
        self.setStyleSheet(self._qss())

    def _update_connection_card(self):
        combined = f"{self._serial_status_line}\n{self._cam_status_line}"
        self.game_screen.set_serial_status(combined)

    def _consume_display_log_line(self, line: str):
        if not line:
            return

        m_emotion = re.search(r"Display:\s*SetEmotion:\s*([A-Za-z0-9_-]+)", line, flags=re.IGNORECASE)
        if m_emotion:
            self._assistant_emotion = assistant_emotion_label(m_emotion.group(1))
            self.game_screen.set_assistant_emotion(self._assistant_emotion)
            return

        m_role = re.search(r"Display:\s*Role:\s*(assistant|user|system)", line, flags=re.IGNORECASE)
        if m_role:
            self._display_role = m_role.group(1).lower()
            return

        if "Display:" not in line:
            return

        msg = line.split("Display:", 1)[1].strip()
        if not msg:
            return
        if msg.startswith("Role:"):
            return
        if msg.startswith("SetStatus:") or msg.startswith("SetEmotion:"):
            return

        role = self._display_role or "system"
        emotion = self._assistant_emotion if role == "assistant" else ""
        self.game_screen.append_chat_message(role, msg, emotion)

    def _build_menu(self):
        bar = self.menuBar()
        app_menu = bar.addMenu("App")

        act_home = QtGui.QAction("Go to start", self)
        act_home.triggered.connect(lambda: self.stack.setCurrentIndex(0))
        app_menu.addAction(act_home)

        act_dashboard = QtGui.QAction("Go to dashboard", self)
        act_dashboard.triggered.connect(lambda: self.stack.setCurrentIndex(1))
        app_menu.addAction(act_dashboard)

        app_menu.addSeparator()
        act_quit = QtGui.QAction("Quit", self)
        act_quit.triggered.connect(QtWidgets.QApplication.instance().quit)
        app_menu.addAction(act_quit)

    def _rebuild_game_screen(self, theme: Theme):
        old = self.game_screen
        self.game_screen = GameScreen(theme, self.total_players)
        self.game_screen.back_to_start.connect(self.stop_and_back)

        self.stack.removeWidget(old)
        old.deleteLater()

        self.stack.addWidget(self.game_screen)
        self.stack.setCurrentIndex(0)
        self._update_connection_card()
        self.game_screen.set_assistant_emotion(self._assistant_emotion)

    def start_game(self, theme_key: str, human_players: int):
        self.human_players = max(1, min(MAX_HUMAN_PLAYERS, int(human_players)))
        self.total_players = AI_PLAYER_ID + self.human_players
        self.rules_engine = RuleEngine(self.total_players)

        new_theme = theme_by_key(theme_key)
        need_rebuild = (new_theme.key != self.current_theme.key)
        if need_rebuild:
            self.current_theme = new_theme
            self._rebuild_game_screen(self.current_theme)
        else:
            # Still rebuild when player count changed so board/cards match.
            self._rebuild_game_screen(self.current_theme)

        if self.worker is None or not self.worker.is_alive():
            self.worker = SerialWorker(self.serial_cfg, self.msg_queue)
            self.worker.start()
            self._serial_status_line = "Setting up connection…"
            self.start_screen.set_status(self._serial_status_line)

        if self.cam_worker is None or not self.cam_worker.is_alive():
            self.cam_worker = DiceCameraWorker(self.cam_cfg, self.msg_queue)
            self.cam_worker.start()

        if self.worker is not None:
            self.worker.send_line(f"CFG:HUMANS:{self.human_players}")
            self.start_screen.set_status(
                f"Configured board: AI + {self.human_players} human(s) (total {self.total_players})."
            )

        self._dice_lock = False
        self._expected_last_player = None
        self._expected_dice_value = None
        self._lock_sent_pos_sig = None

        self._update_connection_card()
        self._set_prompt_for_active_player()

        self.stack.setCurrentIndex(1)

        settings = QtCore.QSettings("MyCompany", "GAME4Companion")
        show_tut = settings.value("show_tutorial", True, type=bool)

        if show_tut:

            tutorial_steps = [
                {
                    "widget": self.game_screen.board,
                    "text": "Welcome to the game! This is the main board. Your pawns will move around this track automatically."
                },
                {
                    "widget": self.game_screen.players_card,
                    "text": "Here you can see the players. It shows what tile they are on, how close they are to the finish, and if they are stuck skipping a turn."
                },
                {
                    "widget": self.game_screen.info_card,
                    "text": "This is the action panel. Keep an eye on 'Last action', it will tell you when it's your turn to roll the dice under the camera and press SPACE!"
                }
            ]

            self.tutorial_overlay = TutorialOverlay(self, tutorial_steps, settings)
            self.tutorial_overlay.setGeometry(self.rect())
            self.tutorial_overlay.show()

            self.start_screen.tutorial_checkbox.setChecked(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tutorial_overlay') and self.tutorial_overlay.isVisible():
            self.tutorial_overlay.setGeometry(self.rect())

    def stop_and_back(self):
        self.stack.setCurrentIndex(0)

    # From File 2: smarter prompt that knows about skips/backflags/quiz
    def _set_prompt_for_active_player(self):
        ap = self.game_screen.active_player
        skip_left = self.game_screen.skips.get(ap, 0)
        backflag = self.game_screen.backflags.get(ap, False)
        who = player_display_tag(ap)

        hint = []
        if backflag:
            hint.append("BACKWARDS")
        suffix = f" ({', '.join(hint)})" if hint else ""

        if self._dice_lock:
            self.game_screen.set_next_prompt("Sent. Waiting for board update…")
            return

        if self._quiz_pending:
            qp = self._quiz_player if self._quiz_player is not None else "?"
            qp_name = player_display_tag(qp) if isinstance(qp, int) and qp >= 1 else f"Player {qp}"
            self.game_screen.set_next_prompt(
                f"Quiz pending for {qp_name}: wait for voice-assistant judgement."
            )
            return

        if skip_left > 0:
            self.game_screen.set_next_prompt(f"{who} will be skipped by the board…")
            return

        if not self._cam_armed:
            self.game_screen.set_next_prompt("Remove dice / roll again for next player.")
            return

        if self._cam_ready and self._cam_value is not None:
            self.game_screen.set_next_prompt(f"{who}{suffix}: dice detected -> press SPACE or say \"confirm dice\".")
        else:
            self.game_screen.set_next_prompt(f"{who}{suffix}: roll dice in view (still), then press SPACE or say \"confirm dice\".")

    def _on_space(self):
        if self.stack.currentIndex() != 1:
            return

        if self.worker is None or not self.worker.is_alive():
            self.game_screen.append_log("⚠️ SPACE: not connected to ESP32.")
            self.game_screen.set_next_prompt("Connect first, then press SPACE.")
            return

        if self._quiz_pending:
            self.game_screen.append_log("Quiz is pending. Dice input is locked until quiz is resolved.")
            self._set_prompt_for_active_player()
            return

        ap = self.game_screen.active_player
        who = player_display_tag(ap)
        if self.game_screen.skips.get(ap, 0) > 0:
            self.game_screen.append_log(f"⏭️ {who} must skip - board will handle it automatically.")
            self._set_prompt_for_active_player()
            return

        if self.cam_worker is None or not self.cam_worker.is_alive():
            self.game_screen.append_log("⚠️ SPACE: camera worker not running.")
            self.game_screen.set_next_prompt("Camera not running.")
            return

        if self._dice_lock:
            self.game_screen.append_log("⏳ SPACE: waiting for ESP32 to confirm previous action…")
            self.game_screen.set_next_prompt("Waiting for board update…")
            return

        value = self.cam_worker.get_ready_value()
        if value is None:
            self.game_screen.append_log("❌ SPACE: no stable dice value detected.")
            self._set_prompt_for_active_player()
            return

        self.worker.send_line(f"DICE:{value}")
        self._dice_lock = True
        self._expected_last_player = int(ap)
        self._expected_dice_value = int(value)
        self._lock_sent_pos_sig = self._last_pos_sig
        self._lock_set_at = time.time()

        self.cam_worker.consume()

        self.game_screen.append_log(f"🎲 {who} confirmed roll = {value} (sent to ESP32)")
        self.game_screen.set_next_prompt("Sent. Waiting for board update…")

    # From File 2: rich special tile popup
    def _show_special_popup(self, data: dict):
        try:
            player = int(data.get("player", 0))
        except Exception:
            player = 0
        try:
            tile = int(data.get("tile", 0))
        except Exception:
            tile = 0

        name = str(data.get("name", "Special Tile"))
        msg = str(data.get("msg", ""))

        if self._evt_popup is not None:
            try:
                self._evt_popup.close()
            except Exception:
                pass
            self._evt_popup = None

        popup = SpecialTilePopup(player, tile, name, msg, parent=self)
        self._evt_popup = popup
        popup.show()
        popup.raise_()
        popup.activateWindow()
        QtCore.QTimer.singleShot(3000, popup.close)

    # From File 2: quiz popup
    def _show_quiz_popup(self, state: str, player: Optional[int]):
        p = player if player is not None else 0
        if state == "pending":
            name = "Tile 4 - Quiz Challenge"
            msg = f"{player_display_tag(p)} triggered the quiz tile. Waiting for the voice assistant to ask a question."
        elif state == "correct":
            name = "Quiz Passed"
            msg = f"{player_display_tag(p)} answered correctly."
        elif state == "wrong":
            name = "Quiz Failed"
            msg = f"{player_display_tag(p)} failed (wrong). Penalty applied."
        else:
            return

        self._show_special_popup({
            "player": p,
            "tile": 34,
            "name": name,
            "msg": msg,
        })

    # From File 2: robust multi-strategy unlock
    def _unlock_if_confirmed(self, payload: dict, sig):
        if not self._dice_lock:
            return

        now = time.time()

        if (now - self._lock_set_at) > self._lock_timeout_s:
            self.game_screen.append_log("⚠️ Unlock timeout: ESP32 confirmation took too long. Unlocking locally.")
            self._dice_lock = False
            self._expected_last_player = None
            self._expected_dice_value = None
            self._lock_sent_pos_sig = None
            return

        lp = payload.get("last_player")
        incoming_dice = payload.get("dice")
        incoming_active = payload.get("active")

        try:
            if lp is not None and self._expected_last_player is not None:
                if int(lp) == int(self._expected_last_player):
                    self._dice_lock = False
                    self._expected_last_player = None
                    self._expected_dice_value = None
                    self._lock_sent_pos_sig = None
                    return

            if self._lock_sent_pos_sig is not None and sig is not None:
                if sig != self._lock_sent_pos_sig:
                    self._dice_lock = False
                    self._expected_last_player = None
                    self._expected_dice_value = None
                    self._lock_sent_pos_sig = None
                    return

            if incoming_active is not None and self._expected_last_player is not None:
                if int(incoming_active) != int(self._expected_last_player):
                    self._dice_lock = False
                    self._expected_last_player = None
                    self._expected_dice_value = None
                    self._lock_sent_pos_sig = None
                    return

            if incoming_dice is not None and self._expected_dice_value is not None:
                if int(incoming_dice) == int(self._expected_dice_value):
                    self._dice_lock = False
                    self._expected_last_player = None
                    self._expected_dice_value = None
                    self._lock_sent_pos_sig = None
                    return

        except Exception:
            pass

    # From File 2: board reset detection
    def _handle_board_reset(self, payload: dict):
        try:
            lp = int(payload.get("last_player", -1))
            dice = int(payload.get("dice", -1))
            positions = [int(payload.get(f"p{i}", -999)) for i in range(1, self.total_players + 1)]
        except Exception:
            return

        if lp == 0 and dice == 0 and all(p == 1 for p in positions):
            if self._dice_lock:
                self._dice_lock = False
                self._expected_last_player = None
                self._expected_dice_value = None
                self._lock_sent_pos_sig = None
                self.game_screen.append_log("🔄 Board reset detected → unlocking input.")
            if getattr(self.game_screen, "_winner_dialog", None) is not None:
                try:
                    self.game_screen._winner_dialog.close()
                except Exception:
                    pass
                self.game_screen._winner_dialog = None
            self.rules_engine = RuleEngine(self.total_players)
            self.rules_engine.on_pos(payload)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()

                if kind == "log":
                    self._serial_status_line = str(payload)
                    self.start_screen.set_status(self._serial_status_line)
                    self._update_connection_card()
                    self.game_screen.append_log(payload)
                    self._consume_display_log_line(str(payload))

                elif kind == "error":
                    self._serial_status_line = "Error: " + str(payload)
                    self.start_screen.set_status(self._serial_status_line)
                    self._update_connection_card()
                    self.game_screen.append_log("ERROR: " + str(payload))
                    QtWidgets.QMessageBox.critical(self, "Serial error", str(payload))

                elif kind == "cam":
                    armed = bool(payload.get("armed", True))
                    ready = bool(payload.get("ready", False))
                    value = payload.get("value", None)
                    self._cam_armed = armed
                    self._cam_ready = ready and (value is not None)
                    self._cam_value = int(value) if value is not None else None

                    if not armed:
                        self._cam_status_line = "Camera: 🔒 move dice away…"
                    elif self._cam_ready:
                        self._cam_status_line = f"Camera: ✅ Ready ({self._cam_value})"
                    else:
                        self._cam_status_line = "Camera: ❌ No dice / unstable"

                    self._update_connection_card()

                    if self.stack.currentIndex() == 1 and not self._dice_lock:
                        self._set_prompt_for_active_player()

                elif kind == "evt":
                    self._show_special_popup(payload)

                elif kind == "voice_confirm":
                    self.game_screen.append_log("🎤 Voice command requests dice confirmation.")
                    self._on_space()

                elif kind == "quiz":
                    state = str(payload.get("state", ""))
                    self._quiz_player = int(payload.get("player", 0)) if payload.get("player") is not None else None
                    self._quiz_pending = (state == "pending")
                    quiz_player_tag = player_display_tag(self._quiz_player if self._quiz_player is not None else 0)
                    self._show_quiz_popup(state, self._quiz_player)
                    if state == "pending":
                        self.game_screen.append_log(
                            f"QUIZ pending for {quiz_player_tag}. Waiting for voice assistant..."
                        )
                    elif state == "correct":
                        self.game_screen.append_log(f"QUIZ result: {quiz_player_tag} answered correctly.")
                    elif state == "wrong":
                        self.game_screen.append_log(
                            f"QUIZ result: {quiz_player_tag} failed (wrong), penalty applied."
                        )
                    self._set_prompt_for_active_player()

                elif kind == "pos":
                    sig = self.rules_engine._sig_from_pos(payload)
                    self._last_pos_sig = sig

                    self._handle_board_reset(payload)

                    evt_local = self.rules_engine.on_pos(payload)

                    self.game_screen.update_from_pos(payload)
                    self.game_screen.apply_rule_engine(self.rules_engine)

                    if evt_local is not None:
                        self._show_special_popup(evt_local)

                    self._unlock_if_confirmed(payload, sig)

                    if self.stack.currentIndex() == 1:
                        self._set_prompt_for_active_player()

                elif kind == "win":
                    self.game_screen.append_log(f"WINNER: {player_display_tag(int(payload))}")
                    self.game_screen.show_winner(int(payload))

        except queue.Empty:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent):
        if self.cam_worker is not None:
            self.cam_worker.stop()
            time.sleep(0.1)
        if self.worker is not None:
            self.worker.stop()
            time.sleep(0.2)
        super().closeEvent(event)

    def _qss(self) -> str:
        return """
        * { color: #F8F7F3; }

        QMenuBar, QMenu {
            background: #1F3D2B;
            color: #F8F7F3;
        }

        QWidget {
            background: #1F3D2B;
            color: #F8F7F3;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 14px;
        }

        QLabel#Title {
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 38px;
            font-weight: 700;
            color: #F8F7F3;
        }
        QLabel#Subtitle {
            font-size: 16px;
            color: #F6F1E9;
            font-family: "Segoe UI", "Arial", sans-serif;
        }
        QLabel#Info {
            font-size: 12px;
            color: #E8E0CF;
            font-family: "Segoe UI", "Arial", sans-serif;
            background: transparent;
        }

        QLabel#H1 {
            font-size: 24px;
            font-weight: 700;
            color: #F8F7F3;
            font-family: "Segoe UI", "Arial", sans-serif;
        }
        QLabel#FormLabel {
            color: #F8F7F3;
            font-size: 14px;
            font-family: "Segoe UI", "Arial", sans-serif;
            background: transparent;
        }

        QLabel#BodyLabel {
            color: #1F3D2B;
            font-size: 14px;
            font-family: "Segoe UI", "Arial", sans-serif;
            background: transparent;
        }

        QCheckBox#TutorialCheck {
            color: #F8F7F3;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 16px;
            spacing: 10px;
            background: transparent;
        }
        QCheckBox#TutorialCheck::indicator {
            width: 20px;
            height: 20px;
            border: 2px solid #E4C580;
            border-radius: 5px;
            background: rgba(246, 241, 233, 0.08);
        }
        QCheckBox#TutorialCheck::indicator:hover {
            border: 2px solid #E4C580;
        }
        QCheckBox#TutorialCheck::indicator:checked {
            background: #E4C580;
            border: 2px solid #E4C580;
        }

        QPushButton#PrimaryButton {
            background: #E4C580;
            color: #1F3D2B;
            border: none;
            padding: 12px 24px;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 700;
            font-family: "Segoe UI", "Arial", sans-serif;
        }
        QPushButton#PrimaryButton:hover { background: #D9B46A; }
        QPushButton#PrimaryButton:pressed { background: #C79D4C; }

        QPushButton#GhostButton {
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 14px;
            font-weight: 600;
            background: #2E5A3F;
            border: 1px solid #6E8B69;
            color: #F8F7F3;
            padding: 8px 14px;
            border-radius: 10px;
        }
        QPushButton#GhostButton:hover { background: #356849; }

        QGroupBox#Card {
            background: #2E5A3F;
            border: 1px solid #6E8B69;
            border-radius: 14px;
            margin-top: 18px;
        }
        QGroupBox#Card::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #F6F1E9;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-weight: 600;
            background: transparent;
        }

        QGroupBox#PlayerCard {
            background: #FBF7F0;
            border: 1px solid #D8CFAF;
            border-radius: 14px;
            margin-top: 18px;
        }
        QGroupBox#PlayerCard::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #1F3D2B;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-weight: 700;
            background: transparent;
        }

        QProgressBar {
            border: 1px solid #D8CFAF;
            border-radius: 8px;
            height: 18px;
            text-align: center;
            background: #F6F1E9;
            color: #1F3D2B;
        }
        QProgressBar::chunk {
            background: #5D7F57;
            border-radius: 8px;
        }

        QTextEdit {
            background: #FBF7F0;
            color: #1F3D2B;
            border: 1px solid #D8CFAF;
            border-radius: 10px;
            font-family: "Segoe UI", "Arial", sans-serif;
        }

        QTextEdit#TerminalLog {
            background: #183224;
            color: #F6F1E9;
            border: 1px solid #5D7F57;
            border-radius: 10px;
            font-family: Consolas, "Courier New", monospace;
            font-size: 11px;
        }

        QGroupBox#ChatCard {
            background: #F7F2E8;
            border: 1px solid #D9CBA9;
            border-radius: 14px;
            margin-top: 18px;
        }
        QGroupBox#ChatCard::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #F6F1E9;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-weight: 700;
            background: #2E5A3F;
        }

        QFrame#ChatHeader {
            background: #FBF7F0;
            border: 1px solid #E1D4B7;
            border-radius: 12px;
        }
        QLabel#ChatHeaderTitle {
            color: #1F3D2B;
            font-size: 14px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#ChatEmotionPill {
            color: #5E6B57;
            background: #EFE5D1;
            border: 1px solid #D9CBA9;
            border-radius: 8px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
        }
        QScrollArea#ChatFeed {
            background: #F7F2E8;
            border: 1px solid #D9CBA9;
            border-radius: 12px;
        }
        QWidget#ChatFeedContent {
            background: #F7F2E8;
        }
        QLabel#ChatMeta {
            color: #5E6B57;
            font-size: 11px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#ChatAvatarAssistant {
            background: #E8DDC6;
            color: #1F3D2B;
            border: 1px solid #D4C39E;
            border-radius: 17px;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#ChatAvatarUser {
            background: #DCE8FF;
            color: #1B4FD7;
            border: 1px solid #C4D6FF;
            border-radius: 17px;
            font-size: 10px;
            font-weight: 800;
        }
        QLabel#ChatBubbleAssistant {
            background: #FFFFFF;
            color: #1F2933;
            border: 1px solid #E2D6BC;
            border-radius: 14px;
            padding: 9px 12px;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 13px;
        }
        QLabel#ChatBubbleUser {
            background: #2F6BFF;
            color: #FFFFFF;
            border: 1px solid #2459DB;
            border-radius: 14px;
            padding: 9px 12px;
            font-family: "Segoe UI", "Arial", sans-serif;
            font-size: 13px;
        }
        QLabel#ChatBubbleSystem {
            background: #ECE4D3;
            color: #5E6B57;
            border: 1px solid #D9CBA9;
            border-radius: 12px;
            padding: 6px 10px;
            font-size: 12px;
        }

        QComboBox {
            border: 1px solid #D8CFAF;
            background: #FBF7F0;
            border-radius: 10px;
            padding: 6px 10px;
            font-size: 14px;
            color: #1F3D2B;
            min-height: 40px;
            font-family: "Segoe UI", "Arial", sans-serif;
        }
        QComboBox:hover { background: #F3EBDC; }
        QComboBox::drop-down { border: none; width: 28px; }
        QComboBox::down-arrow {
            image: url("data:image/svg+xml;utf8,\
<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'>\
<path d='M0 0 L10 0 L5 6 Z' fill='%231F3D2B'/></svg>");
        }
        QComboBox QAbstractItemView {
            background: #FBF7F0;
            border: 1px solid #D8CFAF;
            selection-background-color: #E4C580;
            selection-color: #1F3D2B;
            color: #1F3D2B;
        }

        QDialog {
            background: #FBF7F0;
        }
        QDialog QLabel {
            color: #1F3D2B;
            background: transparent;
        }
        """

# =============================
# Entrypoint
# =============================
def main():
    import sys
    serial_cfg = SerialConfig(port="COM7", baudrate=115200)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GAME4 Companion – Serial")

    font_path = os.path.join(ASSETS_DIR, "PressStart2P-Regular.ttf")
    if not os.path.exists(font_path):
        print("Font file not found:", font_path)

    font_id = QtGui.QFontDatabase.addApplicationFont(font_path)

    title_family = "Arial"  # fallback
    if font_id != -1:
        families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
        print("Loaded font families:", families)
        if families:
            title_family = families[0]
    else:
        print("Font failed to load")

    win = Shell(serial_cfg)
    win.setStyleSheet(win._qss().replace("__TITLE_FONT__", title_family))
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


