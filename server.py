import base64
import binascii
import hmac
import os
import re
import shutil
import subprocess
import tempfile
import time
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIMIT = 80 * 1024 * 1024
TOKEN = os.environ.get("F65_RENDER_TOKEN", "")
BUSY = threading.BoundedSemaphore(1)
JOBS = Path(tempfile.mkdtemp(prefix="f65-jobs-"))
TTL = 7200
STRIPE = Path(__file__).resolve().with_name("faixa.png")
FILTER = (
    "[0:v:0]setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2[video];"
    "[1:v]crop=49:800:10:19,format=rgba,"
    "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
    "a='if((lt(Y,18)+gte(Y,H-18))*lt(X,20)*gt(g(X,Y),180),0,255)'[rail];"
    "[rail][video]scale2ref=w=trunc(iw/10):h=ih[brand][base];"
    "[base][brand]overlay=0:0:format=auto,drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
    "textfile=title.txt:expansion=none:fontcolor=white:fontsize=min(w/36\\,h/32):"
    "line_spacing=6:box=1:boxcolor=black@0.78:boxborderw=8:"
    "x=w*0.15:y=h*0.80-th:enable='lt(t,3)'[reel]"
)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def do_GET(self):
        if self.path != "/health":
            job = self.job()
            if job is None:
                self.send_error(404)
            elif self.authorized():
                if (job / "output.mp4").is_file():
                    self.send_video(job / "output.mp4")
                elif (job / "error").is_file():
                    self.send_error(int((job / "error").read_text()))
                else:
                    self.respond(202 if job.is_dir() else 404)
            return
        ready = bool(TOKEN and shutil.which("ffmpeg") and STRIPE.is_file())
        self.send_response(200 if ready else 503)
        self.end_headers()
        self.wfile.write(b"ok" if ready else b"unavailable")

    def authorized(self):
        expected = ("Bearer " + TOKEN).encode()
        supplied = self.headers.get("Authorization", "").encode()
        if TOKEN and hmac.compare_digest(supplied, expected):
            return True
        self.send_error(401)
        return False

    def job(self):
        match = re.fullmatch(r"/jobs/([0-9a-f]{32})", self.path)
        return JOBS / match[1] if match else None

    def send_video(self, path):
        with path.open("rb") as video:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.fstat(video.fileno()).st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            shutil.copyfileobj(video, self.wfile)

    def do_PUT(self):
        job = self.job()
        if self.path != "/render" and job is None:
            self.send_error(404)
            return
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            title = base64.b64decode(self.headers.get("X-F65-Headline", ""), validate=True).decode()
        except (ValueError, UnicodeError, binascii.Error):
            self.send_error(400)
            return
        title = " ".join(title.split())
        if not title or len(title) > 145:
            self.send_error(400)
            return
        if not 0 < size <= LIMIT:
            self.send_error(413)
            return
        if not BUSY.acquire(blocking=False):
            self.send_error(503)
            return
        root = None
        background = False
        try:
            for old in JOBS.iterdir():
                if time.time() - old.stat().st_mtime > TTL:
                    shutil.rmtree(old, ignore_errors=True)
            if job and job.exists():
                self.respond(202)
                return
            if job and sum(1 for _ in JOBS.iterdir()) >= 12:
                self.send_error(503)
                return
            root = Path(tempfile.mkdtemp(prefix="f65-")) if job is None else job
            if job:
                root.mkdir(mode=0o700)
            (root / "title.txt").write_text("\n".join(textwrap.wrap(title, 29)))
            with (root / "input.mp4").open("wb") as output:
                while size:
                    chunk = self.rfile.read(min(size, 65536))
                    if not chunk:
                        self.send_error(400)
                        return
                    output.write(chunk)
                    size -= len(chunk)
            if job:
                threading.Thread(target=render_job, args=(root,), daemon=True).start()
                background = True
                self.respond(202)
                return
            result = encode(root, timeout=120)
            if result == 200:
                self.send_video(root / "output.mp4")
            else:
                self.send_error(result)
        except OSError:
            self.close_connection = True
        finally:
            if not background:
                if root:
                    shutil.rmtree(root, ignore_errors=True)
                BUSY.release()

    def respond(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def encode(root, timeout):
    result = root / "pending.mp4"
    try:
        process = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-max_alloc", "134217728",
            "-protocol_whitelist", "file", "-f", "mov", "-threads", "2", "-i", "input.mp4",
            "-i", str(STRIPE), "-filter_complex_threads", "1", "-filter_complex", FILTER,
            "-map", "[reel]", "-map", "0:a:0?", "-c:v", "libx264", "-threads", "2",
            "-preset", "ultrafast", "-crf", "18", "-maxrate", "6M", "-bufsize", "12M", "-fps_mode", "passthrough",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", result.name,
        ], cwd=root, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 504
    if process.returncode or not result.is_file() or not 0 < result.stat().st_size <= LIMIT:
        return 422
    result.rename(root / "output.mp4")
    return 200


def render_job(root):
    try:
        code = encode(root, timeout=420)
        if code != 200:
            (root / "error").write_text(str(code))
    except OSError:
        (root / "error").write_text("500")
    finally:
        for name in ("input.mp4", "title.txt", "pending.mp4"):
            (root / name).unlink(missing_ok=True)
        BUSY.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Handler).serve_forever()
