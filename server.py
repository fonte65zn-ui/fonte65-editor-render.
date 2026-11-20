import base64
import binascii
import hmac
import os
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
STRIPE = Path(__file__).resolve().with_name("faixa.png")
FILTER = (
    "[0:v:0]setpts=PTS-STARTPTS,fps=25,scale=w='min(iw,540)':h='min(ih,960)':"
    "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear[video];"
    "[1:v]crop=49:800:10:19,format=rgba,"
    "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
    "a='if((lt(Y,18)+gte(Y,H-18))*lt(X,20)*gt(g(X,Y),180),0,255)'[rail];"
    "[rail][video]scale2ref=w=trunc(iw/10):h=ih[brand][base];"
    "[base][brand]overlay=0:0:format=auto,split[playing][cover];"
    "[cover]trim=end_frame=1[still];"
    "[playing][still]overlay=eof_action=repeat:repeatlast=1:enable='lt(t,1)',drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
    "textfile=title.txt:expansion=none:fontcolor=white:fontsize=min(w/36\\,h/32):"
    "line_spacing=6:box=1:boxcolor=black@0.78:boxborderw=8:"
    "x=(w-tw)/2:y=(h-th)/2:enable='lt(t,1)'[reel]"
)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        ready = bool(TOKEN and shutil.which("ffmpeg") and STRIPE.is_file())
        self.send_response(200 if ready else 503)
        self.end_headers()
        self.wfile.write(b"ok" if ready else b"unavailable")

    def do_PUT(self):
        if self.path != "/render":
            self.send_error(404)
            return
        expected = ("Bearer " + TOKEN).encode()
        supplied = self.headers.get("Authorization", "").encode()
        if not TOKEN or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
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
        try:
            with tempfile.TemporaryDirectory(prefix="f65-") as folder:
                root = Path(folder)
                (root / "title.txt").write_text("\n".join(textwrap.wrap(title, 29)))
                with (root / "input.mp4").open("wb") as output:
                    while size:
                        chunk = self.rfile.read(min(size, 65536))
                        if not chunk:
                            self.send_error(400)
                            return
                        output.write(chunk)
                        size -= len(chunk)
                started = time.monotonic()
                result = subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-max_alloc", "134217728",
                    "-protocol_whitelist", "file", "-f", "mov", "-threads", "2", "-i", "input.mp4",
                    "-i", str(STRIPE), "-filter_complex_threads", "1", "-filter_complex", FILTER,
                    "-map", "[reel]", "-map", "0:a:0?", "-c:v", "libx264", "-threads", "2",
                    "-preset", "ultrafast", "-crf", "24", "-maxrate", "2400k", "-bufsize", "4800k", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", "-fs", str(LIMIT), "output.mp4",
                ], cwd=root, timeout=150, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log_message("FFmpeg: %.1fs, exit=%d", time.monotonic() - started, result.returncode)
                output = root / "output.mp4"
                if result.returncode or not output.is_file() or not 0 < output.stat().st_size < LIMIT:
                    self.send_error(422)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(output.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with output.open("rb") as video:
                    shutil.copyfileobj(video, self.wfile)
        except subprocess.TimeoutExpired:
            self.send_error(504)
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            BUSY.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Handler).serve_forever()
