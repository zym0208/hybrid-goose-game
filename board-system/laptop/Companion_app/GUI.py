from __future__ import annotations

from dataclasses import dataclass
import threading
import queue
import json
import time
import os

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
NUM_PLAYERS = 2


# --- Camera deps (optional) ---
try:
    import cv2
    import numpy as np
    from sklearn.cluster import DBSCAN
except Exception:
    cv2 = None
    np = None
    DBSCAN = None


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
    port: str = "COM9"
    baudrate: int = 115200


# =============================
# Camera config (no UI change)
# =============================
ESP32_IP = "192.168.137.172"
STREAM_URL = f"http://{ESP32_IP}/stream"
SHOW_CAMERA_PREVIEW = True


@dataclass
class CameraConfig:
    stream_url: str = STREAM_URL
    show_preview_window: bool = SHOW_CAMERA_PREVIEW

    blur_size: tuple[int, int] = (5, 5)

    stable_frames: int = 2
    rearm_no_dice_s: float = 0.3
    max_ready_age_s: float = 5

    min_area: float = 90
    max_area: float = 400
    min_circularity: float = 0.5
    min_inertia_ratio: float = 0.3

    dbscan_eps: float = 80.0


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
                        self.q.put(("win", winner))
                        self.q.put(("log", f"WIN received: AI {winner}"))
                except json.JSONDecodeError:
                    self.q.put(("log", f"WIN JSON error: {json_str}"))
                continue

            if line.startswith("POS:"):
                json_str = line[4:]
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    self.q.put(("log", f"POS JSON error: {json_str}"))
                    continue

                self.q.put(("pos", data))

                p1 = data.get("p1")
                p2 = data.get("p2")
                active = data.get("active")
                dice = data.get("dice")
                last_player = data.get("last_player")

                log_line = f"POS: p1={p1}, p2={p2}, active={active}"
                if dice is not None:
                    log_line += f", dice={dice}"
                if last_player is not None:
                    log_line += f", last_player={last_player}"
                self.q.put(("log", log_line))
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
        self._last_candidate: int | None = None
        self._stable_count: int = 0
        self._ready_value: int | None = None
        self._ready_since: float = 0.0

        self._armed: bool = True
        self._no_dice_since: float | None = None
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

    def get_ready_value(self) -> int | None:
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

    def _process_frame(self, frame, detector):
        if frame is None:
            return None

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, self.cfg.blur_size, 0)

        keypoints = detector.detect(gray)
        if not keypoints:
            return None

        pts = np.array([kp.pt for kp in keypoints])
        try:
            clustering = DBSCAN(eps=self.cfg.dbscan_eps, min_samples=1).fit(pts)
            labels = clustering.labels_
        except Exception:
            return None

        counts = []
        for cid in np.unique(labels):
            if cid == -1:
                continue
            mask = labels == cid
            counts.append(int(np.sum(mask)))

        if not counts:
            return None

        v = max(counts)
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

        title = QtWidgets.QLabel(f"🏆 AI {winner_index} wins!")
        title.setAlignment(QtCore.Qt.AlignCenter)
        font = title.font()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)

        subtitle = QtWidgets.QLabel(
            "The physical board has a winner.\n"
            "The companion app now shows the win animation."
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
        x1 = 0.5 + 0.5 * __import__('math').sin(t * 2 * 3.14159)
        y1 = 0.5 + 0.5 * __import__('math').cos(t * 2 * 3.14159)

        gradient = QtGui.QLinearGradient(
            rect.width() * x1, rect.height() * y1,
            rect.width() * (1 - x1), rect.height() * (1 - y1)
        )

        gradient.setColorAt(0.0, QtGui.QColor("#FF00C8"))
        gradient.setColorAt(0.5, QtGui.QColor("#7700FF"))
        gradient.setColorAt(1.0, QtGui.QColor("#32005E"))

        painter.fillRect(rect, gradient)
# =============================
# Start screen (theme selector)
# =============================
class StartScreen(AnimatedGradientWidget):
    start_clicked = QtCore.Signal(str)  # theme_key

    def __init__(self, serial_cfg: SerialConfig):
        super().__init__()
        self.serial_cfg = serial_cfg
        self.setObjectName("StartScreen")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        title = QtWidgets.QLabel("GAME4 Companion")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setObjectName("Title")

        subtitle = QtWidgets.QLabel(
            "Live dashboard for the physical board\n"
            "4 fixed AI players • ESP32 • LED strip"
        )
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setObjectName("Subtitle")

        card = QtWidgets.QGroupBox("Connection & game")
        card.setObjectName("Card")
        form = QtWidgets.QFormLayout(card)

        players_label = QtWidgets.QLabel("2 AI players (fixed)")
        
        form.addRow("Players:", players_label)

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

        rules_btn = QtWidgets.QPushButton("How it works")
        rules_btn.setObjectName("GhostButton")
        rules_btn.setFixedHeight(40)
        rules_btn.clicked.connect(self._show_rules)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(card)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)
        layout.addWidget(rules_btn, alignment=QtCore.Qt.AlignRight)
        layout.addStretch(2)

    def _on_start(self):
        key = self.theme_combo.currentData()
        if not key:
            key = "leuven"
        self.start_clicked.emit(str(key))

    def set_status(self, text: str):
        self.status_label.setText(text)

    def _show_rules(self):
        msg = (
            "Companion app for your physical board:\n\n"
            "• 2 AI players move on the board.\n"
            "• The current player rolls a physical dice under the camera.\n"
            "• When the dice lies still in view, press SPACE in the app to confirm.\n"
            "• The app sends DICE:1–6 to the ESP32 which updates pawn positions.\n"
            "• This app shows:\n"
            "   – which AI is currently on turn\n"
            "   – the positions (tiles 1–36)\n"
            "   – the last dice roll\n"
            "   – a winner screen when someone reaches tile 36.\n\n"
            "Themes only change visuals."
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
    # Your Leuven content (kept)
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
        9:  TileMeta("Sint-pieterskerk", "Move places forward",
                     "Je bezoekt de sint-pieterskerk. De priester wilt naar de kruidtuin en jij moet hem de weg wijzen. "
                     "Ga verder naar de kruidtuin met de priester"),
        10: TileMeta("Brusselsestraat", "—", ""),
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
        19: TileMeta("Celestijnenlaan", "—", ""),
        20: TileMeta("Kasteel van Arenberg", "—",
                     "You fell in the Dijle and and went with the stream and ended up in stadium of OHLeuven"),
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
    tile_size: int = 64
    gap: int = 10
    margin: int = 30
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
# Board view widget (theme-aware)
# =============================
class BoardView(QtWidgets.QGraphicsView):
    def __init__(self, parent=None, theme: Theme | None = None):
        super().__init__(parent)
        self.theme = theme or theme_by_key("leuven")
        self.assets_dir = self.theme.assets_dir

        self.layout_cfg = BoardLayout(tile_size=72, gap=12, margin=30)

        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._goose_overlay = None

        self._apply_theme_background()

        # metas
        if self.theme.key == "leuven":
            self.metas = build_tile_metas_leuven()
        else:
            # Goose (or others): generic names
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

        # Goose overlay (ONLY for goose theme)
        if goose_theme is not None and self.theme.key == goose_theme.THEME_KEY:
            self._install_goose_overlay()

        self._fit_scene()

    def _theme_path(self, *parts) -> str:
        return os.path.join(self.assets_dir, *parts)

    def _apply_theme_background(self):
        bg_path = self._theme_path("background.png")
        if os.path.exists(bg_path):
            pm = QtGui.QPixmap(bg_path)
            self.setBackgroundBrush(QtGui.QBrush(pm))
        else:
            # fallback
            if self.theme.key == "goose":
                self.setBackgroundBrush(QtGui.QColor(244, 236, 220))
            else:
                self.setBackgroundBrush(QtGui.QColor(245, 248, 252))

    def _install_goose_overlay(self):
        gif_path = self._theme_path("goose.gif")
        if not os.path.exists(gif_path):
            return

        # IMPORTANT: stop any previous overlay
        if self._goose_overlay is not None:
            try:
                self._goose_overlay.stop()
            except Exception:
                pass
            self._goose_overlay = None

        # Use tiles as boundary -> geese roam inside the inner hole
        tile_items_list = list(self.tile_items.values())

        self._goose_overlay = goose_theme.GooseOverlay(
            self,  # <-- this QGraphicsView
            lambda: goose_theme.inner_rect_from_tile_items(tile_items_list, pad=10)
        )

        self._goose_overlay.start(
            gif_path=gif_path,
            size_a=int(self.layout_cfg.tile_size * 0.95),
            size_b=int(self.layout_cfg.tile_size * 0.75),
        )

    # ---------- center ----------
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
        step = ts + g

        board_w = m * 2 + self.layout_cfg.top_count * ts + (self.layout_cfg.top_count - 1) * g
        board_h = m * 2 + (self.layout_cfg.right_count + 2) * ts + (self.layout_cfg.right_count + 1) * g

        # keep Leuven center as before (2x2 tiles)
        w = ts * 2 + g * 1
        h = ts * 2 + g * 1
        x = (board_w - w) / 2
        y = (board_h - h) / 2
        return QtCore.QRectF(x, y, w, h)

    def _build_center_panel(self):
        r = self._center_panel_rect()
        self._center_target_rect = r

        self.center_bg = QtWidgets.QGraphicsRectItem(r)
        self.center_bg.setBrush(QtGui.QColor(255, 255, 255, 220))
        self.center_bg.setPen(QtGui.QPen(QtGui.QColor(170, 185, 205), 2))
        self.center_bg.setZValue(10)
        self.scene.addItem(self.center_bg)

        self.center_pix = QtWidgets.QGraphicsPixmapItem()
        self.center_pix.setZValue(15)
        self.scene.addItem(self.center_pix)

        # theme center image (if exists)
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

    # ---------- assets ----------
    def _load_tile_pix(self, tile_id: int) -> QtGui.QPixmap:
        # theme supports either numbered tiles OR a single tile.png
        p_num = self._theme_path("tiles", f"{tile_id:02d}.png")
        if os.path.exists(p_num):
            return QtGui.QPixmap(p_num)

        p_single = self._theme_path("tiles", "tile.png")
        if os.path.exists(p_single):
            return QtGui.QPixmap(p_single)

        return QtGui.QPixmap()

    def _load_pawn_pix(self, player_id: int) -> QtGui.QPixmap:
        # try theme pawns first
        p = self._theme_path("pawns", f"p{player_id}.png")
        if os.path.exists(p):
            return QtGui.QPixmap(p)

        # fallback to assets/pawns (shared)
        shared = os.path.join(ASSETS_DIR, "pawns", f"p{player_id}.png")
        if os.path.exists(shared):
            return QtGui.QPixmap(shared)

        return QtGui.QPixmap()

    # ---------- build scene ----------
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
        pawn_size = int(self.layout_cfg.tile_size * 0.55)
        offsets = {
            1: QtCore.QPointF(6, 6),
            2: QtCore.QPointF(self.layout_cfg.tile_size - pawn_size - 6, 6),
            3: QtCore.QPointF(6, self.layout_cfg.tile_size - pawn_size - 6),
            4: QtCore.QPointF(self.layout_cfg.tile_size - pawn_size - 6,
                              self.layout_cfg.tile_size - pawn_size - 6),
        }

        for pid in range(1, NUM_PLAYERS + 1):
            pix = self._load_pawn_pix(pid)
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


# =============================
# Game dashboard screen
# =============================
class GameScreen(QtWidgets.QWidget):
    back_to_start = QtCore.Signal()

    def __init__(self, theme: Theme):
        super().__init__()
        self.setObjectName("GameScreen")
        self.theme = theme

        self.positions = {1: 1, 2: 1}
        self.previous_positions = {1: 1, 2: 1}
        self.active_player = 1
        self.last_dice = None
        self.last_player = None

        self._last_action_base = "Waiting for data…"
        self._next_prompt = ""

        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("📡 Live game dashboard")
        title.setObjectName("H1")
        header.addWidget(title)
        header.addStretch(1)
        back_btn = QtWidgets.QPushButton("← Back to start")
        back_btn.setObjectName("GhostButton")
        back_btn.clicked.connect(self.back_to_start.emit)
        header.addWidget(back_btn)

        info_card = QtWidgets.QGroupBox("Current turn")
        info_card.setObjectName("Card")
        info_layout = QtWidgets.QFormLayout(info_card)
        self.lbl_turn = QtWidgets.QLabel("AI 1")
        self.lbl_dice = QtWidgets.QLabel("—")
        self.lbl_action = QtWidgets.QLabel("Waiting for data…")
        self.lbl_action.setWordWrap(True)
        self.lbl_special = QtWidgets.QLabel("—")

        info_layout.addRow("Active player:", self.lbl_turn)
        info_layout.addRow("Last roll:", self.lbl_dice)
        info_layout.addRow("Last action:", self.lbl_action)
        info_layout.addRow("Special tile:", self.lbl_special)

        players_card = QtWidgets.QGroupBox("Players")
        players_card.setObjectName("Card")
        players_layout = QtWidgets.QGridLayout(players_card)
        players_layout.setSpacing(12)

        self.player_widgets = {}
        for i in range(1, NUM_PLAYERS + 1):
            frame = QtWidgets.QGroupBox(f"AI {i}")
            frame.setObjectName("PlayerCard")
            v = QtWidgets.QVBoxLayout(frame)
            v.setSpacing(6)

            lbl_pos = QtWidgets.QLabel("Tile: 1")
            lbl_pos.setObjectName("BodyLabel")

            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 36)
            bar.setValue(1)
            bar.setFormat("%v / 36")

            lbl_status = QtWidgets.QLabel("Status: playing")
            lbl_status.setObjectName("Info")

            v.addWidget(lbl_pos)
            v.addWidget(bar)
            v.addWidget(lbl_status)

            row = 0
            col = i - 1
            players_layout.addWidget(frame, row, col)

            self.player_widgets[i] = {"frame": frame, "pos": lbl_pos, "bar": bar, "status": lbl_status}

        meta_card = QtWidgets.QGroupBox("Connection")
        meta_card.setObjectName("Card")
        meta_form = QtWidgets.QFormLayout(meta_card)
        self.lbl_serial_status = QtWidgets.QLabel("Setting up connection…")
        self.lbl_serial_status.setWordWrap(True)
        meta_form.addRow("Serial status:", self.lbl_serial_status)

        log_card = QtWidgets.QGroupBox("Game log")
        log_card.setObjectName("Card")
        log_layout = QtWidgets.QVBoxLayout(log_card)
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)

        top_row = QtWidgets.QHBoxLayout()
        top_row.addWidget(info_card, stretch=1)

        self.board = BoardView(self, theme=self.theme)
        self.board.setMinimumSize(520, 420)

        side = QtWidgets.QVBoxLayout()
        side.setSpacing(12)
        side.addWidget(players_card)
        side.addWidget(meta_card)
        side.addStretch(1)

        side_wrap = QtWidgets.QWidget()
        side_wrap.setLayout(side)

        middle_row = QtWidgets.QHBoxLayout()
        middle_row.setSpacing(14)
        middle_row.addWidget(self.board, stretch=3)
        middle_row.addWidget(side_wrap, stretch=2)

        root.addLayout(header)
        root.addLayout(top_row)
        root.addLayout(middle_row)
        root.addWidget(log_card, stretch=1)

    def set_serial_status(self, text: str):
        self.lbl_serial_status.setText(text)

    def append_log(self, text: str):
        self.log.append(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

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
        p1 = data.get("p1")
        p2 = data.get("p2")
        active = data.get("active")
        dice = data.get("dice")
        last_player = data.get("last_player")

        if p1 is not None:
            self.previous_positions[1] = self.positions.get(1)
            self.positions[1] = p1
        if p2 is not None:
            self.previous_positions[2] = self.positions.get(2)
            self.positions[2] = p2
        if active is not None:
            self.active_player = active

        if dice is not None:
            self.last_dice = dice
        if last_player is not None:
            self.last_player = last_player

        self._refresh_players()
        self._refresh_header()

        self.board.sync_from_state(self.positions, previous=self.previous_positions, last_player=self.last_player)

    def _refresh_players(self):
        for i in range(1, NUM_PLAYERS + 1):
            w = self.player_widgets[i]
            pos = max(1, min(36, int(self.positions.get(i, 1) or 1)))
            w["pos"].setText(f"Tile: {pos}")
            w["bar"].setValue(pos)

            if i == self.active_player:
                w["frame"].setStyleSheet("""
                    QGroupBox { border: 3px solid #0ea5e9; border-radius: 14px; margin-top: 18px; }
                    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0f172a; }
                """)
            else:
                w["frame"].setStyleSheet("""
                    QGroupBox { border: 1px solid #e2e8f0; border-radius: 14px; margin-top: 18px; }
                    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0f172a; }
                """)

    def _refresh_header(self):
        self.lbl_turn.setText(f"AI {self.active_player}")
        self.lbl_special.setText("—")

        if self.last_dice is not None and self.last_player is not None:
            old_pos = self.previous_positions.get(self.last_player)
            new_pos = self.positions.get(self.last_player)
            self.lbl_dice.setText(str(self.last_dice))

            if old_pos is not None and new_pos is not None:
                self._set_last_action_base(
                    f"AI {self.last_player} rolled {self.last_dice} and moved from tile {old_pos} to {new_pos}."
                )
            else:
                self._set_last_action_base(f"AI {self.last_player} rolled {self.last_dice}.")

            meta = self.board.metas.get(int(new_pos)) if self.board else None
            if meta and meta.rule and meta.rule != "—":
                self.lbl_special.setText(f"{meta.name} (tile {new_pos})")
                self.append_log(f"✨ Special tile #{new_pos}: {meta.name} — {meta.rule}")
                if meta.short_text:
                    self.append_log(f"   {meta.short_text}")
                self.board.flash_tile(int(new_pos))
            else:
                self.lbl_special.setText("—")
        else:
            self.lbl_dice.setText("—")
            self._set_last_action_base("Waiting for data…")
            self.lbl_special.setText("—")

    def show_winner(self, winner: int):
        w = self.player_widgets.get(winner)
        if w:
            w["status"].setText("Status: WINNER 🏆")
        dlg = WinDialog(winner, self)
        dlg.exec()


# =============================
# Main window (shell)
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

        self._serial_status_line = "Not connected."
        self._cam_status_line = "Camera: —"
        self._cam_ready = False
        self._cam_value: int | None = None
        self._cam_armed = True

        self._dice_lock = False
        self._expected_last_player: int | None = None

        self.current_theme: Theme = theme_by_key("leuven")

        self.stack = QtWidgets.QStackedWidget()
        self.setCentralWidget(self.stack)

        self.start_screen = StartScreen(self.serial_cfg)
        self.game_screen = GameScreen(self.current_theme)

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
        self.game_screen = GameScreen(theme)
        self.game_screen.back_to_start.connect(self.stop_and_back)

        self.stack.removeWidget(old)
        old.deleteLater()

        self.stack.addWidget(self.game_screen)
        self.stack.setCurrentIndex(0)
        self._update_connection_card()

    def start_game(self, theme_key: str):
        new_theme = theme_by_key(theme_key)
        if new_theme.key != self.current_theme.key:
            self.current_theme = new_theme
            self._rebuild_game_screen(self.current_theme)

        if self.worker is None or not self.worker.is_alive():
            self.worker = SerialWorker(self.serial_cfg, self.msg_queue)
            self.worker.start()
            self._serial_status_line = "Setting up connection…"
            self.start_screen.set_status(self._serial_status_line)

        if self.cam_worker is None or not self.cam_worker.is_alive():
            self.cam_worker = DiceCameraWorker(self.cam_cfg, self.msg_queue)
            self.cam_worker.start()

        self._update_connection_card()

        self._dice_lock = False
        self._expected_last_player = None
        self.game_screen.set_next_prompt(
            f"Next: AI {self.game_screen.active_player} → roll dice in view (still) and press SPACE."
        )

        self.stack.setCurrentIndex(1)

    def stop_and_back(self):
        self.stack.setCurrentIndex(0)

    def _on_space(self):
        if self.stack.currentIndex() != 1:
            return

        if self.worker is None or not self.worker.is_alive():
            self.game_screen.append_log("⚠️ SPACE: not connected to ESP32.")
            self.game_screen.set_next_prompt("Connect first, then press SPACE.")
            return

        if self.cam_worker is None or not self.cam_worker.is_alive():
            self.game_screen.append_log("⚠️ SPACE: camera worker not running.")
            self.game_screen.set_next_prompt("Camera not running.")
            return

        if self._dice_lock:
            self.game_screen.append_log("⏳ SPACE: waiting for ESP32 to confirm previous move…")
            self.game_screen.set_next_prompt("Waiting for board update…")
            return

        value = self.cam_worker.get_ready_value()
        active = self.game_screen.active_player

        if value is None:
            self.game_screen.append_log("❌ SPACE: no stable dice value detected.")
            self.game_screen.set_next_prompt(
                f"AI {active}: roll again, place dice still under camera, then press SPACE."
            )
            return

        self.worker.send_line(f"DICE:{value}")
        self._dice_lock = True
        self._expected_last_player = int(active)

        self.cam_worker.consume()

        self.game_screen.append_log(f"🎲 AI {active} confirmed roll = {value} (sent to ESP32)")
        self.game_screen.set_next_prompt("Sent. Waiting for board update…")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()

                if kind == "log":
                    self._serial_status_line = str(payload)
                    self.start_screen.set_status(self._serial_status_line)
                    self._update_connection_card()
                    self.game_screen.append_log(payload)

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
                        ap = self.game_screen.active_player
                        if not armed:
                            self.game_screen.set_next_prompt("Remove dice / roll again for next player.")
                        elif self._cam_ready:
                            self.game_screen.set_next_prompt(f"AI {ap}: dice detected → press SPACE to confirm.")
                        else:
                            self.game_screen.set_next_prompt(f"AI {ap}: roll dice in view (still) and press SPACE.")

                elif kind == "pos":
                    self.game_screen.update_from_pos(payload)

                    lp = payload.get("last_player")
                    if self._dice_lock and lp is not None and self._expected_last_player is not None:
                        if int(lp) == int(self._expected_last_player):
                            self._dice_lock = False
                            self._expected_last_player = None

                    if self.stack.currentIndex() == 1:
                        ap = self.game_screen.active_player
                        if self._dice_lock:
                            self.game_screen.set_next_prompt("Waiting for board update…")
                        else:
                            if self._cam_armed and self._cam_ready:
                                self.game_screen.set_next_prompt(f"AI {ap}: dice detected → press SPACE to confirm.")
                            else:
                                self.game_screen.set_next_prompt(f"AI {ap}: roll dice in view (still) and press SPACE.")

                elif kind == "win":
                    self.game_screen.append_log(f"WINNER: AI {payload}")
                    self.game_screen.show_winner(payload)

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
        * { color: #0f172a; }

        QMenuBar, QMenu { color: #0f172a; }

        QLabel#Title {
            font-family: "__TITLE_FONT__";
            font-size: 40px;

            letter-spacing: 0.5px;
            color: #ffffff;
        }
        QLabel#Subtitle {
            font-size: 16px;
            color: #ffffff;
            font-family: "__TITLE_FONT__";
        }
        QLabel#Info {
            font-size: 12px;
            color: #ffffff;
            font-family: "__TITLE_FONT__";
        }
        QLabel#H1 {
            font-size: 24px;
            font-weight: 600;
            color: #ffffff;
            font-family: "__TITLE_FONT__";
        }
        QLabel#FormLabel {
            color: #ffffff;
            font-size: 14px;
            letter-spacing: 2px;
            font-family: "__TITLE_FONT__";
        }

        QPushButton#PrimaryButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #0F0082, stop:1 #05002B);
            color: #FFFFFF;
            border: 2px solid #FFFFFF;
            padding: 12px 28px;
            border-radius: 4px;
            font-size: 16px;
            font-weight: 700;
            letter-spacing: 3px;
            font-family: "__TITLE_FONT__";
        }
        QPushButton#PrimaryButton:hover { background: #0284c7; }
        QPushButton#PrimaryButton:pressed { background: #0369a1; }

        QPushButton#GhostButton {
            font-family: "__TITLE_FONT__";
            background: transparent;
            border: 1px solid #e2e8f0;
            color: #ffffff;
            padding: 8px 14px;
            border-radius: 4px;
        }
        QPushButton#GhostButton:hover { background: #f1f5f9; }

        QGroupBox#Card {
            border: 1px solid #e2e8f0;
            border-radius: 14px;
            margin-top: 18px;
            
        }
        QGroupBox#Card::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #0f172a;
        }

        QProgressBar {
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            height: 20px;
            text-align: center;
            background: #ffffff;
        }
        QProgressBar::chunk {
            background: #0ea5e9;
            border-radius: 8px;
        }

        QTextEdit {
            background: #ffffff;
            color: #0f172a;
            border: 1px solid #e2e8f0;
            border-radius: 8px;

        }

        QComboBox {
            border: 2px solid #e2e8f0;
            background: #05002B;
            border-radius: 4px;
            padding: 6px 10px;
            font-size: 16px;
            color: #ffffff;
            min-height: 44px;
            font-family: "__TITLE_FONT__";
            
        }
        QComboBox:hover { background: #0284c7; }
        QComboBox::drop-down { border: none; width: 28px; }
        QComboBox::down-arrow {
            image: url("data:image/svg+xml;utf8,\
<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'>\
<path d='M0 0 L10 0 L5 6 Z' fill='%230f172a'/></svg>");
        }
        QComboBox QAbstractItemView {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            selection-background-color: #e2e8f0;
            color: #0f172a;
        }
        """

# =============================
# Entrypoint
# =============================
def main():
    import sys
    serial_cfg = SerialConfig(port="COM9", baudrate=115200)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GAME4 Companion – Serial")

    font_path = os.path.join(ASSETS_DIR, "PressStart2P-Regular.ttf")
    if not os.path.exists(font_path):
        print("Font file not found:", font_path)

    font_id = QtGui.QFontDatabase.addApplicationFont(font_path)

    title_family = "Arial"  #fallback
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
