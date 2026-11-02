"""Google Cast discovery and a token-protected, single-file HTTP stream."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import threading

from PySide6.QtCore import QObject, Signal
from dlna import DlnaDevice, discover_dlna


class MediaServer:
    def __init__(self, filename, host):
        self.path = Path(filename).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError('Choose a video file first.')
        self.route = '/' + secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if self.path != owner.route:
                    self.send_error(404)
                    return
                try:
                    stream = owner.path.open('rb')
                except OSError:
                    self.send_error(404)
                    return
                with stream:
                    import os
                    size = os.fstat(stream.fileno()).st_size
                    start, end = 0, size - 1
                    requested = self.headers.get('Range')
                    if requested:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested.strip())
                        if not match or not any(match.groups()):
                            self.send_error(416)
                            return
                        first, last = match.groups()
                        if first:
                            start = int(first)
                            end = min(int(last), end) if last else end
                        else:
                            start = max(0, size - int(last))
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header('Content-Range', f'bytes */{size}')
                            self.end_headers()
                            return
                    self.send_response(206 if requested else 200)
                    self.send_header('Content-Type', owner.mime)
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(max(0, end - start + 1)))
                    if requested:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    if body:
                        stream.seek(start)
                        remaining = end - start + 1
                        try:
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            pass

        self.mime = {'.mkv': 'video/x-matroska', '.mp4': 'video/mp4',
                     '.m4v': 'video/mp4', '.webm': 'video/webm'}.get(
                         self.path.suffix.lower(), mimetypes.guess_type(self.path.name)[0] or 'application/octet-stream')
        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://{host}:{self.server.server_port}{self.route}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CastManager(QObject):
    devices_found = Signal(list)
    status = Signal(str)
    started = Signal(str, str)
    failed = Signal(str)
    busy = Signal(bool)
    active_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='still-cast')
        self.devices = []
        self.browser = None
        self.cast = None
        self.server = None
        self.closed = threading.Event()

    def submit(self, operation):
        if self.closed.is_set():
            return
        self.busy.emit(True)
        def run():
            try:
                if not self.closed.is_set():
                    operation()
            except Exception as error:
                if not self.closed.is_set():
                    self.failed.emit(str(error))
            finally:
                if not self.closed.is_set():
                    self.busy.emit(False)
        self.executor.submit(run)

    def discover(self):
        def work():
            import pychromecast
            self.status.emit('Searching for TVs…')
            if self.browser:
                pychromecast.discovery.stop_discovery(self.browser)
                self.browser = None
            previous = self.devices
            # Search both protocols together; neither changes the shared UI.
            with ThreadPoolExecutor(max_workers=2) as scans:
                google = scans.submit(pychromecast.get_chromecasts, timeout=6)
                dlna = scans.submit(discover_dlna)
                errors = []
                try:
                    discovered, self.browser = google.result()
                except Exception as error:
                    discovered = []
                    errors.append(f'Google Cast: {error}')
                try:
                    renderers = dlna.result()
                except Exception as error:
                    renderers = []
                    errors.append(f'DLNA: {error}')
            # Prefer Google Cast when a TV advertises both protocols.
            google_hosts = {c.cast_info.host for c in discovered}
            discovered += [d for d in renderers if d.host not in google_hosts]
            self.devices = []
            for device in discovered:
                if self.cast and device.uuid == self.cast.uuid:
                    self.disconnect_device(device)
                    device = self.cast
                self.devices.append(device)
            for device in previous:
                if device is not self.cast and device not in self.devices:
                    self.disconnect_device(device)
            if not self.closed.is_set():
                self.devices_found.emit([
                    (str(c.uuid), c.name or c.cast_info.host) for c in self.devices])
                if errors:
                    self.status.emit('Search completed with a partial result. ' + '; '.join(errors))
        self.submit(work)

    def start(self, identifier, filename, position):
        def work():
            selected = next((c for c in self.devices if str(c.uuid) == identifier), None)
            if selected is None:
                raise RuntimeError('TV is no longer available. Refresh the device list.')
            self.stop_session()
            self.cast = selected
            try:
                selected.wait(timeout=10)
                # Ask the OS which local interface reaches this TV.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                    route.connect((selected.cast_info.host, 8009))
                    host = route.getsockname()[0]
                self.server = MediaServer(filename, host)
                media = selected.media_controller
                note = ''
                if isinstance(selected, DlnaDevice):
                    note = selected.load(self.server.url, self.server.mime, Path(filename).name,
                                         position, self.closed)
                else:
                    media.play_media(self.server.url, self.server.mime,
                                     title=Path(filename).name, current_time=position / 1000,
                                     stream_type='BUFFERED')
                    media.block_until_active(timeout=15)
                    import time
                    deadline = time.monotonic() + 10
                    while media.status.player_state not in ('PLAYING', 'PAUSED', 'BUFFERING'):
                        if self.closed.is_set() or time.monotonic() >= deadline:
                            break
                        media.update_status()
                        self.closed.wait(0.2)
                    if media.status.player_state not in ('PLAYING', 'PAUSED', 'BUFFERING'):
                        raise RuntimeError('The TV did not start playback. Check its format support and the computer firewall.')
                if not self.closed.is_set():
                    self.started.emit(selected.name, filename)
                    self.active_changed.emit(True)
                    self.status.emit(f'Casting to {selected.name}. ' + (note or 'Use the TV remote or the cast controls below.'))
            except Exception:
                self.stop_session()
                raise
        self.submit(work)

    def command(self, name):
        def work():
            if not self.cast:
                raise RuntimeError('No active cast session.')
            if name == 'stop':
                stopped = self.stop_session()
                self.status.emit('Casting stopped. Local playback remains paused.' if stopped else
                    'Disconnected locally. The TV did not acknowledge Stop; use its remote if playback continues.')
            else:
                getattr(self.cast.media_controller, name)()
        self.submit(work)

    def stop_session(self):
        cast, server = self.cast, self.server
        self.cast = self.server = None
        stopped = True
        try:
            if cast:
                try:
                    cast.media_controller.stop()
                except Exception:
                    # A receiver may have ended the session or gone offline.
                    # Cleanup must still finish, and a new cast must be allowed.
                    stopped = False
        finally:
            if server:
                server.close()
            if cast and cast not in self.devices:
                self.disconnect_device(cast)
            if not self.closed.is_set():
                self.active_changed.emit(False)
        return stopped

    @staticmethod
    def disconnect_device(device):
        try:
            device.disconnect(timeout=2)
        except Exception:
            pass

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        def cleanup():
            try:
                self.stop_session()
            finally:
                if self.browser:
                    import pychromecast
                    pychromecast.discovery.stop_discovery(self.browser)
                for device in self.devices:
                    self.disconnect_device(device)
        self.executor.submit(cleanup)
        self.executor.shutdown(wait=False)
