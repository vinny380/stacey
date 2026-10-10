"""Forward Spotify's pairing endpoint out of Docker's private network.

Only the Soloist-advertised /zc endpoint is forwarded; control stays loopback-only.
"""

import http.client
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from zeroconf import ServiceBrowser, Zeroconf

ready = threading.Event()
target = {}


class Discovery:
    def add_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name)
        if info:
            target.update(port=info.port, path=info.properties.get(b"CPath", b"/zc").decode())
            ready.set()

    def update_service(self, zeroconf, service_type, name):
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        pass


class Proxy(BaseHTTPRequestHandler):
    def forward(self):
        if urlsplit(self.path).path != target["path"]:
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", 0))
        if size > 65536:
            self.send_error(413)
            return
        connection = http.client.HTTPConnection("127.0.0.1", target["port"], timeout=10)
        try:
            connection.request(
                self.command,
                self.path,
                body=self.rfile.read(size) if size else None,
                headers={"Content-Type": self.headers.get("Content-Type", "")},
            )
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_error(502)
        finally:
            connection.close()

    do_GET = forward
    do_POST = forward

    def log_message(self, *args):
        # Pairing requests contain credentials; never log their paths or bodies.
        pass


# Let Soloist bind its discovery socket first. Opening another multicast
# listener before Soloist starts can prevent its own advertisement from binding.
for _ in range(100):
    try:
        with socket.create_connection(("127.0.0.1", 9090), timeout=0.1):
            break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("Soloist did not start its WebSocket API")

zeroconf = Zeroconf()
browser = ServiceBrowser(zeroconf, "_spotify-connect._tcp.local.", Discovery())
try:
    if not ready.wait(20):
        raise SystemExit("Soloist did not advertise its pairing endpoint")
    print("Spotify pairing relay ready on port 24879", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 24879), Proxy).serve_forever()
finally:
    browser.cancel()
    zeroconf.close()
