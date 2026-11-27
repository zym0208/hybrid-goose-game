# goose_theme.py
# Goose theme module: visuals + roaming geese (confined to center panel) + mirroring fix.

from __future__ import annotations
import os
import random
from dataclasses import dataclass
import shiboken6

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import QMovie


THEME_KEY = "goose"
THEME_LABEL = "Goose Classic"


def theme_assets_dir(base_dir: str) -> str:
    # base_dir = folder where GUI.py lives
    return os.path.join(base_dir, "assets", "themes", "goose")


def build_tile_metas_generic():
    return {
        1:  ("Goose Nest", "Start", ""),
        2:  ("Feather Path", "—", ""),
        3:  ("Pond Edge", "—", ""),
        4:  ("Mud Trap", "Skip one turn",
             "You got stuck in sticky mud. Skip one turn."),
        5:  ("Reed Crossing", "—", ""),
        6:  ("Golden Egg Hill", "Throw again",
             "You found a lucky golden egg. Throw again."),
        7:  ("Quack Lane", "—", ""),
        8:  ("Willow Bend", "—", ""),
        9:  ("Old Duck Chapel", "Move places forward",
             "A friendly goose guide shows you a shortcut. Move forward to tile 12."),
        10: ("Pebble Shore", "—", ""),
        11: ("Barn Bridge", "—", ""),
        12: ("Sunny Pond", "—", ""),
        13: ("Tall Grass Trail", "—", ""),
        14: ("Fox Ambush Field", "Go back to the beginning",
             "A fox scared the whole flock away. Go back to the start."),
        15: ("Windmill Road", "—", ""),
        16: ("Twisted Reed Maze", "Throw again and go that amount back",
             "You flap around in confusion. Throw again, then move that many places back."),
        17: ("Feather Gate", "—", ""),
        18: ("Rain Puddle Row", "—", ""),
        19: ("Crooked Fence", "—", ""),
        20: ("River Current", "Move places forward",
             "The river current carries you ahead to tile 24."),
        21: ("Goose Meadow", "—", ""),
        22: ("Quiet Nest Library", "Skip one turn",
             "You stop to read ancient goose wisdom. Skip one turn."),
        23: ("Cornfield Corner", "—", ""),
        24: ("Farmer's Yard", "—", ""),
        25: ("Waddle Walk", "—", ""),
        26: ("Festival Pond", "Roll again",
             "The pond festival gives you energy. Roll again!"),
        27: ("Soft Feather Street", "—", ""),
        28: ("Night Barn", "Skip two turns",
             "You fell asleep in the warm barn. Skip two turns."),
        29: ("Foggy Hollow", "—", ""),
        30: ("Bridge of Echoes", "—", ""),
        31: ("Wing Training Field", "Move 1 space forward",
             "Your wings feel stronger. Move 1 tile forward."),
        32: ("Lake View Point", "—", ""),
        33: ("Egg Basket Lane", "—", ""),
        34: ("Misty Marsh", "—", ""),
        35: ("Scarecrow Cinema", "Go back to square 9",
             "A terrifying scarecrow sends you running back to tile 9."),
        36: ("Royal Goose Castle", "Finish!",
             "Congratulations, you reached the Royal Goose Castle!"),
    }


def pixmap_mirror_h(pm: QtGui.QPixmap) -> QtGui.QPixmap:
    """
    Some PySide builds don't expose QPixmap.mirrored().
    Reliable path: convert to QImage -> mirrored -> back to QPixmap.
    """
    img = pm.toImage()
    # QImage.mirrored(horizontal, vertical)
    img2 = img.mirrored(True, False)
    return QtGui.QPixmap.fromImage(img2)



@dataclass
class GooseState:
    label: QtWidgets.QLabel
    movie: QMovie
    vx: int
    vy: int
    size: int
    mirror: bool



def inner_rect_from_tile_items(tile_items, pad: int = 10) -> QtCore.QRectF | None:
    """
    Build the inner roaming rectangle using the tiles as boundaries.

    tile_items: iterable of QGraphicsItems (your 36 tile items)
    pad: extra padding inside the inner boundary so geese don't touch the tiles

    Returns QRectF in SCENE coordinates, or None if it can't compute.
    """
    tile_items = list(tile_items)
    if not tile_items:
        return None

    union: QtCore.QRectF | None = None
    first_w = None
    first_h = None

    for it in tile_items:
        try:
            r = it.mapRectToScene(it.boundingRect())
        except Exception:
            continue

        if r.isNull():
            continue

        if union is None:
            union = QtCore.QRectF(r)
            first_w = r.width()
            first_h = r.height()
        else:
            union = union.united(r)

    if union is None or union.isNull() or first_w is None or first_h is None:
        return None

    inner = union.adjusted(first_w + pad, first_h + pad, -(first_w + pad), -(first_h + pad))
    if inner.width() < 80 or inner.height() < 80:
        return None

    return inner


class GooseOverlay(QtCore.QObject):
    """
    Creates 2 roaming goose GIF labels as overlay widgets (parented to the QGraphicsView viewport).
    Constrained to the center panel area.
    """
    def __init__(self, view: QtWidgets.QGraphicsView, center_scene_rect_getter):
        super().__init__(view)
        self.view = view
        self._center_scene_rect_getter = center_scene_rect_getter
        self._geese: list[GooseState] = []
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self, gif_path: str, size_a: int, size_b: int):
        if not os.path.exists(gif_path):
            return

        self._geese = [
            self._add_goose(gif_path, size=size_a, side="left"),
            self._add_goose(gif_path, size=size_b, side="right"),
        ]

        # Slow but playful: slightly lower tick rate
        self._timer.start(80)

    def stop(self):
        self._timer.stop()

        for g in self._geese:
            try:
                g.movie.frameChanged.disconnect()
            except Exception:
                pass

            try:
                g.movie.stop()
            except Exception:
                pass

            try:
                if g.label is not None and shiboken6.isValid(g.label):
                    g.label.hide()
                    g.label.deleteLater()
            except Exception:
                pass

        self._geese.clear()

    def clamp_into_allowed(self):
        allowed = self._allowed_rect_view()
        pad = 2
        for g in self._geese:
            s = g.size
            min_x = allowed.left() + pad
            max_x = allowed.right() - s - pad
            min_y = allowed.top() + pad
            max_y = allowed.bottom() - s - pad
            nx = max(min_x, min(g.label.x(), max_x))
            ny = max(min_y, min(g.label.y(), max_y))
            g.label.move(nx, ny)

    # ---------- internals ----------
    def _lake_rect_view(self) -> QtCore.QRect:
        """Central lake exclusion zone (35% of allowed rect, centred)."""
        a = self._allowed_rect_view()
        lw = int(a.width() * 0.45)
        lh = int(a.height() * 0.45)
        lx = a.left() + (a.width() - lw) // 2
        ly = a.top() + (a.height() - lh) // 2
        return QtCore.QRect(lx, ly, lw, lh)

    def _allowed_rect_view(self) -> QtCore.QRect:
        """
        Allowed area in VIEW coordinates (viewport coords),
        based on center panel scene rect.
        """
        sr: QtCore.QRectF | None = None
        try:
            sr = self._center_scene_rect_getter()
        except Exception:
            sr = None

        if sr is None or sr.isNull():
            return self.view.viewport().rect()

        tl = self.view.mapFromScene(sr.topLeft())
        br = self.view.mapFromScene(sr.bottomRight())
        vr = QtCore.QRect(tl, br).normalized()

        # padding so geese don't touch border
        pad = 8
        vr = vr.adjusted(pad, pad, -pad, -pad)
        if vr.width() < 80 or vr.height() < 80:
            return self.view.viewport().rect()
        return vr

    def _set_goose_frame(self, g: GooseState):
        if g is None:
            return

        if g.label is None:
            return

        try:
            if shiboken6.isValid(g.label) is False:
                return
        except Exception:
            return

        pm = g.movie.currentPixmap()
        if pm.isNull():
            return

        if pm.size() != QtCore.QSize(g.size, g.size):
            pm = pm.scaled(g.size, g.size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)

        if g.mirror:
            pm = pixmap_mirror_h(pm)

        try:
            g.label.setPixmap(pm)
        except RuntimeError:
            return

    def _add_goose(self, gif_path: str, size: int, side: str = "left") -> GooseState:
        lbl = QtWidgets.QLabel(self.view.viewport())
        lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        lbl.setStyleSheet("background: transparent;")
        lbl.setFixedSize(size, size)

        movie = QMovie(gif_path)
        movie.setScaledSize(QtCore.QSize(size, size))

        allowed = self._allowed_rect_view()
        pad = 2
        mid_x = allowed.left() + allowed.width() // 2
        y = random.randint(allowed.top() + pad, max(allowed.top() + pad, allowed.bottom() - size - pad))
        if side == "left":
            x = random.randint(allowed.left() + pad, max(allowed.left() + pad, mid_x - size - pad))
        else:
            x = random.randint(mid_x + pad, max(mid_x + pad, allowed.right() - size - pad))
        lbl.move(x, y)

        # left-side goose starts moving right, right-side goose starts moving left
        vx = 2 if side == "left" else -2
        vy = random.choice([-1, 1])
        mirror = (vx < 0)

        g = GooseState(label=lbl, movie=movie, vx=vx, vy=vy, size=size, mirror=mirror)

        def on_frame_changed(_i, gg=g):
            self._set_goose_frame(gg)

        movie.frameChanged.connect(on_frame_changed)
        movie.start()

        lbl.show()
        self._set_goose_frame(g)
        return g

    def _tick(self):
        if not self._geese:
            return

        allowed = self._allowed_rect_view()
        lake = self._lake_rect_view()
        pad = 2

        for g in self._geese:
            s = g.size
            x = g.label.x() + g.vx
            y = g.label.y() + g.vy

            min_x = allowed.left() + pad
            max_x = allowed.right() - s - pad
            min_y = allowed.top() + pad
            max_y = allowed.bottom() - s - pad

            bounced = False

            if x < min_x or x > max_x:
                g.vx *= -1
                x = max(min_x, min(x, max_x))
                bounced = True

            if y < min_y or y > max_y:
                g.vy *= -1
                y = max(min_y, min(y, max_y))
                bounced = True

            # bounce off lake exclusion zone
            goose_rect = QtCore.QRect(int(x), int(y), s, s)
            if goose_rect.intersects(lake):
                prev_x = g.label.x()
                prev_y = g.label.y()
                overlap_x = goose_rect.intersected(lake).width()
                overlap_y = goose_rect.intersected(lake).height()
                if overlap_x <= overlap_y:
                    g.vx *= -1
                    x = prev_x
                else:
                    g.vy *= -1
                    y = prev_y
                bounced = True


            new_mirror = (g.vx < 0)
            if new_mirror != g.mirror or bounced:
                g.mirror = new_mirror
                self._set_goose_frame(g)

            g.label.move(x, y)