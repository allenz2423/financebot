"""Tiny private CDP TCP bridge: rewrite Chromium's loopback Host metadata."""

import os
import re
import selectors
import socket
import socketserver

TARGET_PORT = int(os.getenv("CDP_PORT", "9222"))
PUBLIC_PORT = int(os.getenv("CDP_PROXY_PORT", "9223"))
PUBLIC_HOST = os.getenv("CDP_PUBLIC_HOST", socket.gethostname())


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection(("127.0.0.1", TARGET_PORT), timeout=10)
        try:
            request, _ = self._read_headers(self.request)
            request = re.sub(rb"(?im)^Host:.*\r\n", b"Host: localhost\r\n", request, count=1)
            upstream.sendall(request)
            response, remainder = self._read_headers(upstream)
            if b" 101 " in response or b" 101\r\n" in response:
                self.request.sendall(response)
                self._relay(upstream)
                return
            match = re.search(rb"(?im)^Content-Length:\s*(\d+)\r\n", response)
            body = remainder[:int(match.group(1))] if match else b""
            while match and len(body) < int(match.group(1)):
                body += upstream.recv(int(match.group(1)) - len(body))
            public = PUBLIC_HOST.encode() + b":" + str(PUBLIC_PORT).encode()
            body = body.replace(b"localhost:" + str(TARGET_PORT).encode(), public)
            body = body.replace(b"localhost", public)
            response = re.sub(rb"(?im)^Content-Length:\s*\d+\r\n",
                              b"Content-Length: " + str(len(body)).encode() + b"\r\n", response)
            self.request.sendall(response + body)
        finally:
            upstream.close()

    @staticmethod
    def _read_headers(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        marker = b"\r\n\r\n"
        pos = data.find(marker)
        return data[:pos + len(marker)], data[pos + len(marker):]

    def _relay(self, upstream):
        sel = selectors.DefaultSelector()
        sel.register(self.request, selectors.EVENT_READ, upstream)
        sel.register(upstream, selectors.EVENT_READ, self.request)
        while True:
            events = sel.select()
            if not events:
                return
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with Server(("0.0.0.0", PUBLIC_PORT), Handler) as server:
        server.serve_forever()
