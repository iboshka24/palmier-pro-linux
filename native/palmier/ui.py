import copy
import html
import json
import math
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PySide6.QtCore import Qt, Signal, Slot, QObject, QTimer, QUrl, QRectF, QSize, QEvent
from PySide6.QtGui import QAction, QKeySequence, QPainter, QColor, QPen, QPixmap, QFont, QIcon, QGuiApplication
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QAbstractScrollArea, QStackedWidget,
    QFileDialog, QMessageBox, QSlider, QFormLayout, QSpinBox, QDoubleSpinBox, QComboBox,
    QDialog, QDialogButtonBox, QLineEdit, QTextEdit, QInputDialog, QProgressBar, QCheckBox, QListView, QMenu)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
from .model import Editor, end
from .media import probe, render, duration, frozen_editor, ExportQueue, run, executable
from .mcp import Tools, MCPServer
from .chat import converse
from .platforms import desktop_name, ui_font
from . import VERSION

THEME = """
QWidget {background:#0F1115;color:#E8EAF0;font-family:'Inter';font-size:13px;}
QMainWindow {background:#0F1115;}
QMenuBar {background:#14171D;color:#E8EAF0;padding:4px;}
QMenuBar::item:selected {background:#232833;border-radius:6px;}
QMenu {background:#171B22;border:1px solid #2A3040;border-radius:8px;padding:6px;}
QMenu::item {padding:6px 18px;border-radius:6px;}
QMenu::item:selected {background:#2B3B5C;}
QStatusBar {background:#14171D;color:#9AA3B5;}
QLabel {color:#E8EAF0;}
QPushButton {background:#1E2430;border:1px solid #2E3648;border-radius:8px;padding:7px 12px;font-weight:600;}
QPushButton:hover {background:#283050;border-color:#3D4A6B;}
QPushButton:pressed {background:#1A2130;}
QPushButton:disabled {color:#5B6373;}
QPushButton#accent {background:#4C7DFF;border-color:#4C7DFF;color:white;}
QPushButton#accent:hover {background:#5B8BFF;}
QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit {background:#141821;border:1px solid #2A3040;border-radius:8px;padding:7px;selection-background-color:#4C7DFF;}
QComboBox:focus,QLineEdit:focus {border-color:#4C7DFF;}
QListWidget,QTextEdit {background:#12151C;border:1px solid #232A3A;border-radius:10px;padding:4px;}
QListWidget::item {border-radius:8px;padding:6px;}
QListWidget::item:selected {background:#24304D;}
QListWidget::item:hover {background:#1B2130;}
QSplitter::handle {background:#1C212C;width:1px;height:1px;}
QSplitter::handle:hover {background:#4C7DFF;}
QSlider::groove:horizontal {height:5px;background:#232A3A;border-radius:3px;}
QSlider::handle:horizontal {width:14px;margin:-5px 0;background:#E8EAF0;border-radius:7px;}
QSlider::handle:horizontal:hover {background:#4C7DFF;}
QProgressBar {border:1px solid #2A3040;border-radius:6px;background:#141821;text-align:center;color:#E8EAF0;}
QProgressBar::chunk {background:#3FB97F;border-radius:5px;}
QToolTip {background:#1E2430;color:#E8EAF0;border:1px solid #2E3648;padding:6px;}
QScrollBar:vertical {background:transparent;width:10px;}
QScrollBar::handle:vertical {background:#2A3040;border-radius:5px;min-height:30px;}
QScrollBar::handle:vertical:hover {background:#3D4A6B;}
QScrollBar:horizontal {background:transparent;height:10px;}
QScrollBar::handle:horizontal {background:#2A3040;border-radius:5px;min-width:30px;}
"""
THEME = THEME.replace("'Inter'", f"'{ui_font()}'")
COLORS = dict(video="#2F6FED", audio="#2FA37C", image="#8A63D2", text="#E8912D")
ACCENT = "#4C7DFF"


class Bridge(QObject):
    changed = Signal()
    completed = Signal(object, object, object)
    playback = Signal(str, int)
    progress = Signal(int, str)


class Timeline(QAbstractScrollArea):
    selected = Signal(str)
    seek = Signal(int)
    mediaDropped = Signal(object, int, int)

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.scale = 2.0
        self.selected_id = None
        self.drag = None
        self.ghost = None
        self.snap = True
        self.setMinimumHeight(170)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.viewport().setContextMenuPolicy(Qt.CustomContextMenu)
        self.viewport().customContextMenuRequested.connect(self.context_menu)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())

    def refresh(self):
        with self.editor.lock:
            self.t = copy.deepcopy(self.editor.timeline)
            self.media_names = {m["id"]:m["name"] for m in self.editor.manifest["entries"]}
            self.frame = self.editor.current_frame
        extent = max(duration(self.t) + self.t["fps"]*5, self.t["fps"]*30)
        self.horizontalScrollBar().setRange(0, max(0, round(extent*self.scale) - self.viewport().width()+160))
        self.verticalScrollBar().setRange(0, max(0, len(self.t["tracks"])*58+44-self.viewport().height()))
        self.viewport().update()

    def position(self, frame):
        return 160 + frame*self.scale - self.horizontalScrollBar().value()

    def frame_at(self, x):
        return max(0, round((x-160+self.horizontalScrollBar().value())/self.scale))

    def row_at(self, y):
        return math.floor((y-44+self.verticalScrollBar().value())/58)

    def paintEvent(self, event):
        if not hasattr(self, "t"):
            return
        from PySide6.QtGui import QPainterPath
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.viewport().rect(), QColor("#0F1115"))
        p.setFont(QFont(ui_font(), 9))
        scroll_y = self.verticalScrollBar().value()
        header_h, row_h, ruler_h, marker_h = 160, 58, 28, 16
        top = ruler_h + marker_h
        for i, tr in enumerate(self.t["tracks"]):
            y = top+i*row_h-scroll_y
            p.fillRect(QRectF(0,y,self.viewport().width(),row_h), QColor("#14171D" if i%2==0 else "#0F1115"))
            p.setPen(QColor("#232A3A"))
            p.drawLine(0,round(y+row_h-1),self.viewport().width(),round(y+row_h-1))
            for c in tr["clips"]:
                x, w = self.position(c["startFrame"]), c["durationFrames"]*self.scale
                if x+w < header_h or x > self.viewport().width():
                    continue
                p.save()
                p.setClipRect(QRectF(header_h,top,self.viewport().width()-header_h,self.viewport().height()-top))
                rect = QRectF(x,y+5,max(2,w-2),row_h-10)
                selected = c["id"] == self.selected_id
                base = QColor(COLORS.get(c["mediaType"], "#8A63D2"))
                if selected:
                    base = base.lighter(125)
                p.setBrush(base)
                p.setPen(QPen(QColor("#FFFFFF" if selected else "#00000000"),2 if selected else 1))
                p.drawRoundedRect(rect,8,8)
                # fade triangles
                fps = self.t["fps"]
                for key, left in (("fadeInFrames", True), ("fadeOutFrames", False)):
                    frames = c.get(key, 0) or 0
                    if frames > 0 and w > 14:
                        fw = min(w/2, frames*fps/ max(fps,1) * self.scale if False else frames*self.scale)
                        fw = min(float(w/2), frames*self.scale)
                        tri = QPainterPath()
                        if left:
                            tri.moveTo(rect.left(), rect.bottom()); tri.lineTo(rect.left()+fw, rect.bottom()); tri.lineTo(rect.left(), rect.bottom()-10)
                        else:
                            tri.moveTo(rect.right(), rect.bottom()); tri.lineTo(rect.right()-fw, rect.bottom()); tri.lineTo(rect.right(), rect.bottom()-10)
                        p.fillPath(tri, QColor(255,255,255,70))
                image=getattr(self,"images",{}).get(c["mediaRef"])
                if image and not image.isNull():
                    p.setClipRect(rect.adjusted(1,20,-1,-1),Qt.IntersectClip)
                    thumb=image.scaled(64,27,Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
                    if c["mediaType"]=="audio":
                        p.setOpacity(0.85)
                        p.drawPixmap(rect.adjusted(1,20,-1,-1).toRect(),image)
                        p.setOpacity(1.0)
                    else:
                        for pos in range(max(header_h,round(x)),min(self.viewport().width(),round(x+w)),64):
                            p.drawPixmap(pos,round(y+25),thumb)
                    p.restore()
                    p.save()
                    p.setClipRect(QRectF(header_h,top,self.viewport().width()-header_h,self.viewport().height()-top))
                p.setClipRect(rect.adjusted(5,0,-5,0), Qt.IntersectClip)
                p.setPen(QColor("#FFFFFF"))
                label = c.get("textContent") if c["mediaType"] == "text" else self.media_names.get(c["mediaRef"], "Missing media")
                badges = ""
                if c.get("color"):
                    badges += " ●"
                if c.get("effects"):
                    badges += f" fx{len(c['effects'])}"
                if c.get("linkGroupId"):
                    badges += " 🔗"
                if c.get("denoise"):
                    badges += " 🎧"
                p.drawText(rect.adjusted(8,4,-6,-4), Qt.AlignLeft|Qt.AlignTop, (label or "")[:48] + badges)
                p.setPen(QColor(255,255,255,180))
                p.drawText(rect.adjusted(8,22,-6,-4), Qt.AlignLeft|Qt.AlignTop, f"{c['durationFrames']/self.t['fps']:.2f}s · {c.get('speed',1):g}x")
                p.restore()
            p.fillRect(QRectF(0,y,header_h,row_h),QColor("#14171D"))
            p.setPen(QColor("#9AA3B5"))
            p.drawText(QRectF(12,y,100,row_h),Qt.AlignVCenter, tr.get("name") or (("A" if tr["type"]=="audio" else "V")+str(i+1)))
            flags = ""
            if tr.get("muted"):
                flags += "M"
            if tr.get("hidden"):
                flags += "H"
            if not flags:
                flags = "♪" if tr["type"]=="audio" else "●"
            p.setPen(QColor("#E8B44F" if tr.get("muted") or tr.get("hidden") else "#9AA3B5"))
            p.drawText(QRectF(118,y,36,row_h),Qt.AlignCenter, flags)
            if tr.get("syncLocked", True):
                p.setPen(QColor("#3FB97F"))
                p.drawText(QRectF(140,y,18,row_h),Qt.AlignCenter,"⚡")
        # ruler + markers
        p.fillRect(QRectF(0,0,self.viewport().width(),ruler_h+marker_h),QColor("#14171D"))
        for m in self.t.get("markers", []):
            x = self.position(m.get("startFrame", 0))
            if x >= header_h:
                p.setPen(QColor(m.get("color", "#E8B44F")))
                p.drawLine(round(x), ruler_h, round(x), ruler_h+marker_h)
                p.fillRect(QRectF(x, ruler_h, 8, 8), QColor(m.get("color", "#E8B44F")))
                p.drawText(QRectF(x+10, ruler_h, 90, marker_h), Qt.AlignVCenter, str(m.get("name", ""))[:18])
        step = max(1, math.ceil(70/self.scale/self.t["fps"])) * self.t["fps"]
        first = self.frame_at(160)//step*step
        p.setPen(QColor("#949499"))
        for f in range(first, self.frame_at(self.viewport().width())+step, step):
            x = self.position(f)
            if x >= 160:
                p.setPen(QColor("#5B6373"))
                p.drawLine(round(x),18,round(x),28)
                secs = f/self.t['fps']
                label = f"{int(secs//60):02}:{int(secs%60):02}" if secs >= 60 else f"{secs:.0f}s"
                p.setPen(QColor("#9AA3B5"))
                p.drawText(QRectF(x+4,0,100,26),Qt.AlignVCenter, label)
        p.fillRect(QRectF(0,0,160,28),QColor("#14171D"))
        p.setPen(QColor("#E8EAF0"))
        p.drawText(QRectF(12,0,145,28),Qt.AlignVCenter,"Timeline")
        p.fillRect(QRectF(0,28,self.viewport().width(),16),QColor("#10131A"))
        p.setPen(QColor("#5B6373"))
        p.drawText(QRectF(12,28,145,16),Qt.AlignVCenter,"Markers")
        p.setPen(QPen(QColor("#FF5C5C"),2))
        x = self.position(self.frame)
        if x >= 160:
            p.drawLine(round(x),0,round(x),self.viewport().height())
            p.fillRect(QRectF(x-5,0,10,10), QColor("#FF5C5C"))
        if self.ghost:
            start, length, row = self.ghost
            p.setPen(QPen(QColor("#4C7DFF"),2,Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.position(start),44+row*58-scroll_y+5,length*self.scale,48),8,8)

    def hit(self, pos):
        row = self.row_at(pos.y())
        if pos.x()<160 or not 0 <= row < len(self.t["tracks"]):
            return None
        frame = self.frame_at(pos.x())
        return next(((row,c) for c in reversed(self.t["tracks"][row]["clips"]) if c["startFrame"]<=frame<end(c)),None)

    def mousePressEvent(self, event):
        self.setFocus()
        if event.position().y()<28 and event.position().x()>=160:
            self.seek.emit(self.frame_at(event.position().x()))
            return
        if 28 <= event.position().y() < 44 and event.position().x() >= 160:
            self.seek.emit(self.frame_at(event.position().x()))
            return
        row = self.row_at(event.position().y())
        if event.position().x()<160 and 0<=row<len(self.t["tracks"]):
            tr = self.t["tracks"][row]
            key = "muted" if event.position().x()>115 else "hidden"
            self.window().act(lambda: self.editor.mutate("Track visibility/audio",lambda: self.editor.timeline["tracks"][row].update({key:not tr.get(key)})))
            return
        hit = self.hit(event.position())
        if hit:
            row,c = hit
            self.selected_id = c["id"]
            self.selected.emit(c["id"])
            mode = "left" if abs(event.position().x()-self.position(c["startFrame"]))<7 else "right" if abs(event.position().x()-self.position(end(c)))<7 else "move"
            self.drag = dict(row=row, clip=copy.deepcopy(c), x=event.position().x(), mode=mode)
            self.viewport().update()

    def snap_frame(self, frame, exclude_id=None):
        if not self.snap:
            return max(0, frame)
        points = [self.frame, 0]
        for tr in self.t["tracks"]:
            for c in tr["clips"]:
                if c["id"] == exclude_id:
                    continue
                points.append(c["startFrame"])
                points.append(end(c))
        threshold = max(1, round(8 / max(self.scale, 0.2)))
        best, best_distance = frame, threshold + 1
        for point in points:
            distance = abs(point - frame)
            if distance < best_distance:
                best, best_distance = point, distance
        return max(0, best)

    def mouseMoveEvent(self, event):
        if self.drag:
            c=self.drag["clip"]
            delta=round((event.position().x()-self.drag["x"])/self.scale)
            if self.drag["mode"]=="move":
                row=max(0,min(len(self.t["tracks"])-1,self.row_at(event.position().y())))
                start=self.snap_frame(max(0,c["startFrame"]+delta), c["id"])
                self.ghost=(start,c["durationFrames"],row)
            elif self.drag["mode"]=="right":
                length=max(1,c["durationFrames"]+delta)
                end_frame=self.snap_frame(c["startFrame"]+length, c["id"])
                self.ghost=(c["startFrame"],max(1,end_frame-c["startFrame"]),self.drag["row"])
            else:
                start=max(0,min(end(c)-1,c["startFrame"]+delta))
                start=min(self.snap_frame(start, c["id"]), end(c)-1)
                self.ghost=(start,end(c)-start,self.drag["row"])
            self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self.drag and self.ghost:
            c=self.drag["clip"]
            start,length,row=self.ghost
            mode=self.drag["mode"]
            def edit():
                if mode=="move":
                    self.editor.move([dict(clipId=c["id"],toFrame=start,toTrack=row)])
                elif mode=="right":
                    self.editor.properties([c["id"]],dict(durationFrames=length))
                else:
                    def trim():
                        self.editor.properties([c["id"]],dict(durationFrames=length,trimStartFrame=c.get("trimStartFrame",0)+round((start-c["startFrame"])*c.get("speed",1))))
                        self.editor.move([dict(clipId=c["id"],toFrame=start)], follow_links=False)
                    self.editor.mutate("Trim start",trim)
            self.window().act(edit)
        self.drag=self.ghost=None
        self.refresh()

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-palmier-media"):
            event.acceptProposedAction()

    def dragMoveEvent(self,event):
        event.acceptProposedAction()

    def dropEvent(self,event):
        row=self.row_at(event.position().y())
        frame=self.frame_at(event.position().x())
        try:
            if event.mimeData().hasFormat("application/x-palmier-media"):
                payload = json.loads(bytes(event.mimeData().data("application/x-palmier-media")))
                if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
                    raise ValueError("Dropped media is not a list of ids")
            else:
                payload = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        except Exception:
            self.window().statusBar().showMessage("Could not read the dropped item", 8000)
            event.ignore()
            return
        if payload:
            self.mediaDropped.emit(payload,frame,row)
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End, Qt.Key_BracketLeft, Qt.Key_BracketRight):
            self.window().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def context_menu(self, pos):
        menu = QMenu(self)
        win = self.window()
        hit = self.hit(pos) if hasattr(self, "t") else None
        if hit:
            _, clip = hit
            self.selected_id = clip["id"]
            self.selected.emit(clip["id"])
            self.viewport().update()
            menu.addAction("Split at playhead", win.split_selected)
            menu.addAction("Duplicate", win.duplicate_selected)
            menu.addAction("Ripple delete", win.ripple_delete_selected)
            menu.addAction("Delete", win.delete_selected)
            menu.addSeparator()
        menu.addAction("Paste", win.paste_selected)
        menu.exec(self.viewport().mapToGlobal(pos))


class MediaList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setViewMode(QListView.IconMode)
        self.setIconSize(QSize(96,54))
        self.setGridSize(QSize(112,96))
        self.setResizeMode(QListView.Adjust)
        self.setWordWrap(True)

    def mimeTypes(self):
        return ["application/x-palmier-media"]

    def mimeData(self,items):
        from PySide6.QtCore import QMimeData
        data=QMimeData()
        data.setData("application/x-palmier-media",json.dumps([item.data(Qt.UserRole) for item in items]).encode())
        return data


class Window(QMainWindow):
    def __init__(self, port=19789):
        super().__init__()
        self.editor=Editor()
        self.exports=ExportQueue(self.editor)
        self.tools=Tools(self.editor,self.exports)
        self.bridge=Bridge()
        self.editor.changed=self.bridge.changed.emit
        self.bridge.changed.connect(self.refresh)
        self.bridge.completed.connect(self.finish_task, Qt.QueuedConnection)
        self.bridge.playback.connect(self.control_playback)
        self.bridge.progress.connect(self.show_progress)
        self.tools.playback=self.bridge.playback.emit
        self.progress_owner=None
        self.pool=ThreadPoolExecutor(max_workers=2)
        self.cache=tempfile.TemporaryDirectory(prefix="palmier-preview-")
        self.preview_cancel=threading.Event()
        self.preview_future=None
        self.preview_revision=-1
        self.preview_job_revision=-1
        self.selected_id=None
        self.images={}
        self.thumbnail_pending=set()
        self.import_busy=False
        self.closing=False
        self.resize(1440,900)
        self.setMinimumSize(1032,600)
        self.setStyleSheet(THEME)
        self.player=QMediaPlayer(self)
        self.audio=QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(1)
        self.player.positionChanged.connect(self.play_position)
        self.player.playbackStateChanged.connect(self.sync_play_button)
        self.player.errorOccurred.connect(lambda error,msg: self.statusBar().showMessage("Playback: "+msg,15000))
        self.build_ui()
        self.build_menu()
        try:
            self.mcp=MCPServer(self.tools,port)
            self.connection.setText(f"MCP · 127.0.0.1:{self.mcp.port}/mcp")
        except OSError as error:
            self.mcp=None
            self.connection.setText("MCP unavailable: "+str(error))
        self.write_agent_intro()
        self.refresh()
        self.poll=QTimer(self)
        self.poll.timeout.connect(self.refresh_exports)
        self.poll.start(300)

    def panel(self,title):
        widget=QWidget()
        layout=QVBoxLayout(widget)
        layout.setContentsMargins(10,8,10,8)
        layout.setSpacing(8)
        header=QLabel(title.upper())
        header.setStyleSheet("color:#9AA3B5;font-weight:700;letter-spacing:1px;")
        header.setFixedHeight(24)
        layout.addWidget(header)
        return widget,layout

    def build_ui(self):
        outer=QSplitter(Qt.Horizontal)
        agent,al=self.panel("Agent")
        self.chat_log=QTextEdit()
        self.chat_log.setReadOnly(True)
        self.chat_log.setPlainText("Starting MCP…")
        al.addWidget(self.chat_log)
        self.chat_input=QLineEdit()
        self.chat_input.setPlaceholderText("Ask your agent…")
        self.chat_input.returnPressed.connect(self.send_chat)
        al.addWidget(self.chat_input)
        self.model_input=QLineEdit("gpt-4.1-mini")
        al.addWidget(self.model_input)
        self.chat_send=QPushButton("Send · BYOK OpenAI")
        self.chat_send.clicked.connect(self.send_chat)
        al.addWidget(self.chat_send)
        agent.setMinimumWidth(240)
        outer.addWidget(agent)
        work=QSplitter(Qt.Vertical)
        top=QSplitter(Qt.Horizontal)
        media,ml=self.panel("Library")
        self.import_button=QPushButton("+ Import")
        self.import_button.clicked.connect(self.import_dialog)
        ml.addWidget(self.import_button)
        self.search=QLineEdit()
        self.search.setPlaceholderText("Search media")
        self.search.textChanged.connect(self.filter_media)
        ml.addWidget(self.search)
        self.library_empty=QLabel("No media yet. Import a file or drop it on the timeline.")
        self.library_empty.setWordWrap(True)
        self.library_empty.setStyleSheet("color:#9AA3B5;padding:8px;")
        ml.addWidget(self.library_empty)
        self.library=MediaList()
        self.library.setContextMenuPolicy(Qt.CustomContextMenu)
        self.library.customContextMenuRequested.connect(self.library_menu)
        self.library.itemDoubleClicked.connect(lambda item:self.place_media([item.data(Qt.UserRole)],self.editor.current_frame,-1))
        ml.addWidget(self.library)
        media.setMinimumWidth(240)
        top.addWidget(media)
        preview,pl=self.panel("Preview")
        self.preview_stack=QStackedWidget()
        self.still=QLabel("Import media, then drag it into the timeline")
        self.still.setAlignment(Qt.AlignCenter)
        self.still.setStyleSheet("background:#000;color:#5B6373;border-radius:12px;font-size:14px;")
        self.still.setMinimumSize(320,240)
        self.video=QLabel()
        self.video.setAlignment(Qt.AlignCenter)
        self.video.setMinimumSize(320,240)
        self.video.setStyleSheet("background:#000;border-radius:12px;")
        self.sink=QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.present_frame)
        self.preview_stack.addWidget(self.still)
        self.preview_stack.addWidget(self.video)
        pl.addWidget(self.preview_stack,1)
        controls=QHBoxLayout()
        controls.setSpacing(8)
        self.timecode=QLabel("00:00:00:00")
        self.timecode.setStyleSheet("color:#E8B44F;font-family:monospace;font-size:15px;font-weight:700;background:#141821;padding:6px 12px;border-radius:8px;")
        self.timecode.setToolTip("Click to copy the timecode")
        self.timecode.installEventFilter(self)
        controls.addWidget(self.timecode)
        for label,callback in [("◀",lambda:self.seek(max(0,self.editor.current_frame-1))), ("Play",self.toggle_play), ("▶|",lambda:self.seek(self.editor.current_frame+1))]:
            button=QPushButton(label)
            button.clicked.connect(callback)
            if callback==self.toggle_play:
                self.play_button=button
            controls.addWidget(button)
        pl.addLayout(controls)
        top.addWidget(preview)
        inspector,il=self.panel("Inspector")
        form=QFormLayout()
        form.setSpacing(6)
        self.clip_label=QLabel("Select a clip")
        self.clip_label.setStyleSheet("font-weight:700;font-size:14px;")
        il.addWidget(self.clip_label)
        self.clip_meta=QLabel("")
        self.clip_meta.setStyleSheet("color:#9AA3B5;")
        il.addWidget(self.clip_meta)
        self.fields={}
        color_keys = ("volumeDb","speed","opacity","exposure","contrast","saturation","temperature","highlights","shadows")
        for key,label in [("startFrame","Start frame"),("durationFrames","Duration frames"),("trimStartFrame","Source trim"),("volumeDb","Volume dB"),("speed","Speed"),("opacity","Opacity"),("fadeInFrames","Fade in frames"),("fadeOutFrames","Fade out frames"),
                          ("exposure","Exposure"),("contrast","Contrast"),("saturation","Saturation"),("temperature","Temperature"),("highlights","Highlights"),("shadows","Shadows")]:
            field=QDoubleSpinBox() if key in color_keys else QSpinBox()
            if hasattr(field, "setDecimals"):
                field.setDecimals(3 if key in ("opacity","speed") else 2 if key in color_keys else 0)
            if key=="volumeDb": low,high=-60,15
            elif key=="speed": low,high=.05,16
            elif key=="opacity": low,high=0,1
            elif key in ("exposure","contrast","saturation","temperature","highlights","shadows"): low,high=-10,10
            elif key=="durationFrames": low,high=1,100_000_000
            else: low,high=0,100_000_000
            field.setRange(low,high)
            if key in ("exposure","contrast","saturation","temperature","highlights","shadows"):
                field.setSingleStep(0.1)
            form.addRow(label,field)
            self.fields[key]=field
        il.addLayout(form)
        apply=QPushButton("Apply properties")
        apply.setObjectName("accent")
        apply.clicked.connect(self.apply_properties)
        il.addWidget(apply)
        row2=QHBoxLayout()
        self.effect_box=QComboBox()
        self.effect_box.addItems(["grayscale","blur","sharpen","vignette","mirror","denoise-light"])
        row2.addWidget(self.effect_box)
        fx_add=QPushButton("+ FX")
        fx_add.clicked.connect(self.apply_effect_ui)
        row2.addWidget(fx_add)
        fx_clear=QPushButton("Clear FX")
        fx_clear.clicked.connect(self.clear_effects_ui)
        row2.addWidget(fx_clear)
        il.addLayout(row2)
        row3=QHBoxLayout()
        self.denoise_check=QCheckBox("Denoise")
        row3.addWidget(self.denoise_check)
        dn_apply=QPushButton("Apply denoise")
        dn_apply.clicked.connect(self.apply_denoise_ui)
        row3.addWidget(dn_apply)
        color_reset=QPushButton("Reset color")
        color_reset.clicked.connect(lambda:self.act(lambda:self.editor.apply_color([self.selected_id],reset=True)) if self.selected_id else None)
        row3.addWidget(color_reset)
        il.addLayout(row3)
        self.text_content=QLineEdit()
        self.text_content.setPlaceholderText("Text clip content")
        il.addWidget(self.text_content)
        update=QPushButton("Update text")
        update.clicked.connect(lambda:self.act(lambda:self.tools.call("update_text",dict(clipIds=[self.selected_id],content=self.text_content.text()))))
        il.addWidget(update)
        il.addStretch()
        inspector.setMinimumWidth(290)
        top.addWidget(inspector)
        top.setSizes([352,600,300])
        work.addWidget(top)
        bottom=QWidget()
        bl=QVBoxLayout(bottom)
        bl.setContentsMargins(0,0,0,0)
        toolbar=QHBoxLayout()
        toolbar.setSpacing(6)
        for text,callback,tip in [("Undo",lambda:self.act(self.editor.undo),"Undo (Ctrl+Z)"),("Redo",lambda:self.act(lambda:self.editor.undo(True)),"Redo (Ctrl+Shift+Z)"),
                              ("Split",self.split_selected,"Split at playhead (Ctrl+K)"),("Delete",self.delete_selected,"Delete clip (Del)"),
                              ("[",lambda:self.trim_playhead(True),"Trim start to playhead"),("]",lambda:self.trim_playhead(False),"Trim end to playhead"),
                              ("T",self.add_text,"Add text"),("Marker",self.add_marker_ui,"Add marker at playhead"),("CC",self.add_caption_ui,"Add caption"),
                              ("Layout",self.apply_layout_ui,"Split-screen / PiP layout"),
                              ("+ V",lambda:self.act(lambda:self.editor.add_track("video")),"Add video track"),("+ A",lambda:self.act(lambda:self.editor.add_track("audio")),"Add audio track"),
                              ("Export",self.export_dialog,"Export (Ctrl+E)")]:
            b=QPushButton(text)
            b.setToolTip(tip)
            if text == "Export":
                b.setObjectName("accent")
            b.clicked.connect(callback)
            toolbar.addWidget(b)
        self.snap_box=QCheckBox("Snap")
        self.snap_box.setChecked(True)
        self.snap_box.setToolTip("Snap clip edges to the playhead and other edits while dragging")
        self.snap_box.toggled.connect(lambda checked: setattr(self.timeline, "snap", checked))
        toolbar.addWidget(self.snap_box)
        toolbar.addStretch()
        self.zoom=QSlider(Qt.Horizontal)
        self.zoom.setRange(2,120)
        self.zoom.setValue(20)
        self.zoom.setMaximumWidth(140)
        self.zoom.valueChanged.connect(self.set_zoom)
        toolbar.addWidget(QLabel("Zoom"))
        toolbar.addWidget(self.zoom)
        bl.addLayout(toolbar)
        self.timeline=Timeline(self.editor)
        self.timeline.images=self.images
        self.timeline.selected.connect(self.select_clip)
        self.timeline.seek.connect(self.seek)
        self.timeline.mediaDropped.connect(self.place_media)
        bl.addWidget(self.timeline)
        work.addWidget(bottom)
        work.setSizes([560,300])
        outer.addWidget(work)
        outer.setSizes([240,1200])
        self.setCentralWidget(outer)
        self.summary=QLabel()
        self.summary.setStyleSheet("color:#9AA3B5;")
        self.statusBar().addWidget(self.summary)
        self.connection=QLabel()
        self.statusBar().addPermanentWidget(self.connection)
        self.progress=QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.setAcceptDrops(True)

    def build_menu(self):
        file=self.menuBar().addMenu("File")
        edit=self.menuBar().addMenu("Edit")
        playback=self.menuBar().addMenu("Playback")
        help_menu=self.menuBar().addMenu("Help")
        def action(menu,label,shortcut,callback):
            a=QAction(label,self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(callback)
            menu.addAction(a)
        for label,key,call in [("New project","Ctrl+N",self.new_project),("Open project…","Ctrl+O",self.open_project),("Save","Ctrl+S",self.save_project),("Save As…","Ctrl+Shift+S",lambda:self.save_project(True)),("Import media…","Ctrl+I",self.import_dialog),("Export…","Ctrl+E",self.export_dialog),("Quit","Ctrl+Q",self.close)]:
            action(file,label,key,call)
        for label,key,call in [("Undo","Ctrl+Z",lambda:self.act(self.editor.undo)),("Redo","Ctrl+Shift+Z",lambda:self.act(lambda:self.editor.undo(True))),("Split at playhead","Ctrl+K",self.split_selected),("Delete clip","Delete",self.delete_selected),("Ripple delete","Shift+Delete",self.ripple_delete_selected),("Duplicate clip","Ctrl+D",self.duplicate_selected),("Cut clip","Ctrl+X",self.cut_selected),("Copy clip","Ctrl+C",self.copy_selected),("Paste clip","Ctrl+V",self.paste_selected)]:
            action(edit,label,key,call)
        action(playback,"Play / Pause","Space",self.toggle_play)
        action(playback,"Previous edit",None,lambda:self.jump_edit(-1))
        action(playback,"Next edit",None,lambda:self.jump_edit(1))
        action(help_menu,"Keyboard shortcuts",None,self.show_shortcuts)
        action(help_menu,"MCP connection instructions",None,self.show_mcp_help)
        action(help_menu,"About / License",None,lambda:QMessageBox.information(self,desktop_name(),f"GPLv3 development port · {VERSION}\nBased on Palmier, Inc. last-gpl-source.\nNative Qt 6 / FFmpeg. Not feature-complete or pixel-perfect.\nSee README, LICENSE and third-party notices."))
        action(help_menu,"About Qt",None,lambda:QMessageBox.aboutQt(self,"Qt licensing"))

    def mcp_url(self):
        port = self.mcp.port if getattr(self, "mcp", None) else 19789
        return f"http://127.0.0.1:{port}/mcp"

    def write_agent_intro(self):
        url = self.mcp_url()
        self.chat_log.setPlainText(
            "Palmier Pro Linux\n\n"
            f"MCP  {url}\n\n"
            "OpenCode: add a remote MCP server named palmier-pro with this URL, restart OpenCode, and keep this window open.\n\n"
            f"Codex: codex mcp add palmier-pro --url {url}\n\n"
            "Editing, markers, ripple, layouts, captions, color, effects and export work offline. "
            "Generation tools report canGenerate:false — this GPL port has no subscription backend.\n\n"
            "BYOK chat uses OPENAI_API_KEY. It cannot import, export, or switch projects."
        )

    def show_mcp_help(self):
        url = self.mcp_url()
        QMessageBox.information(self, "MCP", self.connection.text() +
            f"\n\nOpenCode: add mcp.palmier-pro with type remote and url {url} in ~/.config/opencode/opencode.jsonc, then restart OpenCode.\n\n"
            f"codex mcp add palmier-pro --url {url}\n\n"
            "Loopback only; browser origins are refused. Set PALMIER_MCP_TOKEN to require Authorization: Bearer.")

    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts",
            "Space  play / pause\n"
            "Left / Right  step one frame (Shift = five)\n"
            "Up / Down  previous / next edit\n"
            "Home / End  start / end of the timeline\n"
            "Ctrl+K  split    Delete  delete    Shift+Delete  ripple delete\n"
            "Ctrl+D  duplicate    Ctrl+C/X/V  copy, cut, paste\n"
            "[ / ]  trim the selected clip to the playhead\n"
            "Ctrl+Z / Ctrl+Shift+Z  undo / redo\n"
            "Ctrl+I import    Ctrl+S save    Ctrl+E export\n"
            "Click a timecode to copy it. Right-click a clip or a library item for more actions.")

    def act(self,callback):
        try:
            return callback()
        except Exception as error:
            message = str(error)
            if message.startswith("Nothing to "):
                self.statusBar().showMessage(message, 4000)
                return None
            QMessageBox.warning(self,"Palmier Pro",message)

    def task(self,function,callback):
        future=self.pool.submit(function)
        def done(f):
            try:
                result,error=f.result(),None
            except Exception as ex:
                result,error=None,ex
            if not self.closing:
                self.bridge.completed.emit(callback,result,error)
        future.add_done_callback(done)
        return future

    @Slot(object, object, object)
    def finish_task(self,callback,result,error):
        callback(result,error)

    def show_progress(self, value, label):
        self.progress_owner = label
        self.progress.show()
        self.progress.setValue(max(0, min(100, int(value))))
        self.progress.setFormat(label + " %p%")
        if value >= 100 and label != "Export":
            QTimer.singleShot(700, self.hide_idle_progress)

    def hide_idle_progress(self):
        if self.progress_owner != "Export":
            self.progress.hide()
            self.progress_owner = None

    def inspector_editing(self):
        focus = self.focusWidget()
        return focus in self.fields.values() or focus is self.text_content

    @Slot()
    def refresh(self):
        if self.closing:
            return
        if self.preview_job_revision != -1 and self.preview_job_revision != self.editor.revision:
            self.preview_cancel.set()
        self.setWindowTitle(self.editor.name+(" *" if self.editor.dirty else "")+" — "+desktop_name())
        self.timeline.refresh()
        fps = self.editor.timeline["fps"] or 1
        frames = duration(self.editor.timeline)
        self.summary.setText(f"{self.editor.timeline['width']}×{self.editor.timeline['height']}  ·  {self.editor.timeline['fps']} fps  ·  {frames / fps:.2f}s")
        with self.editor.lock:
            media=copy.deepcopy(self.editor.manifest["entries"])
        selected=self.library.currentItem().data(Qt.UserRole) if self.library.currentItem() else None
        scroll=self.library.verticalScrollBar().value()
        self.library.clear()
        for m in media:
            try:
                missing=not self.editor.media_path(m).is_file()
            except ValueError:
                missing=True
            item=QListWidgetItem(("⚠ " if missing else "")+m["name"]+f"\n{m['type']} · {m['duration']:.2f}s")
            item.setData(Qt.UserRole,m["id"])
            if m["id"] in self.images:
                item.setIcon(QIcon(self.images[m["id"]]))
            self.library.addItem(item)
            if m["id"]==selected:
                self.library.setCurrentItem(item)
            if not missing and m["id"] not in self.images and m["id"] not in self.thumbnail_pending:
                self.thumbnail_pending.add(m["id"])
                source=self.editor.media_path(m)
                mid=m["id"]
                kind=m["type"]
                target=Path(self.cache.name)/("thumbnail-"+mid+".png")
                def thumbnail(source=source,target=target,kind=kind):
                    args=[executable("ffmpeg"),"-v","error","-y","-i",str(source)]
                    if kind=="audio":
                        args += ["-filter_complex","aformat=channel_layouts=mono,showwavespic=s=512x64:colors=white"]
                    else:
                        args += ["-vf","scale=192:108:force_original_aspect_ratio=decrease,pad=192:108:(ow-iw)/2:(oh-ih)/2"]
                    run(args+["-frames:v","1",str(target)],timeout=60)
                    return target
                def ready(file,error,mid=mid):
                    self.thumbnail_pending.discard(mid)
                    if error or not file or not Path(file).is_file():
                        if error:
                            self.statusBar().showMessage("Thumbnail failed: "+str(error)[-180:], 8000)
                        return
                    if len(self.images)>=256:
                        del self.images[next(iter(self.images))]
                    self.images[mid]=QPixmap(str(file))
                    self.refresh()
                self.task(thumbnail,ready)
        self.library.verticalScrollBar().setValue(scroll)
        self.filter_media()
        if self.preview_revision!=self.editor.revision and self.player.playbackState()==QMediaPlayer.PlayingState:
            self.player.pause()
            self.statusBar().showMessage("Timeline changed — press Play to rebuild composited playback")
        if self.selected_id and not self.inspector_editing():
            try:
                self.select_clip(self.selected_id)
            except ValueError:
                self.selected_id=None
                self.timeline.selected_id=None
                self.clip_label.setText("Select a clip")
                self.clip_meta.setText("")
                self.text_content.clear()

    def filter_media(self):
        query=self.search.text().lower()
        visible=0
        for i in range(self.library.count()):
            item=self.library.item(i)
            hidden=query not in item.text().lower()
            item.setHidden(hidden)
            visible += not hidden
        if not self.library.count():
            self.library_empty.setText("No media yet. Import a file or drop it on the timeline.")
            self.library_empty.show()
        elif visible:
            self.library_empty.hide()
        else:
            self.library_empty.setText("No media matches that search.")
            self.library_empty.show()

    def select_clip(self,cid):
        self.selected_id=cid
        with self.editor.lock:
            _,c=self.editor.clip(cid)
            title = c.get("textContent", c["mediaType"]).split("\n")[0][:40] if isinstance(c.get("textContent", c["mediaType"]), str) else c["mediaType"]
            self.clip_label.setText(title)
            meta = f"{c['mediaType']} · {c['durationFrames']}f · trims {c.get('trimStartFrame',0)}/{c.get('trimEndFrame',0)}"
            if c.get("linkGroupId"):
                meta += " · linked"
            if c.get("effects"):
                meta += f" · {len(c['effects'])} fx"
            self.clip_meta.setText(meta)
            self.timeline.selected_id=cid
            color = c.get("color") or {}
            for key,field in self.fields.items():
                if key == "volumeDb":
                    value = 20*math.log10(max(c.get("volume",1),.001))
                elif key in ("exposure","contrast","saturation","temperature","highlights","shadows"):
                    value = float(color.get(key, 0))
                else:
                    value = c.get(key,0)
                try:
                    field.setValue(value)
                except Exception:
                    pass
            self.text_content.setText(c.get("textContent",""))
            self.denoise_check.setChecked(bool(c.get("denoise")))

    def apply_properties(self):
        if not self.selected_id:
            self.statusBar().showMessage("Select a clip before applying properties", 4000)
            return
        def edit():
            def action():
                _, c = self.editor.clip(self.selected_id)
                basic = {}
                current_db = round(20*math.log10(max(c.get("volume",1),.001)), 2)
                if abs(self.fields["volumeDb"].value() - current_db) > 0.001:
                    basic["volumeDb"] = self.fields["volumeDb"].value()
                for key in ("durationFrames","trimStartFrame","speed","opacity","fadeInFrames","fadeOutFrames"):
                    value = self.fields[key].value()
                    current = c.get("speed",1) if key=="speed" else c.get("opacity",1) if key=="opacity" else c.get(key,0)
                    if abs(float(value) - float(current)) > 1e-6:
                        basic[key] = value
                if basic:
                    self.editor.properties([self.selected_id], basic)
                color = c.get("color") or {}
                grade = {}
                for key in ("exposure","contrast","saturation","temperature","highlights","shadows"):
                    value = self.fields[key].value()
                    if abs(value - float(color.get(key, 0))) > 1e-6:
                        grade[key] = value
                if grade:
                    self.editor.apply_color([self.selected_id], grade)
                start = int(self.fields["startFrame"].value())
                if start != c["startFrame"]:
                    self.editor.move([dict(clipId=self.selected_id, toFrame=start)], follow_links=False)
            self.editor.mutate("Inspector edit", action)
        self.act(edit)

    def clear_effects_ui(self):
        if not self.selected_id:
            self.statusBar().showMessage("Select a clip to clear effects", 4000)
            return
        self.act(lambda: self.editor.apply_effect([self.selected_id], []))

    def apply_effect_ui(self):
        if not self.selected_id:
            return
        ftype = self.effect_box.currentText()
        self.act(lambda:self.editor.apply_effect([self.selected_id],[dict(type=ftype)]))

    def apply_denoise_ui(self):
        if not self.selected_id:
            return
        enabled = self.denoise_check.isChecked()
        self.act(lambda:self.editor.set_denoise([self.selected_id],enabled,0.6))

    def set_zoom(self,value):
        self.timeline.scale=value/10
        self.timeline.refresh()

    def split_selected(self):
        if self.selected_id:
            self.act(lambda:self.editor.split([dict(clipId=self.selected_id,atFrame=self.editor.current_frame)]))

    def delete_selected(self):
        if self.selected_id:
            self.act(lambda:self.editor.remove([self.selected_id]))

    def ripple_delete_selected(self):
        if not self.selected_id:
            self.statusBar().showMessage("Select a clip to ripple-delete", 4000)
            return
        selected = self.selected_id
        def action():
            tr, c = self.editor.clip(selected)
            index = self.editor.timeline["tracks"].index(tr)
            self.editor.ripple_delete_ranges([dict(trackIndex=index, startFrame=c["startFrame"], endFrame=end(c))])
        self.act(action)

    def duplicate_selected(self):
        if not self.selected_id:
            self.statusBar().showMessage("Select a clip to duplicate", 4000)
            return
        selected = self.selected_id
        def action():
            result = self.editor.duplicate_clips([selected])
            self.selected_id = result["clipIds"][0]
            self.timeline.selected_id = self.selected_id
        self.act(action)

    def jump_edit(self, direction):
        points = {0, duration(self.editor.timeline)}
        for tr in self.editor.timeline["tracks"]:
            for c in tr["clips"]:
                points.add(c["startFrame"])
                points.add(end(c))
        for marker in self.editor.timeline.get("markers", []):
            points.add(marker.get("startFrame", 0))
        ordered = sorted(points)
        frame = self.editor.current_frame
        if direction < 0:
            earlier = [point for point in ordered if point < frame]
            self.seek(earlier[-1] if earlier else 0)
        else:
            later = [point for point in ordered if point > frame]
            self.seek(later[0] if later else frame)

    def copy_selected(self):
        if self.selected_id:
            with self.editor.lock:
                self.clipboard=copy.deepcopy(self.editor.clip(self.selected_id)[1])

    def cut_selected(self):
        self.copy_selected()
        self.delete_selected()

    def paste_selected(self):
        if not getattr(self,"clipboard",None):
            return
        from .model import uid
        def action():
            c=copy.deepcopy(self.clipboard)
            c["id"],c["startFrame"]=uid(),self.editor.current_frame
            track=next((tr for tr in self.editor.timeline["tracks"] if (tr["type"]=="audio")== (c["mediaType"]=="audio")),None)
            if track is None:
                self.editor.add_track("audio" if c["mediaType"]=="audio" else "video")
                track=self.editor.timeline["tracks"][-1]
            self.editor.overwrite(track,c["startFrame"],end(c))
            track["clips"].append(c)
        self.act(lambda:self.editor.mutate("Paste clip",action))

    def trim_playhead(self,left):
        if not self.selected_id:
            return
        def action():
            _,c=self.editor.clip(self.selected_id)
            f=self.editor.current_frame
            if not c["startFrame"]<f<end(c):
                raise ValueError("Playhead must be inside selected clip")
            if left:
                self.editor.properties([c["id"]],dict(durationFrames=end(c)-f,trimStartFrame=c.get("trimStartFrame",0)+round((f-c["startFrame"])*c.get("speed",1))))
                self.editor.move([dict(clipId=c["id"],toFrame=f)], follow_links=False)
            else:
                self.editor.properties([c["id"]],dict(durationFrames=f-c["startFrame"]))
        self.act(lambda:self.editor.mutate("Trim to playhead",action))

    def add_text(self):
        text,ok=QInputDialog.getText(self,"Text overlay","Content")
        if ok and text:
            self.act(lambda:self.editor.add_texts([dict(startFrame=self.editor.current_frame,endFrame=self.editor.current_frame+self.editor.timeline["fps"]*5,content=text)]))

    def add_marker_ui(self):
        name, ok = QInputDialog.getText(self, "Marker", "Marker name", text="Review")
        if ok and name:
            self.act(lambda:self.editor.manage_markers(create=[dict(startFrame=self.editor.current_frame, durationFrames=0, name=name)]))

    def add_caption_ui(self):
        text, ok = QInputDialog.getText(self, "Caption", "Caption text")
        if ok and text:
            fps = self.editor.timeline["fps"]
            self.act(lambda:self.editor.add_captions([dict(startFrame=self.editor.current_frame,
                                                            endFrame=self.editor.current_frame + fps*3, text=text)]))

    def apply_layout_ui(self):
        names = sorted(self.editor.LAYOUTS)
        name, ok = QInputDialog.getItem(self, "Layout", "Choose a multi-video layout", names, 1, False)
        if not ok:
            return
        with self.editor.lock:
            clips = [c["id"] for tr in self.editor.timeline["tracks"] for c in tr["clips"] if c["mediaType"] in ("video", "image")][:len(self.editor.LAYOUTS[name])]
        if len(clips) < len(self.editor.LAYOUTS[name]):
            QMessageBox.information(self, "Layout", f"Layout {name} needs {len(self.editor.LAYOUTS[name])} visual clips on the timeline.")
            return
        slots = {slot: dict(clipIds=[cid]) for slot, cid in zip(sorted(self.editor.LAYOUTS[name]), clips)}
        self.act(lambda:self.editor.apply_layout(name, slots))

    def import_dialog(self):
        files,_=QFileDialog.getOpenFileNames(self,"Import media","","Media (*.mp4 *.mov *.mkv *.mp3 *.wav *.png *.jpg *.jpeg);;All files (*)")
        self.import_paths(files)

    def import_paths(self,files,placement=None):
        if not files or self.import_busy:
            return
        revision=self.editor.doc["activeTimelineId"]
        self.import_busy=True
        self.import_button.setEnabled(False)
        self.import_button.setText("Importing…")
        self.statusBar().showMessage("Inspecting media…")
        def work():
            return [probe(file) for file in files]
        def ready(entries,error):
            self.import_busy=False
            self.import_button.setEnabled(True)
            self.import_button.setText("+ Import")
            if error:
                self.act(lambda:(_ for _ in ()).throw(error))
                return
            if revision!=self.editor.doc["activeTimelineId"]:
                self.statusBar().showMessage("Import cancelled: active timeline changed")
                return
            self.editor.mutate("Import media",lambda:self.editor.manifest["entries"].extend(entries))
            self.statusBar().showMessage(f"Imported {len(entries)} media file" + ("" if len(entries)==1 else "s"))
            if placement:
                self.place_media([m["id"] for m in entries],*placement)
        self.task(work,ready)

    def library_menu(self, pos):
        item = self.library.itemAt(pos)
        if item is None:
            return
        self.library.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Relink file…", self.relink_selected_media)
        menu.addAction("Show in folder", self.reveal_selected_media)
        menu.exec(self.library.mapToGlobal(pos))

    def relink_selected_media(self):
        item = self.library.currentItem()
        if item is None:
            return
        media_id = item.data(Qt.UserRole)
        file, _ = QFileDialog.getOpenFileName(self, "Relink media", "", "Media (*.mp4 *.mov *.mkv *.mp3 *.wav *.png *.jpg *.jpeg);;All files (*)")
        if not file:
            return
        def ready(probed, error):
            if error:
                self.act(lambda: (_ for _ in ()).throw(error))
                return
            def action():
                self.editor.relink_media(media_id, probed)
                self.images.pop(media_id, None)
                self.thumbnail_pending.discard(media_id)
            self.act(action)
            self.statusBar().showMessage("Relinked " + probed.get("name", "media"))
        self.statusBar().showMessage("Reading replacement file…")
        self.task(lambda: probe(file), ready)

    def reveal_selected_media(self):
        item = self.library.currentItem()
        if item is None:
            return
        try:
            with self.editor.lock:
                entry = self.editor.media(item.data(Qt.UserRole))
                path = self.editor.media_path(entry) if entry else None
        except ValueError as error:
            self.statusBar().showMessage(str(error), 8000)
            return
        if path is None:
            return
        folder = path.parent if path.exists() else path.parent
        if not folder.is_dir():
            self.statusBar().showMessage("That folder is not on this computer", 8000)
            return
        try:
            subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            self.statusBar().showMessage(str(error), 8000)

    def place_media(self,payload,frame,row):
        if any(not self.editor.media(item) for item in payload):
            self.import_paths(payload,(frame,row))
            return
        def place():
            position=frame
            entries=[]
            for ref in payload:
                m=self.editor.media(ref)
                entry=dict(mediaRef=ref,startFrame=position)
                if 0<=row<len(self.editor.timeline["tracks"]):
                    entry["trackIndex"]=row
                entries.append(entry)
                position+=round(m["duration"]*self.editor.timeline["fps"])
            self.editor.add_clips(entries)
        self.act(place)

    def seek(self,frame):
        total=duration(self.editor.timeline)
        self.editor.current_frame=max(0,min(max(0,total-1),frame))
        self.update_timecode()
        self.timeline.refresh()
        if self.preview_revision==self.editor.revision:
            self.preview_stack.setCurrentWidget(self.video)
            self.player.setPosition(round(self.editor.current_frame/self.editor.timeline["fps"]*1000))
            return
        if not total:
            return
        self.preview_cancel.set()
        if self.preview_future:
            self.preview_future.cancel()
        cancel=threading.Event()
        self.preview_cancel=cancel
        revision=self.editor.revision
        self.preview_job_revision=revision
        snapshot=frozen_editor(self.editor)
        destination=Path(self.cache.name)/f"frame-{revision}-{self.editor.current_frame}.png"
        current=self.editor.current_frame
        self.still.setPixmap(QPixmap())
        self.still.setText("Rendering frame…")
        self.preview_stack.setCurrentWidget(self.still)
        self.statusBar().showMessage("Rendering frame…")
        def work():
            if not destination.exists():
                render(snapshot,snapshot.timeline,destination,cancel=cancel,frame=current,
                       progress=lambda pct: self.bridge.progress.emit(pct, "Frame"))
            return destination
        def ready(file,error):
            if cancel.is_set() or revision!=self.editor.revision:
                return
            if error:
                self.still.setPixmap(QPixmap())
                self.still.setText("Could not render this frame.\n"+str(error)[-240:])
                self.preview_stack.setCurrentWidget(self.still)
                self.statusBar().showMessage(str(error)[-500:],15000)
                self.hide_idle_progress()
                return
            pixmap=QPixmap(str(file))
            self.still.setText("")
            self.still.setPixmap(pixmap.scaled(self.still.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation))
            self.preview_stack.setCurrentWidget(self.still)
            self.statusBar().showMessage("Frame ready", 3000)
            self.bridge.progress.emit(100, "Frame")
        self.preview_future=self.task(work,ready)

    def toggle_play(self):
        if self.player.playbackState()==QMediaPlayer.PlayingState:
            self.player.pause()
            return
        if not duration(self.editor.timeline):
            return
        if self.preview_revision==self.editor.revision:
            self.preview_stack.setCurrentWidget(self.video)
            self.player.play()
            return
        self.preview_cancel.set()
        cancel=threading.Event()
        self.preview_cancel=cancel
        snapshot=frozen_editor(self.editor)
        revision=self.editor.revision
        self.preview_job_revision=revision
        destination=Path(self.cache.name)/f"preview-{revision}.mp4"
        self.statusBar().showMessage("Rendering composited playback — edits stay available")
        self.still.setPixmap(QPixmap())
        self.still.setText("Rendering playback…")
        self.preview_stack.setCurrentWidget(self.still)
        def work():
            if not destination.exists():
                render(snapshot,snapshot.timeline,destination,resolution="720p",bitrate=3,cancel=cancel,
                       progress=lambda pct: self.bridge.progress.emit(pct, "Playback"))
            return destination
        def ready(file,error):
            if cancel.is_set() or revision!=self.editor.revision:
                return
            if error:
                self.still.setPixmap(QPixmap())
                self.still.setText("Playback render failed.\n"+str(error)[-240:])
                self.preview_stack.setCurrentWidget(self.still)
                self.hide_idle_progress()
                self.act(lambda:(_ for _ in ()).throw(error))
                return
            self.preview_revision=revision
            self.player.setSource(QUrl.fromLocalFile(str(file)))
            self.player.setPosition(round(self.editor.current_frame/self.editor.timeline["fps"]*1000))
            self.preview_stack.setCurrentWidget(self.video)
            self.player.play()
            self.statusBar().showMessage("Composited playback")
            self.bridge.progress.emit(100, "Playback")
        self.task(work,ready)

    def sync_play_button(self, state):
        if hasattr(self, "play_button"):
            self.play_button.setText("Pause" if state == QMediaPlayer.PlayingState else "Play")

    def copy_timecode(self, event=None):
        QGuiApplication.clipboard().setText(self.timecode.text())
        self.statusBar().showMessage("Timecode copied", 2000)
        if event is not None:
            event.accept()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "timecode", None) and event.type() == QEvent.MouseButtonPress:
            self.copy_timecode(event)
            return True
        return super().eventFilter(obj, event)

    @Slot(QVideoFrame)
    def present_frame(self, frame):
        image=frame.toImage()
        if not image.isNull():
            self.video.setPixmap(QPixmap.fromImage(image).scaled(self.video.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation))

    @Slot(str, int)
    def control_playback(self,action,frame):
        if action=="seek":
            self.seek(frame)
        elif action=="pause":
            self.player.pause()
            self.preview_cancel.set()
        elif self.player.playbackState()!=QMediaPlayer.PlayingState:
            self.toggle_play()

    def play_position(self,milliseconds):
        self.editor.current_frame=round(milliseconds*self.editor.timeline["fps"]/1000)
        self.timeline.frame=self.editor.current_frame
        self.timeline.viewport().update()
        self.update_timecode()

    def update_timecode(self):
        fps=self.editor.timeline["fps"]
        seconds,frame=divmod(self.editor.current_frame,fps)
        minutes,seconds=divmod(seconds,60)
        hours,minutes=divmod(minutes,60)
        self.timecode.setText(f"{hours:02}:{minutes:02}:{seconds:02}:{frame:02}")

    def export_dialog(self):
        dialog=QDialog(self)
        dialog.setWindowTitle("Export video")
        layout=QVBoxLayout(dialog)
        form=QFormLayout()
        codec=QComboBox()
        codec.addItems(["H.264","H.265"])
        resolution=QComboBox()
        resolution.addItems(["Match Timeline","720p","1080p","2K","4K"])
        bitrate=QDoubleSpinBox()
        bitrate.setRange(.1,200)
        bitrate.setValue(8)
        hardware=QCheckBox("Try NVIDIA NVENC, then fall back to software")
        from .media import hw_encoders
        found = hw_encoders()
        hint = QLabel("Detected encoders: " + (", ".join(found) if found else "software only (libx264 / libx265)"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9AA3B5;")
        form.addRow("Codec",codec)
        form.addRow("Resolution",resolution)
        form.addRow("Bitrate (Mbps)",bitrate)
        layout.addLayout(form)
        layout.addWidget(hardware)
        layout.addWidget(hint)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            file,_=QFileDialog.getSaveFileName(self,"Export",str(Path.home()/"Downloads"/(self.editor.name+".mp4")),"MP4 (*.mp4)")
            if file:
                if not file.lower().endswith(".mp4"):
                    file += ".mp4"
                self.act(lambda:self.exports.start(dict(outputPath=file,codec=codec.currentText(),resolution=resolution.currentText(),bitrateMbps=bitrate.value(),hardware=hardware.isChecked())))

    def refresh_exports(self):
        jobs=self.exports.list()
        active=next((j for j in jobs if j["status"] in ("queued","running")),None)
        if active:
            self.progress_owner="Export"
            self.progress.show()
            self.progress.setValue(active["progress"])
            self.progress.setFormat(active["status"]+" %p%")
        elif jobs:
            latest=jobs[-1]
            if getattr(self,"reported_job",None)!=(latest["jobId"],latest["status"]):
                self.reported_job=(latest["jobId"],latest["status"])
                detail = latest.get("error") or latest["path"]
                self.statusBar().showMessage("Export "+latest["status"]+": "+detail,20000)
                if self.progress_owner=="Export":
                    self.progress.hide()
                    self.progress_owner=None

    def confirm_discard(self):
        if not self.editor.dirty:
            return True
        choice=QMessageBox.question(self,"Unsaved project","Discard unsaved changes? Save first using File → Save to keep them.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        return choice==QMessageBox.Yes

    def new_project(self):
        if self.confirm_discard():
            self.player.stop()
            self.preview_cancel.set()
            self.editor.new()

    def open_project(self):
        if not self.confirm_discard():
            return
        file=QFileDialog.getExistingDirectory(self,"Open .palmier package",str(Path.home()))
        if file:
            self.player.stop()
            self.preview_cancel.set()
            revision=self.editor.revision
            self.task(lambda:self.editor.load(file, expected_revision=revision),lambda result,error:self.act(lambda:(_ for _ in ()).throw(error)) if error else self.seek(0))

    def save_project(self,save_as=False):
        target=None
        if save_as or not self.editor.package:
            target,_=QFileDialog.getSaveFileName(self,"Save project",str(Path.home()/(self.editor.name+".palmier")),"Palmier package (*.palmier)")
            if not target:
                return
            if not target.endswith(".palmier"):
                target+=".palmier"
        self.task(lambda:self.editor.save(target),lambda result,error:self.act(lambda:(_ for _ in ()).throw(error)) if error else self.statusBar().showMessage("Saved "+result))

    def send_chat(self):
        prompt=self.chat_input.text().strip()
        if not prompt or not self.chat_send.isEnabled():
            return
        self.chat_input.clear()
        self.chat_log.append("You: "+html.escape(prompt))
        self.chat_send.setEnabled(False)
        self.chat_input.setEnabled(False)
        self.chat_send.setText("Sending…")
        model=self.model_input.text().strip() or "gpt-4.1-mini"
        def ready(result,error):
            self.chat_send.setEnabled(True)
            self.chat_input.setEnabled(True)
            self.chat_send.setText("Send · BYOK OpenAI")
            text = str(error or result)
            prefix = "Agent error: " if error else "Agent: "
            self.chat_log.append(prefix+html.escape(text))
            self.chat_input.setFocus()
        self.task(lambda:converse(prompt,self.tools,model),ready)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self,event):
        self.import_paths([u.toLocalFile() for u in event.mimeData().urls()])
        event.acceptProposedAction()

    def keyPressEvent(self,event):
        if isinstance(self.focusWidget(),(QLineEdit,QTextEdit,QSpinBox,QDoubleSpinBox)):
            super().keyPressEvent(event)
            return
        step=5 if event.modifiers()&Qt.ShiftModifier else 1
        if event.key() in (Qt.Key_Left,Qt.Key_Right):
            self.seek(self.editor.current_frame+step*(-1 if event.key()==Qt.Key_Left else 1))
        elif event.key()==Qt.Key_Up:
            self.jump_edit(-1)
        elif event.key()==Qt.Key_Down:
            self.jump_edit(1)
        elif event.key()==Qt.Key_Home:
            self.seek(0)
        elif event.key()==Qt.Key_End:
            total=duration(self.editor.timeline)
            self.seek(max(0, total-1) if total else 0)
        elif event.key()==Qt.Key_BracketLeft:
            self.trim_playhead(True)
        elif event.key()==Qt.Key_BracketRight:
            self.trim_playhead(False)
        else:
            super().keyPressEvent(event)

    def closeEvent(self,event):
        if not getattr(self,"smoke",False) and not self.confirm_discard():
            event.ignore()
            return
        self.closing=True
        self.preview_cancel.set()
        self.player.stop()
        self.player.setSource(QUrl())
        self.exports.stop()
        if self.mcp:
            self.mcp.stop()
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.cache.cleanup()
        event.accept()
