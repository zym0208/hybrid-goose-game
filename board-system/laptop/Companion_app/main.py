# game4_app.py
# Requirements: pip install PySide6
from PySide6 import QtCore, QtGui, QtWidgets
from dataclasses import dataclass
import random

# =============================
# Config & State
# =============================
@dataclass
class AppConfig:
    players_count: int = 2
    players: str = "1h-1ai"  # kept for compatibility with SettingsScreen
    ai_difficulty: str = "Normal"
    sound_enabled: bool = True
    brightness: int = 70  # 0–100

@dataclass
class GameState:
    tile_count: int = 36
    positions: list = None
    current: int = 0
    finished: bool = False

    def __post_init__(self):
        if self.positions is None:
            self.positions = [0, 0]

# =============================
# Win dialog (first finisher)
# =============================
class WinDialog(QtWidgets.QDialog):
    def __init__(self, winner_index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Winner!")
        self.setModal(True)
        self.resize(420, 220)
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(f"🏆 Player {winner_index+1} wins!")
        title.setAlignment(QtCore.Qt.AlignCenter)
        font = title.font(); font.setPointSize(18); font.setBold(True); title.setFont(font)
        subtitle = QtWidgets.QLabel("Do you want to continue until everyone finishes, or return to the home screen?")
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setWordWrap(True)
        btns = QtWidgets.QDialogButtonBox()
        self.cont = btns.addButton("Continue", QtWidgets.QDialogButtonBox.AcceptRole)
        self.back = btns.addButton("Back to Home", QtWidgets.QDialogButtonBox.DestructiveRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(btns)

# =============================
# Dice roll animation (small dialog)
# =============================
class DiceRollDialog(QtWidgets.QDialog):
    def __init__(self, roll: int, parent=None):
        super().__init__(parent)
        self.roll = max(1, min(6, int(roll)))
        self.setWindowTitle("Dice")
        self.setModal(True)
        self.setFixedSize(240, 260)
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a}"
        )
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)

        self.lbl = QtWidgets.QLabel()
        self.lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl.setMinimumSize(160, 160)

        self.info = QtWidgets.QLabel("Rolling…")
        self.info.setAlignment(QtCore.Qt.AlignCenter)

        v.addStretch(1)
        v.addWidget(self.lbl)
        v.addWidget(self.info)
        v.addStretch(1)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._elapsed = 0

        # draw first frame as fallback so it's never blank
        self._set_face(random.randint(1,6))

    def start(self):
        # center on parent if possible
        if self.parent() and isinstance(self.parent(), QtWidgets.QWidget):
            parent_rect = self.parent().frameGeometry()
            self.move(parent_rect.center() - self.rect().center())
        self.timer.start(80)
        QtCore.QTimer.singleShot(900, self._finish)
        return self.exec()

    def _tick(self):
        self._elapsed += 80
        r = random.randint(1, 6)
        self._set_face(r)

    def _finish(self):
        self.timer.stop()
        self._set_face(self.roll)
        self.info.setText(f"You rolled {self.roll}!")
        QtCore.QTimer.singleShot(350, self.accept)

    def _set_face(self, n: int):
        # Render a dice face to pixmap (vector, no font needed)
        size = 160
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        # body
        rect = QtCore.QRectF(4, 4, size-8, size-8)
        p.setPen(QtGui.QPen(QtGui.QColor("#0f172a"), 3))
        p.setBrush(QtGui.QBrush(QtGui.QColor("#ffffff")))
        p.drawRoundedRect(rect, 16, 16)

        # pip helper
        def pip(cx, cy):
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QBrush(QtGui.QColor("#0f172a")))
            r = 11
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)

        # positions
        cx = [size*0.25, size*0.5, size*0.75]
        cy = [size*0.25, size*0.5, size*0.75]
        spots = {
            1: [(1,1)],
            2: [(0,0),(2,2)],
            3: [(0,0),(1,1),(2,2)],
            4: [(0,0),(0,2),(2,0),(2,2)],
            5: [(0,0),(0,2),(1,1),(2,0),(2,2)],
            6: [(0,0),(0,1),(0,2),(2,0),(2,1),(2,2)],
        }
        for ix, iy in spots.get(int(n), [(1,1)]):
            pip(cx[ix], cy[iy])

        p.end()
        self.lbl.setPixmap(pm)

# =============================
# Special tile dialog
# =============================
class SpecialTileDialog(QtWidgets.QDialog):
    def __init__(self, tile: int, title: str, delta, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tile {tile}")
        self.setModal(True)
        self.setFixedSize(360, 220)
        # Force a clear, light look to avoid any dark overlays
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a}"
            "QPushButton#PrimaryButton{background:#0ea5e9;color:white;border:none;padding:8px 14px;border-radius:10px}"
            "QPushButton#PrimaryButton:hover{background:#0284c7}"
        )
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)

        h = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel("✨")
        f = icon.font(); f.setPointSize(24); icon.setFont(f)
        h.addWidget(icon)
        lbl_title = QtWidgets.QLabel(f"<b>{title}</b>")
        h.addWidget(lbl_title)
        h.addStretch(1)

        lbl_msg = QtWidgets.QLabel(message)
        lbl_msg.setWordWrap(True)

        # Delta can be an int (movement) or a descriptive string (skip/wait/roll again)
        if isinstance(delta, int):
            eff_text = f"{('+' if delta>0 else '')}{delta}"
            color_style = "color: #16a34a" if delta>0 else "color:#dc2626"
        else:
            eff_text = str(delta)
            color_style = "color: #0f172a"
        lbl_delta = QtWidgets.QLabel(f"Effect: {eff_text}")
        lbl_delta.setStyleSheet(color_style)

        btn = QtWidgets.QPushButton("OK")
        btn.setObjectName("PrimaryButton")
        btn.clicked.connect(self.accept)

        v.addLayout(h)
        v.addWidget(lbl_msg)
        v.addWidget(lbl_delta)
        v.addStretch(1)
        v.addWidget(btn, alignment=QtCore.Qt.AlignRight)
        # Auto-close after 3 seconds (user requested longer visibility)
        QtCore.QTimer.singleShot(3000, self.accept)

# =============================
# Special rules overview
# =============================
class SpecialRulesDialog(QtWidgets.QDialog):
    def __init__(self, rules: dict[int, tuple[str,int,str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Special Tiles")
        self.setModal(True)
        self.resize(520, 320)
        # Strong, explicit styles to avoid any dark panes
        self.setStyleSheet(
            "QDialog{background:white;border:1px solid #e2e8f0;border-radius:12px}"
            "QLabel{color:#0f172a}"
            "QTableWidget{background:white;color:#0f172a;border:1px solid #e2e8f0;border-radius:8px;gridline-color:#e2e8f0;alternate-background-color:#f8fafc}"
            "QHeaderView::section{background:#f1f5f9;color:#0f172a;border:1px solid #e2e8f0;padding:4px}"
        )

        v = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("📜 Special tiles overview")
        f = title.font(); f.setPointSize(14); f.setBold(True); title.setFont(f)
        v.addWidget(title)

        table = QtWidgets.QTableWidget(len(rules), 4)
        table.setHorizontalHeaderLabels(["Tile", "Title", "Effect", "Description"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)

        for row, (tile, (name, delta, msg)) in enumerate(sorted(rules.items())):
            table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(tile)))
            table.setItem(row, 1, QtWidgets.QTableWidgetItem(name))
            # delta may be int or string; display appropriately
            if isinstance(delta, int):
                eff = f"{('+' if delta>0 else '')}{delta}"
            else:
                eff = str(delta)
            table.setItem(row, 2, QtWidgets.QTableWidgetItem(eff))
            table.setItem(row, 3, QtWidgets.QTableWidgetItem(msg))

        v.addWidget(table)
        btn = QtWidgets.QPushButton("Close")
        btn.setObjectName("PrimaryButton")
        btn.clicked.connect(self.accept)
        v.addWidget(btn, alignment=QtCore.Qt.AlignRight)

class DigitalBoard(QtWidgets.QGraphicsView):
    COLORS = [QtGui.QColor("#0ea5e9"), QtGui.QColor("#22c55e"), QtGui.QColor("#f59e0b"), QtGui.QColor("#ef4444")]

    def __init__(self, tile_count=40, parent=None):
        super().__init__(parent)
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.tile_count = tile_count
        # dynamic layout values (will be computed on first draw/resize)
        self._cell = 48
        self._margin = 20
        self.path = self._generate_path(tile_count)
        self.tokens: list[QtWidgets.QGraphicsEllipseItem] = []
        self._effects: list[QtWidgets.QGraphicsItem] = []
        # store board items (tiles, labels) separately so we can redraw without
        # deleting tokens (which are persistent QGraphicsItems)
        self._board_items = []
        self._draw_board()
        self.setMinimumHeight(260)
        self.setStyleSheet("QGraphicsView{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px}")

    def resizeEvent(self, event):
        # Recompute layout on resize to stay responsive and square
        self.path = self._generate_path(self.tile_count)
        self._draw_board()
        # Reposition tokens if any
        for idx, t in enumerate(self.tokens):
            try:
                self.update_token(idx, self._token_positions[idx])
            except Exception:
                # fallback: keep token at same index or 0
                if hasattr(self, '_token_positions') and idx < len(self._token_positions):
                    self.update_token(idx, self._token_positions[idx])
                else:
                    self.update_token(idx, 0)
        super().resizeEvent(event)

    def _generate_path(self, tiles: int):
        # Generate a square grid and place tiles along the outer border clockwise.
        # For tiles >= 36 we use a 10x10 grid (perimeter=36) so indices 0..35 map to
        # the outer border starting top-left clockwise; index 36 is placed in the center.
        # The returned list has length tiles+1 (indices 0..tiles).
        from math import ceil

        # Decide grid size (m x m)
        if tiles >= 36:
            m = 10
        else:
            # find minimal m >=3 with perimeter >= tiles
            m = 3
            while 4 * m - 4 < tiles:
                m += 1

        # compute available drawing area (make board square)
        vp = self.viewport()
        w = vp.width() if vp is not None else (m * self._cell + 2 * self._margin)
        h = vp.height() if vp is not None else (m * self._cell + 2 * self._margin)
        size = max(160, min(w, h) - 2 * self._margin)
        # cell distance between grid lines
        cell = size / (m - 1) if m > 1 else self._cell
        margin = (min(w, h) - cell * (m - 1)) / 2

        # precompute grid points
        grid = [[QtCore.QPointF(margin + c * cell, margin + r * cell) for c in range(m)] for r in range(m)]

        # perimeter positions clockwise starting at top-left
        perim = []
        # top row (left->right)
        for c in range(0, m):
            perim.append(grid[0][c])
        # right column (top+1 -> bottom)
        for r in range(1, m):
            perim.append(grid[r][m-1])
        # bottom row (right-1 -> left)
        for c in range(m-2, -1, -1):
            perim.append(grid[m-1][c])
        # left column (bottom-1 -> top+1)
        for r in range(m-2, 0, -1):
            perim.append(grid[r][0])

        points = []
        total = tiles + 1
        # Initialize with dummy points to desired length
        for _ in range(total):
            points.append(QtCore.QPointF(0, 0))

        # Map indices 0..len(perim)-1 to perimeter
        for idx, p in enumerate(perim):
            if idx < total:
                points[idx] = p

        # If tiles >= 36 and center exists, place index 36 at center cell
        if tiles >= 36:
            center = grid[m // 2][m // 2]
            if 36 < total:
                points[36] = center

        # Fill remaining indices (if any) with interior cells row-major excluding center
        next_idx = 0
        for r in range(m):
            for c in range(m):
                # skip perimeter cells
                if (r == 0 or r == m-1 or c == 0 or c == m-1):
                    continue
                # skip center if already used for 36
                if tiles >= 36 and (r == m // 2 and c == m // 2):
                    continue
                # find next unused index
                while next_idx < total and not (points[next_idx].x() == 0 and points[next_idx].y() == 0):
                    next_idx += 1
                if next_idx >= total:
                    break
                points[next_idx] = grid[r][c]
                next_idx += 1
            if next_idx >= total:
                break

        # store last computed cell/margin for drawing
        self._cell = cell
        self._margin = margin
        return points

    def _draw_board(self):
        s = self.scene()
        # Remove previous board items (tiles/labels) but keep tokens alive
        try:
            for it in getattr(self, '_board_items', []):
                try:
                    s.removeItem(it)
                except Exception:
                    pass
        finally:
            self._board_items = []
        # Also clear any active visual effects (they will be recreated as needed)
        for e in list(self._effects):
            try:
                s.removeItem(e)
            except Exception:
                pass
        self._effects = []
        pen = QtGui.QPen(QtGui.QColor("#94a3b8"))
        tile_size = max(12, self._cell * 0.7)
        half = tile_size / 2
        for i, p in enumerate(self.path):
            # draw only sensible points (non-zero)
            try:
                x = p.x(); y = p.y()
            except Exception:
                continue
            rect = QtCore.QRectF(x - half, y - half, tile_size, tile_size)
            path = QtGui.QPainterPath()
            # corner tiles (indices on perimeter corners) get stronger style
            corner = False
            # identify corners: top-left (index 0), top-right, bottom-right, bottom-left
            if i == 0:
                corner = True
            # center tile (36) highlighted
            if i == 36:
                brush = QtGui.QBrush(QtGui.QColor("#fde68a"))
                pen2 = QtGui.QPen(QtGui.QColor("#f59e0b"), 2)
                path.addRect(rect)
                item = s.addPath(path, pen2, brush)
                self._board_items.append(item)
                lbl = s.addText("36\nFinish")
                lbl.setDefaultTextColor(QtGui.QColor("#0f172a"))
                lbl.setPos(x - half, y - half)
                self._board_items.append(lbl)
                continue
            if corner:
                path.addRoundedRect(rect, 6, 6)
                item = s.addPath(path, QtGui.QPen(QtGui.QColor("#0ea5e9"), 2), QtGui.QBrush(QtGui.QColor("#e0f2fe")))
                self._board_items.append(item)
            else:
                path.addRoundedRect(rect, 6, 6)
                item = s.addPath(path, pen, QtGui.QBrush(QtGui.QColor("#e2e8f0")))
                self._board_items.append(item)
            # small tile number
            label = s.addText(str(i))
            font = label.font(); font.setPointSize(8); label.setFont(font)
            label.setDefaultTextColor(QtGui.QColor("#0f172a"))
            label.setPos(x - half + 2, y - half + 2)
            self._board_items.append(label)
        # connecting line
        # No connecting lines between tiles — visual only grid

    def reset_tokens(self, n_players: int):
        s = self.scene()
        for t in self.tokens:
            s.removeItem(t)
        self.tokens = []
        for e in self._effects:
            s.removeItem(e)
        self._effects = []
        # initialize token storage for responsive repositioning
        self._token_positions = [0] * n_players
        for i in range(n_players):
            color = DigitalBoard.COLORS[i % len(DigitalBoard.COLORS)]
            t = s.addEllipse(-8, -8, 16, 16, QtGui.QPen(QtCore.Qt.NoPen), QtGui.QBrush(color))
            # Ensure tokens are rendered above board items/effects
            try:
                t.setZValue(1000 + i)
            except Exception:
                pass
            self.tokens.append(t)
            self.update_token(i, 0)

    def update_token(self, player: int, tile: int):
        # remember logical token tile for resize repositioning
        if not hasattr(self, '_token_positions'):
            self._token_positions = [0] * len(self.tokens)
        if player >= 0:
            if player >= len(self._token_positions):
                # extend storage if needed
                self._token_positions += [0] * (player - len(self._token_positions) + 1)
            self._token_positions[player] = tile
        tile = max(0, min(tile, len(self.path)-1))
        pos = self.path[tile]
        # small per-player offset so tokens don't overlap
        dx = (player % 2) * 10 - 5
        dy = (player // 2) * 10 - 5
        self.tokens[player].setPos(pos.x()+dx, pos.y()+dy)

    def effect_pulse(self, tile: int, text: str, color: QtGui.QColor = QtGui.QColor("#0ea5e9")):
        """Draw a short pulse highlight on a tile + fading text."""
        s = self.scene()
        tile = max(0, min(tile, len(self.path)-1))
        p = self.path[tile]
        ring = s.addEllipse(p.x()-22, p.y()-22, 44, 44, QtGui.QPen(color, 2), QtGui.QBrush(QtCore.Qt.NoBrush))
        ring.setZValue(10)
        label = s.addText(text)
        label.setDefaultTextColor(color)
        label.setZValue(11)
        label.setScale(1.0)
        label.setPos(p.x()-30, p.y()-42)
        self._effects += [ring, label]

        # animation: opacity and scale
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(900)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        def _step(v):
            ring.setRect(p.x()-22 - v*6, p.y()-22 - v*6, 44 + v*12, 44 + v*12)
            op = 1.0 - abs(v-0.5)*2  # 0→1→0
            label.setOpacity(max(0.0, op))
        def _done():
            s.removeItem(ring)
            s.removeItem(label)
            try:
                self._effects.remove(ring); self._effects.remove(label)
            except ValueError:
                pass
        anim.valueChanged.connect(_step)
        anim.finished.connect(_done)
        anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)

# =============================
# Start Screen (players select + Demo)
# =============================
class StartScreen(QtWidgets.QWidget):
    go_main = QtCore.Signal()
    start_demo = QtCore.Signal(int)   # number of players
    start_human = QtCore.Signal(int)  # number of players (1 human + rest AI)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setObjectName("StartScreen")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        title = QtWidgets.QLabel("GAME4 Companion")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setObjectName("Title")

        subtitle = QtWidgets.QLabel("Hybrid board game • Camera dice • LED feedback")
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setObjectName("Subtitle")

        # Card: choose players
        card = QtWidgets.QGroupBox("Start settings")
        card.setObjectName("Card")
        form = QtWidgets.QFormLayout(card)

        # ComboBox for number of players (2..4)
        self.players_combo = QtWidgets.QComboBox()
        self.players_combo.addItems(["2 players", "3 players", "4 players"])
        self.players_combo.setCurrentText({2: "2 players", 3: "3 players", 4: "4 players"}.get(self.config.players_count, "2 players"))
        self.players_combo.setFixedHeight(44)
        self.players_combo.setMinimumWidth(160)
        self.players_combo.setToolTip("Select the number of players.")
        form.addRow("Players:", self.players_combo)

        # Action buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_main = QtWidgets.QPushButton("Start ▶ Play yourself")
        btn_main.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        btn_main.setObjectName("PrimaryButton")
        btn_main.setFixedHeight(48)
        btn_main.clicked.connect(self._start_human)

        btn_demo = QtWidgets.QPushButton("🎬 Demo: play automatically")
        btn_demo.setObjectName("GhostButton")
        btn_demo.setFixedHeight(48)
        btn_demo.clicked.connect(self._start_demo)

        btn_row.addWidget(btn_demo)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_main)

        # Info text under buttons
        info = QtWidgets.QLabel("Choose the number of players. Start a game (1 human + AI) or watch the automatic demo.")
        info.setAlignment(QtCore.Qt.AlignCenter)
        info.setObjectName("Info")

        # Layout
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(card)
        layout.addLayout(btn_row)
        layout.addWidget(info)
        layout.addStretch(2)

    def _sync_count(self):
        text = self.players_combo.currentText()
        self.config.players_count = int(text.split()[0])
        mapping = {2: "1h-1ai", 3: "1h-2ai", 4: "3ai"}
        self.config.players = mapping.get(self.config.players_count, "1h-1ai")

    def _to_main(self):
        self._sync_count()
        self.go_main.emit()

    def _start_demo(self):
        self._sync_count()
        self.start_demo.emit(self.config.players_count)

    def _start_human(self):
        self._sync_count()
        self.start_human.emit(self.config.players_count)

# =============================
# Main Screen
# =============================
class MainScreen(QtWidgets.QWidget):
    open_settings = QtCore.Signal()
    go_game = QtCore.Signal()
    go_human = QtCore.Signal()
    open_about = QtCore.Signal()

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setObjectName("MainScreen")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("🏠 Home")
        title.setObjectName("H1")
        header.addWidget(title)
        header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("⚙️ Settings")
        settings_btn.setObjectName("GhostButton")
        settings_btn.clicked.connect(self.open_settings.emit)
        header.addWidget(settings_btn)

        card = QtWidgets.QGroupBox("Quick actions")
        card.setObjectName("Card")
        v = QtWidgets.QVBoxLayout(card)
        play_btn = QtWidgets.QPushButton("🎲 Start demo")
        play_btn.setObjectName("PrimaryButton")
        play_btn.clicked.connect(self.go_game.emit)
        v.addWidget(play_btn)
        play_human_btn = QtWidgets.QPushButton("🎮 Play yourself (1 human + AI)")
        play_human_btn.setObjectName("GhostButton")
        play_human_btn.clicked.connect(self.go_human.emit)
        v.addWidget(play_human_btn)

        self.status = QtWidgets.QGroupBox("Status")
        self.status.setObjectName("Card")
        self.grid = QtWidgets.QFormLayout(self.status)
        self.lbl_players = QtWidgets.QLabel(self._players_label())
        self.lbl_ai = QtWidgets.QLabel(self.config.ai_difficulty)
        self.lbl_sound = QtWidgets.QLabel("On" if self.config.sound_enabled else "Off")
        self.lbl_bright = QtWidgets.QLabel(f"{self.config.brightness}%")
        self.grid.addRow("Players:", self.lbl_players)
        self.grid.addRow("AI difficulty:", self.lbl_ai)
        self.grid.addRow("Sound:", self.lbl_sound)
        self.grid.addRow("Brightness:", self.lbl_bright)

        about_btn = QtWidgets.QPushButton("ℹ️ About")
        about_btn.setObjectName("GhostButton")
        about_btn.clicked.connect(self.open_about.emit)

        root.addLayout(header)
        root.addWidget(card)
        root.addWidget(self.status)
        root.addStretch(1)
        root.addWidget(about_btn, alignment=QtCore.Qt.AlignRight)

    def refresh(self):
        self.lbl_players.setText(self._players_label())
        self.lbl_ai.setText(self.config.ai_difficulty)
        self.lbl_sound.setText("On" if self.config.sound_enabled else "Off")
        self.lbl_bright.setText(f"{self.config.brightness}%")

    def _players_label(self) -> str:
        mapping = {2: "2 players", 3: "3 players", 4: "4 players"}
        return mapping.get(self.config.players_count, f"{self.config.players_count} players")

# =============================
# Settings Screen
# =============================
class SettingsScreen(QtWidgets.QWidget):
    back = QtCore.Signal()

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setObjectName("SettingsScreen")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("⚙️ Settings")
        title.setObjectName("H1")
        header.addWidget(title)
        header.addStretch(1)
        back_btn = QtWidgets.QPushButton("← Back")
        back_btn.setObjectName("GhostButton")
        back_btn.clicked.connect(self.back.emit)
        header.addWidget(back_btn)

        # Card 1: Game settings
        card1 = QtWidgets.QGroupBox("Game settings")
        card1.setObjectName("Card")
        f1 = QtWidgets.QFormLayout(card1)

        self.players = QtWidgets.QComboBox()
        self.players.addItems(["2 players", "3 players", "4 players"])
        self.players.setCurrentText({2:"2 players",3:"3 players",4:"4 players"}.get(self.config.players_count, "2 players"))
        self.players.setFixedHeight(44)
        f1.addRow("Players:", self.players)

        self.ai_level = QtWidgets.QComboBox()
        self.ai_level.addItems(["Easy", "Normal", "Hard"])
        # Map any legacy values to English
        if self.config.ai_difficulty in ["Makkelijk", "Moeilijk"]:
            self.config.ai_difficulty = {"Makkelijk":"Easy","Moeilijk":"Hard"}[self.config.ai_difficulty]
        self.ai_level.setCurrentText(self.config.ai_difficulty)
        self.ai_level.setFixedHeight(44)
        f1.addRow("AI difficulty:", self.ai_level)

        # Card 2: Audio & Display
        card2 = QtWidgets.QGroupBox("Audio & Display")
        card2.setObjectName("Card")
        f2 = QtWidgets.QFormLayout(card2)
        self.sound = QtWidgets.QCheckBox("Enable sound effects")
        self.sound.setChecked(self.config.sound_enabled)
        f2.addRow(self.sound)

        self.brightness = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(self.config.brightness)
        self.brightness.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.brightness.setTickInterval(10)
        f2.addRow("Brightness:", self.brightness)

        # Actions
        actions = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.setObjectName("GhostButton")
        cancel_btn.clicked.connect(self.back.emit)
        actions.addStretch(1)
        actions.addWidget(cancel_btn)
        actions.addWidget(save_btn)

        root.addLayout(header)
        root.addWidget(card1)
        root.addWidget(card2)
        root.addStretch(1)
        root.addLayout(actions)

    def _save(self):
        text = self.players.currentText()
        self.config.players_count = int(text.split()[0])
        self.config.ai_difficulty = self.ai_level.currentText()
        self.config.sound_enabled = self.sound.isChecked()
        self.config.brightness = int(self.brightness.value())
        QtWidgets.QMessageBox.information(self, "Saved", "Settings saved.")
        self.back.emit()

# =============================
# Game Screen with auto simulation + digital board
# =============================
class GameScreen(QtWidgets.QWidget):
    back_main = QtCore.Signal()

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.state = GameState()
        self.setObjectName("GameScreen")

        # flags
        self.human_mode = False   # True = player 1 is human
        self.turn_busy = False

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        self.h1 = QtWidgets.QLabel("🎮 Game")
        self.h1.setObjectName("H1")
        header.addWidget(self.h1)
        header.addStretch(1)
        self.btn_rules = QtWidgets.QPushButton("📜 Special tiles")
        self.btn_rules.setObjectName("GhostButton")
        self.btn_rules.clicked.connect(self._show_rules)
        header.addWidget(self.btn_rules)
        self.btn_back = QtWidgets.QPushButton("⏹ Stop & Back")
        self.btn_back.setObjectName("GhostButton")
        self.btn_back.clicked.connect(self._stop_and_back)
        header.addWidget(self.btn_back)

        # Digital board
        self.board = DigitalBoard(tile_count=self.state.tile_count)

        # Actions (only visible in human mode)
        actions = QtWidgets.QHBoxLayout()
        self.btn_roll = QtWidgets.QPushButton("🎲 Roll dice")
        self.btn_roll.setObjectName("PrimaryButton")
        self.btn_roll.clicked.connect(self._human_roll)
        self.btn_roll.setEnabled(False)
        actions.addWidget(self.btn_roll)
        self.lbl_hint = QtWidgets.QLabel("—")
        actions.addWidget(self.lbl_hint)
        actions.addStretch(1)

        # Status
        status = QtWidgets.QGroupBox("Status")
        status.setObjectName("Card")
        form = QtWidgets.QFormLayout(status)
        self.lbl_turn = QtWidgets.QLabel("—")
        self.lbl_last = QtWidgets.QLabel("—")
        self.lbl_done = QtWidgets.QLabel("—")
        form.addRow("Turn:", self.lbl_turn)
        form.addRow("Last roll:", self.lbl_last)
        form.addRow("Finishers:", self.lbl_done)

        # Log
        log_card = QtWidgets.QGroupBox("Log")
        log_card.setObjectName("Card")
        v = QtWidgets.QVBoxLayout(log_card)
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log)

        root.addLayout(header)
        root.addWidget(self.board)
        root.addLayout(actions)
        root.addWidget(status)
        root.addWidget(log_card, stretch=1)

        self.winner_shown = False
        self._move_anim = None

    # ======= Start =======
    def start_demo(self):
        self._start_common(human=False)

    def start_human_game(self):
        self._start_common(human=True)

    def _start_common(self, human: bool):
        n = max(2, min(4, self.config.players_count))
        self.state = GameState(positions=[0]*n)
        self.board.reset_tokens(n)
        self.state.current = 0
        self.state.finished = False
        self.finish_order = []
        self.winner_shown = False
        self.human_mode = human
        self._move_anim = None
        self.turn_busy = False
        # per-player special state: skip turns, waiting, wait counters, extra roll
        self.skip_turns = [0] * n
        self.waiting = [False] * n
        self.wait_turns = [0] * n
        self.extra_roll = [False] * n
        mode = "HUMAN" if human else "DEMO"
        self._append_log(f"Game started ({mode}) with {n} players. Goal: tile {self.state.tile_count}.")
        self.h1.setText("🎮 Game (play yourself)" if human else "🎬 Demo simulation")
        self._update_labels()
        # Action button visible/enabled in human mode
        self.btn_roll.setVisible(self.human_mode)
        self._advance_turn()

    # ======= Turn loop =======
    def _advance_turn(self):
        if self.state.finished:
            return
        i = self.state.current
        # Check skip finished players
        # skip finished players
        if i in getattr(self, 'finish_order', []):
            self.state.current = self._next_player(i)
            QtCore.QTimer.singleShot(10, self._advance_turn)
            return
        # Check skip-one-turn flag
        if getattr(self, 'skip_turns', None) is not None and self.skip_turns[i] > 0:
            self._append_log(f"Player {i+1} skips a turn ({self.skip_turns[i]} left)")
            self.skip_turns[i] -= 1
            self.state.current = self._next_player(i)
            QtCore.QTimer.singleShot(10, self._advance_turn)
            return
        # Check waiting flag: if waiting, decrement wait counter and possibly continue waiting
        if getattr(self, 'waiting', None) is not None and self.waiting[i]:
            # If wait_turns reaches 0, release; otherwise skip this player's turn
            if self.wait_turns[i] <= 0:
                self.waiting[i] = False
                self._append_log(f"Player {i+1} is no longer waiting.")
            else:
                self.wait_turns[i] -= 1
                self._append_log(f"Player {i+1} is waiting ({self.wait_turns[i]} turns left)")
                self.state.current = self._next_player(i)
                QtCore.QTimer.singleShot(10, self._advance_turn)
                return
        self._update_labels()
        if self.human_mode and i == 0:
            # player 1 is human
            self.lbl_hint.setText("Your turn. Click 🎲 Roll dice.")
            self.btn_roll.setEnabled(True)
        else:
            # AI turn: small delay for UX
            self.lbl_hint.setText("AI thinking…")
            delay = {"Easy": 300, "Normal": 600, "Hard": 900}.get(self.config.ai_difficulty, 600)
            QtCore.QTimer.singleShot(delay, self._ai_turn)

    def _ai_turn(self):
        if self.turn_busy or self.state.finished:
            return
        i = self.state.current
        roll = random.randint(1, 6)
        # Show the same dice animation for AI
        dlg = DiceRollDialog(roll, self)
        dlg.start()
        self._execute_roll(i, roll)

    def _human_roll(self):
        if self.turn_busy or self.state.finished:
            return
        if self.state.current != 0:
            return
        self.btn_roll.setEnabled(False)
        roll = random.randint(1, 6)
        # Show dice animation before executing
        dlg = DiceRollDialog(roll, self)
        dlg.start()
        self._execute_roll(0, roll)

    # ======= Execute roll =======
    def _execute_roll(self, i: int, roll: int):
        self.turn_busy = True
        self.lbl_last.setText(str(roll))
        start = self.state.positions[i]
        newpos = min(self.state.tile_count, start + roll)
        self._append_log(f"Player {i+1} rolls {roll} → moves to {newpos}")

        def after_move():
            oldpos = self.state.positions[i]
            self.state.positions[i] = newpos
            # Check if this movement passed any waiting players
            try:
                self._check_passed_players(i, oldpos, newpos)
            except Exception:
                pass
            # Special tiles
            rules = self._rules()
            if newpos in rules and newpos < self.state.tile_count:
                title, delta, msg = rules[newpos]
                # Determine adjusted position (adj). If delta is int, apply movement;
                # otherwise handle special keywords (finish/start) or keep same tile.
                if isinstance(delta, int):
                    adj = max(0, min(self.state.tile_count, newpos + delta))
                else:
                    s = str(delta).lower()
                    if s.startswith("finish"):
                        adj = self.state.tile_count
                    elif s.startswith("start"):
                        adj = 0
                    else:
                        adj = newpos

                # Show explanation dialog first
                if self.config.sound_enabled:
                    QtWidgets.QApplication.beep()
                dlg = SpecialTileDialog(tile=newpos, title=title, delta=delta, message=msg, parent=self)
                dlg.exec()

                # Set special state flags for non-numeric effects
                if not isinstance(delta, int):
                    s2 = str(delta).lower()
                    if "skip" in s2:
                        # skip one turn
                        self.skip_turns[i] += 1
                    if "wait" in s2:
                        # wait until another player passes — limit to 2 turns
                        self.waiting[i] = True
                        # parse number if present, otherwise default 2
                        import re
                        m = re.search(r"(\d+)", s2)
                        self.wait_turns[i] = int(m.group(1)) if m else 2
                    if "roll" in s2:
                        # grant an extra roll
                        self.extra_roll[i] = True

                # Visual effect and jump (pulse shows either numeric delta or descriptive text)
                if isinstance(delta, int):
                    color = QtGui.QColor("#22c55e") if delta>0 else QtGui.QColor("#ef4444")
                    pulse = f"{('+' if delta>0 else '')}{delta}"
                else:
                    color = QtGui.QColor("#0ea5e9")
                    pulse = str(delta)

                self.board.effect_pulse(newpos, pulse, color)
                self._append_log(f"  Special tile {newpos}: {pulse} → {adj}")

                def after_special():
                    old = self.state.positions[i]
                    self.state.positions[i] = adj
                    # Check pass release
                    try:
                        self._check_passed_players(i, old, adj)
                    except Exception:
                        pass
                    self.board.update_token(i, adj)
                    self._finish_or_next(i)

                self._animate_move(i, newpos, adj, on_done=after_special, duration=450)
                return

            self._finish_or_next(i)

        self._animate_move(i, start, newpos, on_done=after_move, duration=350)

    # ======= Animations =======
    def _animate_move(self, player: int, start_tile: int, end_tile: int, on_done=None, duration=350):
        if start_tile == end_tile:
            if on_done:
                QtCore.QTimer.singleShot(0, on_done)
            return
        a = self.board.path[max(0, min(start_tile, len(self.board.path)-1))]
        b = self.board.path[max(0, min(end_tile, len(self.board.path)-1))]
        token = self.board.tokens[player]
        dx = (player % 2) * 10 - 5
        dy = (player // 2) * 10 - 5
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        def _step(v):
            x = a.x() + (b.x()-a.x())*v + dx
            y = a.y() + (b.y()-a.y())*v + dy
            token.setPos(x, y)
        def _end():
            if on_done:
                on_done()
        anim.valueChanged.connect(_step)
        anim.finished.connect(_end)
        anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
        self._move_anim = anim

    # ======= End / Next =======
    def _finish_or_next(self, i: int):
        # first finisher → dialog
        if self.state.positions[i] >= self.state.tile_count and i not in getattr(self, 'finish_order', []):
            self.finish_order.append(i)
            if not self.winner_shown:
                self.winner_shown = True
                dlg = WinDialog(i, self)
                choice = dlg.exec()
                if choice == QtWidgets.QDialog.Rejected:
                    self._stop_and_back()
                    return
        # all finished?
        if len(self.finish_order) == len(self.state.positions):
            self.state.finished = True
            self._append_log("All players finished! Game over.")
            QtWidgets.QMessageBox.information(self, "Game over", "The simulation has ended.")
            self._update_labels()
            self._move_anim = None
            self.turn_busy = False
            return
        # next player or extra roll
        self._move_anim = None
        self.turn_busy = False
        # If player has an extra roll, consume it and give them the next turn
        if getattr(self, 'extra_roll', None) is not None and self.extra_roll[i]:
            self.extra_roll[i] = False
            self._append_log(f"Player {i+1} gets an extra roll")
            self.state.current = i
            self._update_labels()
            QtCore.QTimer.singleShot(300, self._advance_turn)
            return
        # otherwise next player
        self.state.current = self._next_player(i)
        self._update_labels()
        # continue to next turn
        QtCore.QTimer.singleShot(300, self._advance_turn)

    def _check_passed_players(self, mover: int, start: int, end: int):
        """If mover moved from start to end and passed any waiting players, release them."""
        if start == end:
            return
        low, high = (start, end) if start < end else (end, start)
        for j, waiting in enumerate(getattr(self, 'waiting', [])):
            if j == mover:
                continue
            if waiting:
                pos_j = self.state.positions[j]
                # If mover passed over pos_j (exclusive of start, inclusive of end)
                if low <= pos_j < high:
                    self.waiting[j] = False
                    self.wait_turns[j] = 0
                    self._append_log(f"Player {j+1} is released because player {mover+1} passed them.")

    def _next_player(self, i):
        n = len(self.state.positions)
        j = (i + 1) % n
        while j in getattr(self, 'finish_order', []):
            j = (j + 1) % n
        return j

    def _update_labels(self):
        if not self.state.finished:
            self.lbl_turn.setText(f"Player {self.state.current+1}")
        else:
            self.lbl_turn.setText("—")
        self.lbl_done.setText(
            ", ".join([f"P{idx+1}" for idx in getattr(self, 'finish_order', [])]) or "—"
        )

    def _append_log(self, text: str):
        self.log.append(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _stop_and_back(self):
        self.back_main.emit()

    def _show_rules(self):
        dlg = SpecialRulesDialog(self._rules(), self)
        dlg.exec()

    def _rules(self) -> dict[int, tuple[str,int,str]]:
        # central place – used by dialog and game logic
        # Only include the tiles and events the user requested. 'z28' interpreted as 28.
        # delta is relative; non-movement events use delta=0 and a descriptive message.
        return {
            1:  ("Station Leuven (Leuven Station)", "Start", "You arrived in Leuven!"),
            3:  ("Ladeuzeplein (Ladeuze Square)", +2, "You feel lucky under the giant beetle!"),
            7:  ("Oude Markt (Old Market Square)", "Skip one turn", "You drank too much on the Oude Markt — skip one turn."),
            10: ("M-Museum Leuven (M Museum)", +2, "The art inspires you — move ahead!"),
            11: ("Kruidtuin (Botanical Garden)", "Wait until another player passes (max 2 turns)", "You fell asleep among the flowers — wait for someone to wake you up."),
            14: ("Stella Artois Brouwerij (Stella Artois Brewery)", -14, "You drank too much beer — back to start!"),
            17: ("Sportoase Philipssite (Sport Center)", "Roll again", "You feel refreshed — roll again!"),
            19: ("UZ Gasthuisberg (University Hospital)", "skip one turn", "You need to rest — skip one turn."),
            22: ("Heverleebos (Heverlee Forest)", -2, "You got lost in the forest — move 2 back."),
            25: ("STUK Kunstencentrum (STUK Arts Center)", "Roll again", "The concert gives you energy — roll again!"),
            28: ("Bibliotheek Tweebronnen (Public Library)", "Wait until another player passes (max 2 turns)", "You read too long — wait until another player passes."),
            29: ("Wintertijd (Leuven Christmas Market)", "skip one turn", "The waffles and lights keep you here — skip one turn."),
            31: ("Heilige-Geestcollege (Holy Spirit College)", +4, "Knowledge pushes you ahead!"),
            32: ("Sluispark (Lock Park)", "Wait until another player passes (max 2 turns)", "You’re enjoying a picnic — wait for someone to pass."),
            35: ("KU Leuven Rectoraat (KU Leuven Headquarters)", "skip one turn", "You’re called in for a meeting with the rector — skip one turn."),
            36: ("Groep T Leuven (Group T Leuven)", "Finish", "Congratulations — you reached the end! 🧠"),
        }

# =============================
# Shell (Router)
# =============================
class Shell(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GAME4 Companion")
        self.resize(1000, 700)
        self.config = AppConfig()

        self.stack = QtWidgets.QStackedWidget()
        self.setCentralWidget(self.stack)

        # Screens
        self.start = StartScreen(self.config)
        self.main = MainScreen(self.config)
        self.settings = SettingsScreen(self.config)
        self.game = GameScreen(self.config)

        self.stack.addWidget(self.start)      # 0
        self.stack.addWidget(self.main)       # 1
        self.stack.addWidget(self.settings)   # 2
        self.stack.addWidget(self.game)       # 3

        # Navigation
        self.start.go_main.connect(lambda: self._go(1))
        self.start.start_demo.connect(self._start_demo_from_start)
        self.start.start_human.connect(self._start_human_from_start)
        self.main.go_game.connect(self._start_demo_from_main)
        self.main.go_human.connect(self._start_human_from_main)
        self.main.open_settings.connect(lambda: self._go(2))
        self.settings.back.connect(self._back_to_main)
        self.game.back_main.connect(self._back_to_main)

        # 🔁 Synchronize players between Start and Settings
        self.start.players_combo.currentTextChanged.connect(self._sync_players_from_start_text)
        self.settings.players.currentIndexChanged.connect(self._sync_players_from_settings)

        # Menu
        self._build_menu()

        # Stylesheet
        self.setStyleSheet(self._qss())

    def _go(self, index: int):
        self.stack.setCurrentIndex(index)

    def _back_to_main(self):
        self.main.refresh()
        self._go(1)

    def _start_demo_from_main(self):
        self._go(3)
        self.game.start_demo()

    def _start_human_from_main(self):
        self._go(3)
        self.game.start_human_game()

    def _start_demo_from_start(self, players: int):
        self._go(3)
        self.game.start_demo()

    def _start_human_from_start(self, players: int):
        self._go(3)
        self.game.start_human_game()

    def _build_menu(self):
        bar = self.menuBar()
        app_menu = bar.addMenu("App")
        act_home = QtGui.QAction("Go to home", self)
        act_home.triggered.connect(lambda: self._go(1))
        app_menu.addAction(act_home)

        act_settings = QtGui.QAction("Settings", self)
        act_settings.triggered.connect(lambda: self._go(2))
        app_menu.addAction(act_settings)

        act_demo = QtGui.QAction("Start demo", self)
        act_demo.triggered.connect(self._start_demo_from_main)
        app_menu.addAction(act_demo)

        app_menu.addSeparator()
        act_quit = QtGui.QAction("Quit", self)
        act_quit.triggered.connect(QtWidgets.QApplication.instance().quit)
        app_menu.addAction(act_quit)

    # ==== Sync methods with signal blocking to avoid loops ====
    def _sync_players_from_start_text(self, text: str):
        """When changed in Start screen (combo), update Settings + config."""
        try:
            self.settings.players.blockSignals(True)
            self.settings.players.setCurrentText(text)
        finally:
            self.settings.players.blockSignals(False)
        self.config.players_count = int(text.split()[0])

    def _sync_players_from_settings(self, index: int):
        """When changed in Settings, update Start combo + config."""
        text = self.settings.players.currentText()
        try:
            self.start.players_combo.blockSignals(True)
            self.start.players_combo.setCurrentText(text)
        finally:
            self.start.players_combo.blockSignals(False)
        self.config.players_count = int(text.split()[0])

    def _qss(self) -> str:
        return """
        /* Global text color */
        * { color: #0f172a; }

        QMainWindow {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                          stop:0 #f8fafc, stop:1 #ffffff);
        }
        QMenuBar, QMenu { color: #0f172a; }

        /* Typography */
        QLabel#Title { font-size: 40px; font-weight: 700; letter-spacing: 0.5px; color: #0f172a; }
        QLabel#Subtitle { font-size: 16px; color: #0f172a; }
        QLabel#Info { font-size: 12px; color: #0f172a; }
        QLabel#H1 { font-size: 24px; font-weight: 600; }

        /* Buttons */
        QPushButton#PrimaryButton {
            background: #0ea5e9; color: white; border: none;
            padding: 12px 18px; border-radius: 12px; font-size: 16px;
        }
        QPushButton#PrimaryButton:hover { background: #0284c7; }
        QPushButton#PrimaryButton:pressed { background: #0369a1; }

        QPushButton#GhostButton {
            background: transparent; border: 1px solid #e2e8f0;
            color: #0f172a; padding: 8px 14px; border-radius: 10px;
        }
        QPushButton#GhostButton:hover { background: #f1f5f9; }

        /* Cards */
        QGroupBox#Card {
            border: 1px solid #e2e8f0; border-radius: 14px; margin-top: 18px;
        }
        QGroupBox#Card::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0f172a; }

        QProgressBar { border: 1px solid #e2e8f0; border-radius: 8px; height: 20px; text-align: center; }
        QProgressBar::chunk { background: #0ea5e9; }

        QSlider::groove:horizontal { height: 6px; background: #e2e8f0; border-radius: 3px; }
        QSlider::handle:horizontal {
            width: 16px; height: 16px; border-radius: 8px; background: #0ea5e9; margin: -6px 0;
        }

        /* ComboBox styled */
        QComboBox {
            border: 1px solid #e2e8f0; background: #ffffff; border-radius: 10px;
            padding: 6px 10px; font-size: 16px; color: #0f172a; min-height: 44px;
        }
        QComboBox:hover { background: #ffffff; }
        QComboBox::drop-down { border: none; width: 28px; }
        QComboBox::down-arrow {
            image: url("data:image/svg+xml;utf8,\
<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'>\
<path d='M0 0 L10 0 L5 6 Z' fill='%230f172a'/></svg>");
        }
        QComboBox QAbstractItemView {
            background: #ffffff; border: 1px solid #e2e8f0; selection-background-color: #e2e8f0; color: #0f172a;
        }

        /* Make all dialogs clearly light to avoid any dark overlays */
        QDialog { background: #ffffff; }
        QDialog QLabel { color: #0f172a; }

        /* Readable Log window */
        QTextEdit {
            background: #ffffff;
            color: #0f172a;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        }

        /* Table styling inside dialogs */
        QTableWidget {
            background: #ffffff;
            color: #0f172a;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            gridline-color: #e2e8f0;
            alternate-background-color: #f8fafc;
        }
        QHeaderView::section {
            background: #f1f5f9;
            color: #0f172a;
            border: 1px solid #e2e8f0;
            padding: 4px;
        }
        """

# =============================
# Entrypoint
# =============================

def main():
    app = QtWidgets.QApplication([])
    app.setApplicationName("GAME4 Companion")
    win = Shell()
    win.show()
    app.exec()

if __name__ == "__main__":
    main()
