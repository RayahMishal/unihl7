"""
MLLP (Minimal Lower Layer Protocol) listener and client for UniHL7 - real-time HL7 over TCP.

HL7 v2 systems rarely use HTTP. They open a TCP connection and wrap each message in framing bytes:
    <VT 0x0B> message <FS 0x1C><CR 0x0D>
The receiver must answer with an ACK message: MSA|AA (accepted) or MSA|AE (error, rejected).

    python mllp.py serve [port]   # listen (default 2575), writes FHIR JSON to unihl7_out/
    python mllp.py demo           # start a server, send the sample messages, print the ACKs
Standard library only.
"""
import socket
import socketserver
import sys
import threading
from datetime import datetime

from unihl7 import Gateway, Message, NormalizeError, samples

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"


def frame(text):
    return SB + text.replace("\n", "\r").encode("utf-8") + EB + CR


def make_ack(original, ok, reason=""):
    """Build an ACK for the original message (MSA-1 AA = accepted, AE = application error)."""
    try:
        m = Message(original)
        ctrl, app, fac = m.get("MSH-10"), m.get("MSH-3"), m.get("MSH-4")
        ver, ev = m.get("MSH-12") or "2.5.1", m.get("MSH-9.2")
    except NormalizeError:
        ctrl = app = fac = ev = ""
        ver = "2.5.1"
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    msa = f"MSA|{'AA' if ok else 'AE'}|{ctrl}" + (f"|{reason[:200]}" if reason else "")
    msa = msa.replace("\r", " ").replace("\n", " ")
    return (f"MSH|^~\\&|UNIHL7|HUB|{app}|{fac}|{now}||ACK^{ev}|ACK{ctrl or now}|P|{ver}\r{msa}\r")


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        buf = b""
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            buf += chunk
            while EB + CR in buf:
                raw, buf = buf.split(EB + CR, 1)
                text = raw.lstrip(SB).decode("utf-8", errors="replace")
                ok, info, _ = self.server.gateway.process(text)
                reason = "" if ok else "; ".join(info["problems"])
                self.request.sendall(frame(make_ack(text, ok, reason)))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port=2575, gateway=None, host="127.0.0.1"):
        self.gateway = gateway or Gateway(out_dir="unihl7_out")
        super().__init__((host, port), Handler)


def send(text, host="127.0.0.1", port=2575, timeout=5):
    """Send one message, return the ACK text."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(frame(text))
        buf = b""
        while EB + CR not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return buf.lstrip(SB).split(EB)[0].decode("utf-8", errors="replace")


def demo():
    server = Server(port=0)  # port 0 = any free port
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"MLLP listener on 127.0.0.1:{port}\n")
    for name, text in samples().items():
        ack = send(text, port=port)
        msa = [l for l in ack.split("\r") if l.startswith("MSA")][0]
        print(f"{name:22} -> {msa}")
    s = server.gateway.stats
    print(f"\nreceived {s['received']} | accepted {s['accepted']} | rejected {s['rejected']} | "
          f"patients in index {len(server.gateway.index.people)} | FHIR files in unihl7_out/")
    server.shutdown()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 2575
        srv = Server(port=port)
        print(f"UniHL7 MLLP listener on 127.0.0.1:{port}  (Ctrl+C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped;", srv.gateway.stats)
    elif cmd == "demo":
        demo()
    else:
        print(__doc__)
