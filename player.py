"""Still: a frameless PySide6 / Qt6 local video player."""
import sys
import json
import hashlib
from pathlib import Path

from PySide6.QtCore import QEvent, QEasingCurve, QPropertyAnimation, QProcess, QLocale, QRect, QRectF, QSettings, QSizeF, Qt, QTimer, QUrl
from PySide6.QtGui import QActionGroup, QColor, QCursor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPalette, QRegion, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QGridLayout, QGraphicsOpacityEffect, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget,
)


from subtitles import SubtitleTrack, embedded_text
from single_instance import SingleInstance
from casting import CastManager

APP_VERSION = '0.1.5-casting'

def timestamp(ms):
    seconds = max(0, ms // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def apply_rounded_mask(widget, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(widget.rect()), radius, radius)
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


class RoundedMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        apply_rounded_mask(self, 10)


class RoundedDialog(QDialog):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#46505b'))
        painter.setBrush(QColor('#1b2028'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        apply_rounded_mask(self, 12)


class SubtitleLabel(QLabel):
    """Readable subtitle text with an outline, without a background panel."""
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self.font())
        area = QRectF(self.rect().adjusted(12, 6, -12, -6))
        flags = int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap)
        painter.setPen(QColor(0, 0, 0, 230))
        for x, y in ((-2, 0), (2, 0), (0, -2), (0, 2),
                     (-1, -1), (1, -1), (-1, 1), (1, 1)):
            painter.drawText(area.translated(x, y), flags, self.text())
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(area, flags, self.text())


class VideoView(QGraphicsView):
    """Handle file drops at the player level rather than inside the scene."""
    def dragEnterEvent(self, event):
        self.window().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        self.window().dragMoveEvent(event)

    def dropEvent(self, event):
        self.window().dropEvent(event)


class SeekSlider(QSlider):
    """Click the groove to seek immediately; keep native handle dragging."""
    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._groove_drag = False

    def value_at(self, point):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider,
            option, QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider,
            option, QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        pixel = round(point.x() - groove.left() - handle.width() / 2)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
            max(0, min(span, pixel)), span, option.upsideDown)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider,
                option, QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                self._groove_drag = True
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.setSliderDown(True)
                self.setSliderPosition(self.value_at(event.position()))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._groove_drag:
            self.setSliderPosition(self.value_at(event.position()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._groove_drag and event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(self.value_at(event.position()))
            self._groove_drag = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ResizeHandle(QWidget):
    """Transparent window border with native resize and a manual fallback."""
    def __init__(self, parent, edges, cursor):
        super().__init__(parent)
        self.edges = edges
        self._drag = None
        self.setMouseTracking(True)
        self.setCursor(cursor)
        self.setStyleSheet('background: transparent;')
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            window.click_timer.stop()
            native = window.windowHandle()
            if not native or not native.startSystemResize(self.edges):
                self._drag = (event.globalPosition().toPoint(), window.geometry())
            event.accept()
            return
        super().mousePressEvent(event)

    def drag_geometry(self, point):
        origin, initial = self._drag
        delta = point - origin
        window = self.window()
        left, top = initial.x(), initial.y()
        right, bottom = left + initial.width(), top + initial.height()
        if self.edges & Qt.Edge.LeftEdge:
            left = max(right - window.maximumWidth(), min(right - window.minimumWidth(), left + delta.x()))
        if self.edges & Qt.Edge.RightEdge:
            right = max(left + window.minimumWidth(), min(left + window.maximumWidth(), right + delta.x()))
        if self.edges & Qt.Edge.TopEdge:
            top = max(bottom - window.maximumHeight(), min(bottom - window.minimumHeight(), top + delta.y()))
        if self.edges & Qt.Edge.BottomEdge:
            bottom = max(top + window.minimumHeight(), min(top + window.maximumHeight(), bottom + delta.y()))
        from PySide6.QtCore import QRect
        return QRect(left, top, right - left, bottom - top)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.window().setGeometry(self.drag_geometry(event.globalPosition().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Player(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Still Casting — Qt Video Player')
        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Still', 'Still-Casting')
        self.remember_position = self.settings.value('playback/remember', True, type=bool)
        self._resume_key = None
        self._resume_position = None
        self.cast_manager = CastManager(self)
        self.cast_dialog = None
        self.cast_manager.started.connect(self.cast_started)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor('#080a0e'))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self._initial_reveal_pending = False
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self.reveal_prepared_window)
        self._quiet_file_start = False
        self._quiet_start_cursor = None
        self._quiet_pointer_left = False
        self._waiting_for_video_frame = False
        self._fit_video_on_load = False
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.resize(1100, 680)
        self.setMinimumSize(520, 320)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self._scrubbing = False
        self._resume_after_seek = False
        self._preview_position = None
        self.scrub_timer = QTimer(self)
        self.scrub_timer.setSingleShot(True)
        self.scrub_timer.setInterval(70)
        self.scrub_timer.timeout.connect(self.preview_seek)
        self._duration = 0
        self._startup_refresh_started = False
        self._startup_refresh_timers = []
        self._normal_geometry = None
        self.window_resize = QPropertyAnimation(self, b'geometry', self)
        self.window_resize.setDuration(350)
        self.window_resize.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._ignore_video_release = False
        self._window_drag = None
        self._window_drag_started = False
        self._window_drag_native = False
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.toggle_play)
        self.subtitle_track = None
        self.subtitle_path = None
        self.subtitles_enabled = False
        self.subtitle_source = None
        self.embedded_index = -1
        self.embedded_subtitle_text = ''
        self._forced_flags = None
        self._subtitle_manual = False
        self._forced_auto_applied = False
        self._probe_generation = 0
        self._forced_probe = None

        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.media.setAudioOutput(self.audio)
        self.scene = QGraphicsScene(self)
        self.video = QGraphicsVideoItem()
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.scene.addItem(self.video)
        self.view = VideoView(self.scene, self)
        self.view.setAcceptDrops(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setStyleSheet('background: #080a0e; border: none;')
        self.view.setBackgroundBrush(QColor('#080a0e'))
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        # Forward frames only; subtitles are rendered once by our OSD-aware overlay.
        self.media_sink = QVideoSink(self)
        self.media_sink.videoFrameChanged.connect(self.present_video_frame)
        self.media.setVideoSink(self.media_sink)

        self.empty = QWidget(self)
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand = QLabel('S T I L L', self.empty)
        self.splash_brand = brand
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setStyleSheet('color: #c9e7b5; font-size: 14px; padding: 20px;')
        empty_layout.addWidget(brand)
        heading = QLabel('Just you and the picture.', self.empty)
        heading.setStyleSheet('font-size: 30px; color: #eef1f6;')
        empty_layout.addWidget(heading)
        hint = QLabel('Drop a video anywhere to begin.', self.empty)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet('color: #89939f; padding: 14px;')
        empty_layout.addWidget(hint)
        choose = QPushButton('Open a video', self.empty)
        self.splash_open = choose
        choose.clicked.connect(self.choose_file)
        empty_layout.addWidget(choose, alignment=Qt.AlignmentFlag.AlignCenter)
        self.splash_credit = QLabel('An App by Pags', self.empty)
        self.splash_credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.splash_credit.setStyleSheet(brand.styleSheet())
        self.splash_credit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.splash_codex_credit = QLabel('Built with OpenAI Codex', self.empty)
        self.splash_codex_credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.splash_codex_credit.setStyleSheet('color: #c9e7b5; font-size: 11px; padding: 0;')
        self.splash_codex_credit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.loading_cover = QFrame(self)
        self.loading_cover.setStyleSheet('background: #080a0e; border: none;')
        self.loading_cover.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.loading_cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.loading_cover.hide()

        self.top = QFrame(self)
        self.top.setObjectName('osd')
        top_layout = QHBoxLayout(self.top)
        self.title = QLabel('STILL', self.top)
        self.title.setMouseTracking(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.button(top_layout, '×', 'Close', self.close)
        self.button(top_layout, '−', 'Minimize', self.showMinimized)
        self.button(top_layout, '＋', 'Open video (O)', self.choose_file)
        top_layout.addWidget(self.title, 1)

        self.controls = QFrame(self)
        self.controls.setObjectName('osd')
        controls_layout = QVBoxLayout(self.controls)
        self.seek = SeekSlider(self.controls)
        self.seek.setRange(0, 10000)
        self.seek.setTracking(True)
        self.seek.setEnabled(False)
        self.seek.setAccessibleName('Seek')
        self.seek.sliderPressed.connect(self.begin_seek)
        self.seek.sliderReleased.connect(self.end_seek)
        self.seek.valueChanged.connect(self.seek_changed)
        controls_layout.addWidget(self.seek)
        row = QHBoxLayout()
        self.play_button = self.button(row, '▶', 'Play / pause (Space)', self.toggle_play)
        self.time = QLabel('0:00 / 0:00', self.controls)
        row.addWidget(self.time)
        row.addStretch()
        self.menu_button = self.button(row, '☰', 'Menu', self.player_menu)
        self.shortcuts_dialog = None
        self.mute_button = self.button(row, 'Mute', 'Mute (M)', self.toggle_mute)
        self.volume = QSlider(Qt.Orientation.Horizontal, self.controls)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.setAccessibleName('Volume')
        self.volume.valueChanged.connect(self.set_volume)
        row.addWidget(self.volume)
        self.button(row, '⛶', 'Fullscreen (F)', self.toggle_fullscreen)
        controls_layout.addLayout(row)
        self.subtitle_label = SubtitleLabel(self)
        self.subtitle_label.setTextFormat(Qt.TextFormat.PlainText)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.subtitle_label.setStyleSheet('background: transparent; color: white; font-size: 24px; font-weight: 600; padding: 6px 12px;')
        self.subtitle_label.hide()
        self.playback_feedback = SubtitleLabel(self)
        self.playback_feedback.setStyleSheet('background: transparent; color: white; font-size: 22px; font-weight: 600; padding: 6px 12px;')
        self.playback_feedback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playback_feedback.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.playback_feedback.hide()
        self.feedback_opacity = QGraphicsOpacityEffect(self.playback_feedback)
        self.feedback_opacity.setOpacity(1.0)
        self.feedback_opacity.setEnabled(False)
        self.playback_feedback.setGraphicsEffect(self.feedback_opacity)
        self.feedback_fade = QPropertyAnimation(self.feedback_opacity, b'opacity', self)
        self.feedback_fade.setDuration(700)
        self.feedback_fade.setStartValue(1.0)
        self.feedback_fade.setEndValue(0.0)
        self.feedback_fade.finished.connect(self.finish_playback_feedback)
        self.feedback_hold = QTimer(self)
        self.feedback_hold.setSingleShot(True)
        self.feedback_hold.setTimerType(Qt.TimerType.PreciseTimer)
        self.feedback_hold.setInterval(1000)
        self.feedback_hold.timeout.connect(self.fade_playback_feedback)
        E = Qt.Edge
        C = Qt.CursorShape
        self.resize_handles = [
            ResizeHandle(self, E.TopEdge, C.SizeVerCursor),
            ResizeHandle(self, E.BottomEdge, C.SizeVerCursor),
            ResizeHandle(self, E.LeftEdge, C.SizeHorCursor),
            ResizeHandle(self, E.RightEdge, C.SizeHorCursor),
            ResizeHandle(self, E.TopEdge | E.LeftEdge, C.SizeFDiagCursor),
            ResizeHandle(self, E.TopEdge | E.RightEdge, C.SizeBDiagCursor),
            ResizeHandle(self, E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor),
            ResizeHandle(self, E.BottomEdge | E.RightEdge, C.SizeFDiagCursor),
        ]
        self._osd_fades = []
        for overlay in (self.top, self.controls):
            effect = QGraphicsOpacityEffect(overlay)
            effect.setOpacity(1.0)
            effect.setEnabled(False)
            overlay.setGraphicsEffect(effect)
            fade = QPropertyAnimation(effect, b'opacity', self)
            fade.setDuration(800)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            fade.finished.connect(lambda widget=overlay: self.finish_fade(widget))
            self._osd_fades.append((overlay, effect, fade))
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setStyleSheet('background: #242b33; color: #eef1f6; padding: 12px; border-radius: 8px;')
        self.notice.hide()
        self.notice_timer = QTimer(self)
        self.notice_timer.setSingleShot(True)
        self.notice_timer.timeout.connect(self.notice.hide)
        self.leave_timer = QTimer(self)
        self.leave_timer.setSingleShot(True)
        self.leave_timer.setInterval(1000)
        self.leave_timer.timeout.connect(lambda: self.hide_osd(leaving=True))
        self.idle = QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.setInterval(2400)
        self.idle.timeout.connect(self.hide_osd)

        self.media_sink.subtitleTextChanged.connect(self.embedded_subtitle_changed)
        self.media.tracksChanged.connect(self.subtitle_tracks_changed)
        self.media.positionChanged.connect(self.position_changed)
        self.media.durationChanged.connect(self.duration_changed)
        self.media.seekableChanged.connect(self.seek.setEnabled)
        self.media.seekableChanged.connect(lambda _: self.restore_playback_position())
        self.media.mediaStatusChanged.connect(lambda _: self.restore_playback_position())
        self.media.playbackStateChanged.connect(self.playback_changed)
        self.media.errorOccurred.connect(lambda error, message: self.message(message))
        self.audio.mutedChanged.connect(lambda muted: self.mute_button.setText('Unmute' if muted else 'Mute'))
        self.shortcut_bindings = [
            ('Space', 'Play / pause', self.toggle_play),
            ('O', 'Open video', self.choose_file),
            ('F', 'Toggle fullscreen', self.toggle_fullscreen),
            ('Escape', 'Exit fullscreen', self.exit_fullscreen),
            ('M', 'Mute / unmute', self.toggle_mute),
            ('Left', 'Back 10 seconds', lambda: self.skip(-10000)),
            ('Right', 'Forward 10 seconds', lambda: self.skip(10000)),
            ('Up', 'Volume up 5%', lambda: self.volume.setValue(min(100, self.volume.value() + 5))),
            ('Down', 'Volume down 5%', lambda: self.volume.setValue(max(0, self.volume.value() - 5))),
            ('V', 'Toggle subtitles', self.toggle_subtitles),
            ('Ctrl+Shift+S', 'Load subtitles', self.choose_subtitles),
            ('F1', 'Keyboard shortcuts', self.show_shortcuts),
            ('Ctrl+Q', 'Quit Still', self.close),
        ]
        for key, description, action in self.shortcut_bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(action)
        self.setStyleSheet('''
            QWidget { background: #080a0e; color: #eef1f6; font-family: sans-serif; font-size: 12px; }
            QFrame#osd { background: #1b2028; border: 1px solid #303842; border-radius: 12px; }
            QLabel { background: transparent; }
            QMenu { background: #1b2028; color: #eef1f6; border: 1px solid #46505b; border-radius: 10px; padding: 6px; }
            QMenu::item { padding: 8px 20px; margin: 2px; border-radius: 5px; background: transparent; }
            QMenu::item:selected:enabled { background: #394650; color: #ffffff; }
            QMenu::item:disabled { color: #89939f; }
            QMenu::separator { height: 1px; background: #46505b; margin: 5px 8px; }
            QPushButton { background: #263129; color: #c9e7b5; border: none; border-radius: 7px; padding: 9px 13px; }
            QPushButton:hover { background: #3a4740; }
            QPushButton:focus { border: 1px solid #c9e7b5; }
            QSlider { background: transparent; }
            QSlider::groove:horizontal { height: 4px; background: #46505b; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #c9e7b5; border-radius: 2px; }
            QSlider::handle:horizontal { background: #c9e7b5; width: 12px; margin: -4px 0; border-radius: 6px; }
        ''')
        QApplication.instance().installEventFilter(self)
        self.top.hide()
        self.controls.hide()

    def button(self, layout, text, tooltip, action):
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def choose_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Open video', '',
            'Video files (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.mpeg *.mpg *.ts);;All files (*)')
        if filename:
            self.open_file(filename)

    def open_file(self, filename, quiet_start=False):
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            self.message('The selected file does not exist.')
            return
        # Only closeEvent writes history; switching files does not save it.
        self._resume_key = hashlib.sha256(str(path).encode('utf-8')).hexdigest()
        self._resume_position = None
        self.window_resize.stop()
        if quiet_start:
            self._quiet_file_start = True
            self._quiet_start_cursor = QCursor.pos()
            self._quiet_pointer_left = False
            self.idle.stop()
            self.leave_timer.stop()
            for overlay, effect, fade in self._osd_fades:
                fade.stop()
                overlay.hide()
        self._probe_generation += 1
        self._forced_flags = None
        self._subtitle_manual = False
        self._forced_auto_applied = False
        if self._forced_probe is not None:
            self._forced_probe.kill()
            self._forced_probe = None
        self.subtitles_enabled = False
        self.subtitle_source = None
        self.embedded_index = -1
        self.embedded_subtitle_text = ''
        self.media.setActiveSubtitleTrack(-1)
        self.subtitle_track = None
        self.subtitle_path = None
        self.subtitle_label.clear()
        self.subtitle_label.hide()
        self.feedback_hold.stop()
        self.feedback_fade.stop()
        self.playback_feedback.hide()
        self.update_subtitle_button()
        self.scrub_timer.stop()
        self._scrubbing = False
        self._resume_after_seek = False
        self._preview_position = None
        self.notice.hide()
        self._waiting_for_video_frame = False
        self.media.stop()
        self._fit_video_on_load = True
        self.loading_cover.setGeometry(self.rect())
        self.loading_cover.show()
        self.loading_cover.raise_()
        self.empty.hide()
        self._waiting_for_video_frame = True
        self.title.setText(path.name)
        self._resume_position = (self.settings.value(f'positions/{self._resume_key}', 0, type=int)
                                 if self.remember_position else None)
        self.media.setSource(QUrl.fromLocalFile(str(path)))
        self.media.play()
        self.start_forced_probe(path)
        self.show_osd()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor('#080a0e'))

    def present_video_frame(self, frame):
        self.video.videoSink().setVideoFrame(frame)
        if self._fit_video_on_load and frame.isValid():
            self.fit_window_to_video()
        if self._waiting_for_video_frame and frame.isValid():
            # Paint the decoded frame underneath before uncovering the viewport.
            self.view.viewport().repaint()
            self._waiting_for_video_frame = False
            self.loading_cover.hide()

    def fit_window_to_video(self):
        # Qt's native size accounts for the video's display proportions, rather
        # than assuming the encoded pixels are square. Fit once per file so
        # subsequent frames do not undo the user's manual window resizing.
        native = self.video.nativeSize()
        if native.width() <= 0 or native.height() <= 0:
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        previous = (self._normal_geometry if self.isFullScreen()
                    and self._normal_geometry else self.normalGeometry()
                    if self.isMaximized() else self.geometry())
        ratio = native.width() / native.height()
        width = max(previous.width(), 520, 320 * ratio)
        height = width / ratio
        scale = min(1.0, available.width() / width, available.height() / height)
        size = QSizeF(width * scale, height * scale).toSize()
        target = QRect(previous)
        target.setSize(size)
        target.moveCenter(previous.center())
        target.moveLeft(max(available.left(), min(target.left(), available.right() - size.width() + 1)))
        target.moveTop(max(available.top(), min(target.top(), available.bottom() - size.height() + 1)))
        # Unusual portrait/widescreen files may need a smaller minimum in one
        # dimension to fit the display without introducing letterboxing.
        self.setMinimumSize(min(520, size.width()), min(320, size.height()))
        self._fit_video_on_load = False
        if self.isFullScreen():
            self._normal_geometry = target
        else:
            if self.isMaximized():
                self.showNormal()
            self.window_resize.stop()
            self.window_resize.setStartValue(self.geometry())
            self.window_resize.setEndValue(target)
            self.window_resize.start()

    def open_from_another_launch(self, filename):
        if filename:
            self.open_file(filename, quiet_start=True)
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        if self.windowHandle():
            self.windowHandle().requestActivate()

    def create_subtitle_menu(self, parent=None):
        menu = RoundedMenu(self if parent is None else parent)
        off = menu.addAction('Off')
        off.setCheckable(True)
        off.setChecked(not self.subtitles_enabled)
        off.triggered.connect(lambda: self.set_subtitles_enabled(False))
        tracks = self.media.subtitleTracks()
        if tracks:
            menu.addSection('Built-in subtitles')
            languages = []
            for metadata in tracks:
                language = metadata.value(QMediaMetaData.Key.Language)
                languages.append(QLocale.languageToString(language)
                    if isinstance(language, QLocale.Language) and language != QLocale.Language.AnyLanguage else '')
            for index, language in enumerate(languages):
                label = language or f'Track {index + 1}'
                if language and languages.count(language) > 1:
                    label += f' (track {index + 1})'
                forced = bool(self._forced_flags and index < len(self._forced_flags) and self._forced_flags[index]['forced'])
                if forced:
                    label += ' (forced)'
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.subtitles_enabled and self.subtitle_source == 'embedded' and self.embedded_index == index)
                action.triggered.connect(lambda checked=False, track=index: self.select_embedded_subtitle(track))
        if self.subtitle_track is not None:
            menu.addSection('External subtitles')
            external = menu.addAction(self.subtitle_path.name)
            external.setCheckable(True)
            external.setChecked(self.subtitles_enabled and self.subtitle_source == 'external')
            external.triggered.connect(self.select_external_subtitle)
        menu.addSeparator()
        load = menu.addAction('Load subtitles…')
        load.setEnabled(not self.media.source().isEmpty())
        load.triggered.connect(self.choose_subtitles)
        return menu

    def create_audio_menu(self, parent):
        menu = RoundedMenu(parent)
        menu.setTitle('Audio track')
        group = QActionGroup(menu)
        group.setExclusive(True)
        tracks = self.media.audioTracks()
        menu.setEnabled(bool(tracks))
        source = self.media.source()
        labels = []
        for metadata in tracks:
            language = metadata.value(QMediaMetaData.Key.Language)
            language = (QLocale.languageToString(language)
                        if isinstance(language, QLocale.Language)
                        and language != QLocale.Language.AnyLanguage else '')
            title = metadata.stringValue(QMediaMetaData.Key.Title).strip()
            labels.append(' — '.join(part for part in (language, title) if part))
        for index, label in enumerate(labels):
            if not label:
                label = f'Track {index + 1}'
            elif labels.count(label) > 1:
                label += f' (track {index + 1})'
            action = menu.addAction(label.replace('&', '&&'))
            action.setCheckable(True)
            action.setChecked(index == self.media.activeAudioTrack())
            group.addAction(action)
            action.triggered.connect(lambda checked=False, track=index:
                self.select_audio_track(track, source))
        return menu

    def select_audio_track(self, index, source):
        # A second launch may open another file while the menu is still open.
        if source == self.media.source() and 0 <= index < len(self.media.audioTracks()):
            self.media.setActiveAudioTrack(index)

    def create_player_menu(self):
        menu = RoundedMenu(self)
        subtitles = self.create_subtitle_menu(menu)
        subtitles.setTitle('Subtitles')
        menu.addMenu(subtitles)
        menu.addMenu(self.create_audio_menu(menu))
        cast = menu.addAction('Cast…')
        cast.triggered.connect(self.show_cast_dialog)
        remember = menu.addAction('Remember playback position')
        remember.setCheckable(True)
        remember.setChecked(self.remember_position)
        remember.toggled.connect(self.set_remember_position)
        shortcuts = menu.addAction('Keyboard shortcuts')
        shortcuts.setShortcut(QKeySequence('F1'))
        shortcuts.triggered.connect(self.show_shortcuts)
        menu.addSeparator()
        about = menu.addAction('About Still')
        about.triggered.connect(self.show_about)
        return menu

    def cast_started(self, name, filename):
        if self.media.source().toLocalFile() != filename:
            self.cast_manager.command('stop')
            return
        self.media.pause()
        self.show_playback_feedback(f'Casting to {name}')

    def show_cast_dialog(self):
        if self.cast_dialog is None:
            dialog = RoundedDialog(self, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dialog.setWindowTitle('Still — Cast to TV')
            dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            dialog.setObjectName('castPanel')
            dialog.setStyleSheet('''
                QDialog#castPanel { background: transparent; }
                QDialog#castPanel QPushButton { padding: 6px 9px; }
                QDialog#castPanel QListWidget { background: #11161c; border: 1px solid #303842; border-radius: 8px; }
                QDialog#castPanel QListWidget::item { padding: 6px; }
                QDialog#castPanel QListWidget::item:selected { background: #394650; color: white; }
            ''')
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)
            header = QHBoxLayout()
            heading = QLabel('Cast to TV', dialog)
            heading.setStyleSheet('color: #c9e7b5; font-size: 16px;')
            header.addWidget(heading, 1)
            close = QPushButton('×', dialog)
            close.setToolTip('Close panel')
            close.clicked.connect(dialog.hide)
            header.addWidget(close)
            layout.addLayout(header)
            hint = QLabel('TVs on your network', dialog)
            hint.setToolTip('The TV must support the video format. Subtitles and audio-track selection stay on the local player.')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            devices = QListWidget(dialog)
            devices.setMinimumHeight(64)
            layout.addWidget(devices, 1)
            status = QLabel('Searching for TVs…', dialog)
            status.setWordWrap(True)
            status.setFixedHeight(64)
            layout.addWidget(status)
            row = QHBoxLayout()
            refresh = QPushButton('Refresh', dialog)
            start = QPushButton('Start casting', dialog)
            row.addWidget(refresh)
            row.addWidget(start)
            layout.addLayout(row)
            controls = QHBoxLayout()
            cast_controls = []
            for label, command in (('Play', 'play'), ('Pause', 'pause'), ('Stop casting', 'stop')):
                button = QPushButton(label, dialog)
                button.setEnabled(False)
                cast_controls.append(button)
                button.clicked.connect(lambda checked=False, cmd=command: self.cast_manager.command(cmd))
                controls.addWidget(button)
            layout.addLayout(controls)

            def found(entries):
                selected = devices.currentItem()
                selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
                devices.clear()
                selected_row = 0
                for identifier, name in entries:
                    if identifier == selected_id:
                        selected_row = devices.count()
                    item = QListWidgetItem(name)
                    item.setData(Qt.ItemDataRole.UserRole, identifier)
                    devices.addItem(item)
                if entries:
                    devices.setCurrentRow(selected_row)
                status.setText(f'Found {len(entries)} device(s). Select your TV, then press Start casting.' if entries else
                    'No TVs found. Check the TV is on, casting or media sharing is enabled, and both devices are on the same network.')

            def cast_video():
                item = devices.currentItem()
                filename = self.media.source().toLocalFile()
                if item is None or not filename:
                    status.setText('Load a video and select a TV first.')
                    return
                status.setText('Connecting to TV…')
                self.cast_manager.start(item.data(Qt.ItemDataRole.UserRole), filename, self.media.position())

            self.cast_manager.devices_found.connect(found)
            self.cast_manager.status.connect(status.setText)
            self.cast_manager.failed.connect(lambda error: status.setText(f'Could not cast: {error}'))
            self.cast_manager.busy.connect(lambda busy: (refresh.setEnabled(not busy), start.setEnabled(not busy)))
            self.cast_manager.active_changed.connect(lambda active: [button.setEnabled(active) for button in cast_controls])
            refresh.clicked.connect(self.cast_manager.discover)
            start.clicked.connect(cast_video)
            dialog.setFixedSize(300, 340)
            self.cast_dialog = dialog
            self.cast_manager.discover()
        self.cast_dialog.show()
        self.position_cast_panel()
        self.cast_dialog.raise_()
        self.cast_dialog.activateWindow()

    def position_cast_panel(self):
        panel = getattr(self, 'cast_dialog', None)
        if panel is None or not panel.isVisible():
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        main = self.frameGeometry()
        x = main.right() + 9
        if x + panel.width() > available.right() + 1:
            # Prefer the other side to covering the picture on smaller displays.
            x = main.left() - panel.width() - 8
        x = max(available.left(), min(x, available.right() - panel.width() + 1))
        y = max(available.top(), min(main.top(), available.bottom() - panel.height() + 1))
        panel.move(x, y)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.position_cast_panel()

    def show_about(self):
        dialog = RoundedDialog(self, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        dialog.setWindowTitle('About Still')
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        for text, style in (
                ('STILL', 'font-size: 24px; color: #c9e7b5;'),
                (f'Version {APP_VERSION}', 'font-size: 14px; color: #eef1f6;'),
                ('A minimalist video player', 'color: #89939f;'),
                ('An App by Pags', 'color: #c9e7b5;'),
                ('Built with OpenAI Codex', 'font-size: 11px; color: #c9e7b5;')):
            label = QLabel(text, dialog)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(style)
            layout.addWidget(label)
        close = QPushButton('Close', dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignCenter)
        dialog.setFixedWidth(340)
        dialog.adjustSize()
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.exec()
        dialog.deleteLater()

    def set_remember_position(self, enabled):
        self.remember_position = enabled
        if not enabled:
            self._resume_position = None

    def restore_playback_position(self):
        if self._resume_position is None or not self.media.isSeekable():
            return
        if self.media.mediaStatus() not in (QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            return
        duration = self.media.duration()
        if duration <= 0:
            return
        position = self._resume_position
        self._resume_position = None
        if 0 < position < max(0, duration - 30000):
            self.media.setPosition(position)

    def save_playback_position(self):
        self.settings.setValue('playback/remember', self.remember_position)
        if self.remember_position and self._resume_key is not None and self.media.duration() > 0:
            position = self.media.position()
            key = f'positions/{self._resume_key}'
            if self._resume_position is not None:
                # Closing before loading finishes must not erase existing history.
                pass
            elif 0 < position < max(0, self.media.duration() - 30000):
                self.settings.setValue(key, position)
            else:
                self.settings.remove(key)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            print('Still could not save playback settings.', file=sys.stderr)

    def show_shortcuts(self):
        if self.shortcuts_dialog is None:
            dialog = RoundedDialog(self, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            dialog.setWindowTitle('Still — Keyboard shortcuts')
            dialog.setObjectName('shortcutPanel')
            dialog.setStyleSheet('QDialog#shortcutPanel { background: #1b2028; border: 1px solid #46505b; border-radius: 12px; }')
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(16)
            header = QHBoxLayout()
            title = QLabel('Keyboard shortcuts', dialog)
            title.setStyleSheet('font-size: 18px; color: #c9e7b5;')
            header.addWidget(title, 1)
            self.button(header, '×', 'Close shortcuts', dialog.close)
            layout.addLayout(header)
            grid = QGridLayout()
            grid.setHorizontalSpacing(32)
            grid.setVerticalSpacing(9)
            display = {'Escape': 'Esc', 'Left': '←', 'Right': '→', 'Up': '↑', 'Down': '↓'}
            for row, (key, description, action) in enumerate(self.shortcut_bindings):
                name = QLabel(description, dialog)
                key_label = QLabel(display.get(key, QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)), dialog)
                key_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                key_label.setStyleSheet('color: #c9e7b5;')
                grid.addWidget(name, row, 0)
                grid.addWidget(key_label, row, 1)
            layout.addLayout(grid)
            hint = QLabel('Press Esc to close this panel', dialog)
            hint.setStyleSheet('color: #89939f; font-size: 11px;')
            layout.addWidget(hint)
            dialog.setFixedWidth(420)
            self.shortcuts_dialog = dialog
        dialog = self.shortcuts_dialog
        dialog.adjustSize()
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def player_menu(self):
        self.show_osd()
        self.idle.stop()
        self.leave_timer.stop()
        menu = self.create_player_menu()
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().topLeft()))
        menu.deleteLater()
        if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.show_osd()
        else:
            self.leave_timer.start()

    def start_forced_probe(self, path):
        process = QProcess(self)
        self._forced_probe = process
        generation = self._probe_generation
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.setInterval(8000)
        timer.timeout.connect(process.kill)
        process.finished.connect(timer.stop)
        process.finished.connect(lambda code, status: self.forced_probe_finished(process, generation, code))
        process.errorOccurred.connect(lambda error: timer.stop())
        args = ['--probe-forced', str(path)]
        if not getattr(sys, 'frozen', False):
            args.insert(0, str(Path(__file__).resolve()))
        process.start(sys.executable, args)
        timer.start()

    def forced_probe_finished(self, process, generation, code):
        if self._forced_probe is process:
            self._forced_probe = None
        if generation == self._probe_generation and code == 0:
            try:
                flags = json.loads(bytes(process.readAllStandardOutput()).decode('utf-8'))
                if isinstance(flags, list) and all(isinstance(item, dict) and isinstance(item.get('forced'), bool) for item in flags):
                    self._forced_flags = flags
                    self.apply_forced_subtitles()
            except (ValueError, UnicodeError):
                pass
        process.deleteLater()

    def apply_forced_subtitles(self):
        tracks = self.media.subtitleTracks()
        if self._subtitle_manual or self._forced_auto_applied or self._forced_flags is None:
            return
        # Both Qt/FFmpeg and PyAV list subtitle streams in container order.
        # Refuse uncertain mappings rather than enabling an unrelated track.
        if not tracks or len(tracks) != len(self._forced_flags):
            return
        text_codecs = {'srt', 'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text', 'ttml'}
        forced = [index for index, flags in enumerate(self._forced_flags)
                  if flags['forced'] and flags.get('codec') in text_codecs]
        if not forced:
            return
        audio_tracks = self.media.audioTracks()
        audio_index = self.media.activeAudioTrack()
        language = (audio_tracks[audio_index].value(QMediaMetaData.Key.Language)
                    if 0 <= audio_index < len(audio_tracks) else QLocale().language())
        matching = [index for index in forced
                    if tracks[index].value(QMediaMetaData.Key.Language) == language]
        self._forced_auto_applied = True
        self.select_embedded_subtitle((matching or forced)[0], automatic=True)

    def subtitle_tracks_changed(self):
        if self.embedded_index >= len(self.media.subtitleTracks()):
            self.embedded_index = -1
            self.embedded_subtitle_text = ''
            if self.subtitle_source == 'embedded':
                self.subtitle_source = None
                self.subtitles_enabled = False
        self.apply_forced_subtitles()
        self.update_subtitle_button()
        self.update_subtitles(self.media.position())

    def select_embedded_subtitle(self, index, automatic=False):
        if not 0 <= index < len(self.media.subtitleTracks()):
            return
        if not automatic:
            self._subtitle_manual = True
        self.subtitle_source = 'embedded'
        self.embedded_index = index
        self.embedded_subtitle_text = ''
        self.set_subtitles_enabled(True, manual=not automatic)

    def select_external_subtitle(self):
        if self.subtitle_track is not None:
            self.subtitle_source = 'external'
            self.set_subtitles_enabled(True)

    def embedded_subtitle_changed(self, text):
        if self.subtitle_source == 'embedded' and self.subtitles_enabled:
            self.embedded_subtitle_text = embedded_text(text)
            self.update_subtitles(self.media.position())

    def choose_subtitles(self):
        if self.media.source().isEmpty():
            self.message('Open a video before loading subtitles.')
            return
        source = Path(self.media.source().toLocalFile())
        filename, _ = QFileDialog.getOpenFileName(self, 'Load subtitles', str(source.parent),
            'Subtitles (*.srt *.vtt);;All files (*)')
        if filename:
            self.load_subtitles(filename)

    def load_subtitles(self, filename):
        try:
            track = SubtitleTrack.load(filename)
        except (OSError, UnicodeError, ValueError) as error:
            self.message(f'Could not load subtitles: {error}')
            return False
        self.subtitle_track = track
        self.subtitle_path = Path(filename)
        self.select_external_subtitle()
        return True

    def toggle_subtitles(self):
        if not self.subtitles_enabled and self.subtitle_source is None:
            if self.media.subtitleTracks():
                self.select_embedded_subtitle(0)
            elif self.subtitle_track is not None:
                self.select_external_subtitle()
            else:
                self.message('No built-in subtitles. Use Menu → Subtitles → Load subtitles for an SRT or VTT file.')
                return
        else:
            self.set_subtitles_enabled(not self.subtitles_enabled)

    def show_subtitle_feedback(self):
        self.show_playback_feedback('Subtitles ON' if self.subtitles_enabled else 'Subtitles OFF')

    def show_playback_feedback(self, text):
        self.feedback_hold.stop()
        self.feedback_fade.stop()
        self.feedback_opacity.setOpacity(1.0)
        self.feedback_opacity.setEnabled(False)
        self.playback_feedback.setText(text)
        self.playback_feedback.show()
        self.layout_subtitles()
        self.playback_feedback.raise_()
        self.feedback_hold.start()

    def fade_playback_feedback(self):
        self.feedback_opacity.setEnabled(True)
        self.feedback_fade.start()

    def finish_playback_feedback(self):
        self.playback_feedback.hide()
        self.feedback_opacity.setEnabled(False)
        self.layout_subtitles()

    def set_subtitles_enabled(self, enabled, manual=True):
        if manual:
            self._subtitle_manual = True
        self.subtitles_enabled = bool(enabled) and self.subtitle_source is not None
        native_index = self.embedded_index if self.subtitles_enabled and self.subtitle_source == 'embedded' else -1
        if not self.subtitles_enabled or self.subtitle_source != 'embedded':
            self.embedded_subtitle_text = ''
        self.media.setActiveSubtitleTrack(native_index)
        self.update_subtitle_button()
        self.update_subtitles(self.media.position())
        if manual:
            self.show_subtitle_feedback()

    def update_subtitle_button(self):
        active = self.subtitles_enabled and self.subtitle_source is not None
        self.menu_button.setToolTip('Menu')

    def update_subtitles(self, position):
        text = ''
        if self.subtitles_enabled:
            if self.subtitle_source == 'external' and self.subtitle_track is not None:
                text = self.subtitle_track.text_at(position)
            elif self.subtitle_source == 'embedded':
                text = self.embedded_subtitle_text
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))
        if text:
            self.layout_subtitles()
            self.subtitle_label.raise_()

    def layout_subtitles(self):
        width = max(200, self.width() - 80)
        self.subtitle_label.setFixedWidth(width)
        height = min(self.height() // 2, self.subtitle_label.heightForWidth(width))
        bottom = self.controls.y() - 12 if self.controls.isVisible() else self.height() - 24
        if self.playback_feedback.isVisible():
            self.playback_feedback.setGeometry(40, bottom - 44, width, 44)
            self.playback_feedback.raise_()
            bottom = self.playback_feedback.y() - 8
        self.subtitle_label.setGeometry(40, max(80, bottom - height), width, height)

    def toggle_play(self):
        if self.media.source().isEmpty():
            self.choose_file()
        elif self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media.pause()
        else:
            self.media.play()

    def playback_changed(self, state):
        self.play_button.setText('Ⅱ' if state == QMediaPlayer.PlaybackState.PlayingState else '▶')

    def set_volume(self, value):
        self.audio.setVolume(value / 100)
        suffix = ' (muted)' if self.audio.isMuted() else ''
        self.show_playback_feedback(f'Volume {value}%{suffix}')

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        text = 'Muted' if self.audio.isMuted() else f'Volume {round(self.audio.volume() * 100)}%'
        self.show_playback_feedback(text)

    def skip(self, offset):
        if self.media.isSeekable():
            self.media.setPosition(max(0, min(self._duration, self.media.position() + offset)))
            direction = '+' if offset >= 0 else '−'
            self.show_playback_feedback(f'{direction}{abs(offset) // 1000} seconds')

    def begin_seek(self):
        self._scrubbing = True
        self._resume_after_seek = self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.media.pause()
        self.show_osd()

    def preview_seek(self):
        if self._preview_position is not None and self.media.isSeekable():
            position = self._preview_position
            self._preview_position = None
            self.media.setPosition(position)

    def end_seek(self):
        self.scrub_timer.stop()
        self._preview_position = self.seek.value() * self._duration // 10000
        self.preview_seek()
        self._scrubbing = False
        if self._resume_after_seek:
            self.media.play()
        self._resume_after_seek = False
        self.show_osd()

    def seek_changed(self, value):
        if not self.media.isSeekable():
            return
        self._preview_position = value * self._duration // 10000
        if self._scrubbing:
            self.time.setText(f'{timestamp(self._preview_position)} / {timestamp(self._duration)}')
            if not self.scrub_timer.isActive():
                self.scrub_timer.start()
        else:
            self.preview_seek()

    def position_changed(self, position):
        self.update_subtitles(position)
        if not self._scrubbing:
            self.time.setText(f'{timestamp(position)} / {timestamp(self._duration)}')
            self.seek.blockSignals(True)
            self.seek.setValue(position * 10000 // self._duration if self._duration else 0)
            self.seek.blockSignals(False)

    def duration_changed(self, duration):
        self._duration = duration
        self.restore_playback_position()
        self.position_changed(self.media.position())

    def toggle_fullscreen(self):
        # Restore the intended fitted size even if fullscreen interrupts resizing.
        if self.window_resize.state() == QPropertyAnimation.State.Running:
            target = self.window_resize.endValue()
            self.window_resize.stop()
            self.setGeometry(target)
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._normal_geometry = self.geometry()
            self.showFullScreen()
        self.show_osd()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry:
                previous = self._normal_geometry
                self.setGeometry(previous)
                # Reapply after the window manager processes the state change.
                QTimer.singleShot(0, lambda: self.setGeometry(previous) if not self.isFullScreen() else None)
        self.show_osd()

    def show_osd(self):
        if self._quiet_file_start:
            return
        self.leave_timer.stop()
        self.unsetCursor()
        for overlay, effect, fade in self._osd_fades:
            fade.stop()
            effect.setOpacity(1.0)
            effect.setEnabled(False)
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            overlay.show()
            overlay.raise_()
        self.layout_resize_handles()
        self.layout_subtitles()
        self.idle.start()

    def hide_osd(self, leaving=False):
        if QApplication.activePopupWidget() is not None:
            self.idle.start()
            return
        if not leaving and (self.controls.underMouse() or self.top.underMouse() or self._scrubbing):
            self.idle.start()
            return
        for overlay, effect, fade in self._osd_fades:
            if overlay.isVisible() and fade.state() != QPropertyAnimation.State.Running:
                overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                effect.setEnabled(True)
                fade.setStartValue(effect.opacity())
                fade.setEndValue(0.0)
                fade.start()
        if not leaving:
            self.setCursor(Qt.CursorShape.BlankCursor)

    def finish_fade(self, overlay):
        if overlay.graphicsEffect().opacity() < 0.01:
            overlay.hide()
            overlay.graphicsEffect().setEnabled(False)
            self.layout_subtitles()

    def is_video_surface(self, obj):
        return (obj is self or obj is self.view.viewport() or obj is self.empty
                or (isinstance(obj, QLabel) and self.empty.isAncestorOf(obj)))

    def show_osd_from_pointer(self, position):
        if self._quiet_file_start:
            if self._initial_reveal_pending:
                return
            # Mapping a window under a stationary cursor creates an Enter event.
            # Do not treat that as the user moving over the video.
            if position == self._quiet_start_cursor and not self._quiet_pointer_left:
                return
            self._quiet_file_start = False
        self.show_osd()

    def focus_from_pointer(self, target=None):
        if self._initial_reveal_pending or not self.isVisible() or self.isMinimized():
            return
        if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
            return
        target = self if target is None else target
        if target is not self and target is not self.shortcuts_dialog:
            return
        if not target.isActiveWindow():
            # Request focus without raising/reordering the window stack.
            target.activateWindow()
            if target.windowHandle():
                target.windowHandle().requestActivate()

    def eventFilter(self, obj, event):
        if isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)):
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and (obj is self.title or isinstance(obj, ResizeHandle)):
                self.window_resize.stop()
            if kind == QEvent.Type.MouseMove:
                self.focus_from_pointer(obj.window())
                self.show_osd_from_pointer(event.globalPosition().toPoint())
                if self._window_drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
                    origin, window_position = self._window_drag
                    delta = event.globalPosition().toPoint() - origin
                    if not self._window_drag_started and delta.manhattanLength() >= QApplication.startDragDistance():
                        self._window_drag_started = True
                        self._ignore_video_release = True
                        self.click_timer.stop()
                        handle = self.windowHandle()
                        self._window_drag_native = bool(handle and handle.startSystemMove())
                    if self._window_drag_started:
                        if not self._window_drag_native:
                            self.move(window_position + delta)
                        return True
            elif kind == QEvent.Type.MouseButtonPress and obj is self.title and event.button() == Qt.MouseButton.LeftButton:
                if not self.isFullScreen() and self.windowHandle():
                    self.windowHandle().startSystemMove()
                    return True
            elif kind == QEvent.Type.MouseButtonDblClick and self.is_video_surface(obj) and event.button() == Qt.MouseButton.LeftButton:
                self._window_drag = None
                self.click_timer.stop()
                self._ignore_video_release = True
                self.toggle_fullscreen()
                return True
            elif kind == QEvent.Type.MouseButtonPress and self.is_video_surface(obj) and event.button() == Qt.MouseButton.LeftButton:
                self.window_resize.stop()
                self._window_drag = (event.globalPosition().toPoint(), self.pos()) if not self.isFullScreen() and not self.isMaximized() else None
                self._window_drag_started = False
                self._window_drag_native = False
                self._ignore_video_release = False
                return True
            elif kind == QEvent.Type.MouseButtonRelease and self.is_video_surface(obj) and event.button() == Qt.MouseButton.LeftButton:
                self._window_drag = None
                if self._ignore_video_release:
                    self._ignore_video_release = False
                elif obj is self.view.viewport() and not self.media.source().isEmpty():
                    self.click_timer.start(QApplication.doubleClickInterval() + 10)
                return True
        return super().eventFilter(obj, event)

    def leaveEvent(self, event):
        if self._quiet_file_start and not self._initial_reveal_pending:
            self._quiet_pointer_left = True
        self.idle.stop()
        self.leave_timer.start()
        self.unsetCursor()
        super().leaveEvent(event)

    def enterEvent(self, event):
        self.focus_from_pointer()
        self.show_osd_from_pointer(event.globalPosition().toPoint())
        super().enterEvent(event)

    def show_prepared(self):
        # Map invisibly so the compositor cannot display an unpainted surface.
        self._initial_reveal_pending = True
        self.setWindowOpacity(0.0)
        self.show()
        self._reveal_timer.start(60)

    def reveal_prepared_window(self):
        if not self._initial_reveal_pending or not self.isVisible():
            return
        self.refresh_startup_frame()
        # A second event-loop turn lets the backing-store update be submitted.
        QTimer.singleShot(0, self.finish_initial_reveal)

    def finish_initial_reveal(self):
        if self._initial_reveal_pending and self.isVisible():
            self._initial_reveal_pending = False
            self.setWindowOpacity(1.0)
            self.repaint()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._startup_refresh_started:
            self._startup_refresh_started = True
            # X11/compositor mapping can retain the first backing-store frame
            # until input damages the window. Refresh after mapping/animation.
            for delay in (0, 150, 500, 1000):
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self.refresh_startup_frame)
                self._startup_refresh_timers.append(timer)
                timer.start(delay)

    def refresh_startup_frame(self):
        if not self.isVisible() or self.isMinimized():
            return
        self.ensurePolished()
        if self.empty.isVisible():
            self.empty.layout().activate()
            self.layout_splash_credit()
        # Request fresh native-resolution paint, including text-bearing children.
        for child in self.findChildren(QWidget):
            if child.isVisible() and isinstance(child, (QLabel, QPushButton, QSlider)):
                child.update()
        self.update()
        self.repaint()
        if self.windowHandle():
            self.windowHandle().requestUpdate()

    def layout_splash_credit(self):
        self.empty.layout().activate()
        height = self.splash_credit.sizeHint().height()
        gap_top = self.splash_open.geometry().bottom() + 1
        gap_bottom = self.controls.y()
        y = (gap_top + gap_bottom - height) // 2
        self.splash_credit.setGeometry(0, y, self.empty.width(), height)
        text_bottom = y + (height + self.splash_credit.fontMetrics().height()) // 2
        self.splash_codex_credit.setGeometry(0, text_bottom + 3, self.empty.width(),
            self.splash_codex_credit.sizeHint().height())

    def layout_resize_handles(self):
        width, height = self.width(), self.height()
        border, corner = 7, 20
        boxes = [
            (corner, 0, width - 2 * corner, border),
            (corner, height - border, width - 2 * corner, border),
            (0, corner, border, height - 2 * corner),
            (width - border, corner, border, height - 2 * corner),
            (0, 0, corner, corner),
            (width - corner, 0, corner, corner),
            (0, height - corner, corner, corner),
            (width - corner, height - corner, corner, corner),
        ]
        for handle, box in zip(self.resize_handles, boxes):
            handle.setGeometry(*box)
            handle.setVisible(not self.isFullScreen() and not self.isMaximized())
            handle.raise_()

    def resizeEvent(self, event):
        self.position_cast_panel()
        self.view.setGeometry(self.rect())
        size = self.size()
        self.video.setSize(QSizeF(size))
        self.scene.setSceneRect(QRectF(0, 0, size.width(), size.height()))
        self.empty.setGeometry(self.rect())
        self.loading_cover.setGeometry(self.rect())
        self.top.setGeometry(16, 16, self.width() - 32, 58)
        width = min(760, self.width() - 32)
        self.controls.setGeometry((self.width() - width) // 2, self.height() - 116, width, 92)
        self.layout_splash_credit()
        self.layout_resize_handles()
        self.notice.setGeometry(40, 90, self.width() - 80, 70)
        self.layout_subtitles()
        super().resizeEvent(event)

    def message(self, text):
        self.notice.setText(text or 'Unable to play this video.')
        self.notice.show()
        self.notice.raise_()
        self.notice_timer.start(7000)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.suffix.lower() in ('.srt', '.vtt'):
                    if self.media.source().isEmpty():
                        self.message('Open a video before loading subtitles.')
                    else:
                        self.load_subtitles(str(path))
                else:
                    self.open_file(str(path))
                event.acceptProposedAction()
                break

    def closeEvent(self, event):
        self.cast_manager.close()
        self.save_playback_position()
        self.window_resize.stop()
        self.feedback_hold.stop()
        self.feedback_fade.stop()
        if self.shortcuts_dialog is not None:
            self.shortcuts_dialog.close()
        self._initial_reveal_pending = False
        self._reveal_timer.stop()
        for timer in self._startup_refresh_timers:
            timer.stop()
        self.click_timer.stop()
        self._probe_generation += 1
        if self._forced_probe is not None:
            self._forced_probe.kill()
            self._forced_probe.waitForFinished(1000)
        self.scrub_timer.stop()
        self.media.stop()
        self.media.setSource(QUrl())
        QApplication.instance().removeEventFilter(self)
        event.accept()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--probe-forced':
        from forced_subtitles import subtitle_flags
        try:
            print(json.dumps(subtitle_flags(sys.argv[2])))
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    app = QApplication(sys.argv)
    font = app.font()
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)
    app.setApplicationName('Still Casting')
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName('still')
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / 'packaging' / 'still.png')))
    filename = ''
    if len(sys.argv) > 1:
        url = QUrl(sys.argv[1])
        filename = str(Path(url.toLocalFile() if url.isLocalFile() else sys.argv[1]).expanduser().resolve())
    import os
    instance = SingleInstance(app, identity=f'still-casting:{Path.home()}:{os.environ.get("WAYLAND_DISPLAY", os.environ.get("DISPLAY", "desktop"))}')
    try:
        if instance.start_or_forward(filename):
            return 0
    except RuntimeError as error:
        QMessageBox.warning(None, 'Still', str(error))
        return 1
    app.aboutToQuit.connect(instance.close)
    window = Player()
    instance.open_requested.connect(window.open_from_another_launch)
    if filename:
        window.open_file(filename, quiet_start=True)
    window.show_prepared()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
