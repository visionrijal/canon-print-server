#!/usr/bin/env python3
"""
Canon LBP2900B IPP Print Server for Termux
===========================================
Accepts print jobs over WiFi (IPP protocol) and sends them to the
Canon LBP2900B via USB using Android's USB Host API through termux-usb.

No usblp kernel module required.

Called by start.sh via:
    termux-usb -e "$INSTALL_DIR/run_server.sh" /dev/bus/usb/001/002

termux-usb injects TERMUX_USB_FD and TERMUX_USB_DEVICE into the environment.
run_server.sh is a single-executable wrapper that calls: exec python print_server.py
(termux-usb -e cannot take a shell string like "python script.py" — it needs
a path to one executable, otherwise TERMUX_USB_FD never gets set.)
"""

import os
import sys
import socket
import struct
import threading
import subprocess
import tempfile
import time
import logging
from ctypes import c_void_p, c_int, byref, POINTER

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.expanduser('~/canon-print-server/server.log'))
    ]
)
log = logging.getLogger(__name__)

# ── USB setup via termux-usb file descriptor ─────────────────────────────────

def get_printer_device():
    """
    termux-usb sets TERMUX_USB_FD to an open file descriptor for the USB
    device granted by Android's USB Host API. We pass that fd to
    libusb_wrap_sys_device() to get a usable handle — no usblp kernel
    module required.

    IMPORTANT: This function must be called from a process launched by
    termux-usb -e, not directly. The fd is only valid in that child process.
    """
    fd_str = os.environ.get('TERMUX_USB_FD')
    if not fd_str:
        log.error("TERMUX_USB_FD is not set.")
        log.error("This script must be launched via:")
        log.error("  termux-usb -e /path/to/run_server.sh /dev/bus/usb/XXX/YYY")
        log.error("Do not run print_server.py directly.")
        sys.exit(1)

    fd = int(fd_str)
    log.info(f"Got USB file descriptor from termux-usb: fd={fd}")
    log.info(f"USB device: {os.environ.get('TERMUX_USB_DEVICE', 'unknown')}")

    # Try usb1 (python-libusb1) openByFd first — cleanest path
    try:
        import usb1
        ctx = usb1.USBContext()
        handle = ctx.openByFd(fd)
        log.info("Opened printer via usb1.openByFd (libusb_wrap_sys_device)")
        return handle, ctx
    except AttributeError:
        log.warning("usb1.openByFd not available (older version) — trying ctypes fallback")
        return _get_device_ctypes(fd)
    except Exception as e:
        log.warning(f"usb1.openByFd failed: {e} — trying ctypes fallback")
        return _get_device_ctypes(fd)


def _get_device_ctypes(fd):
    """
    Direct ctypes approach for libusb_wrap_sys_device.
    Used when usb1 is too old to have openByFd.
    Based on: github.com/Querela/termux-usb-python
    """
    import ctypes
    import usb.backend.libusb1 as libusb1_backend

    backend = libusb1_backend.get_backend()
    if backend is None:
        log.error("libusb backend not found. Is libusb installed? (pkg install libusb)")
        sys.exit(1)

    lib = backend.lib
    ctx = backend.ctx

    lib.libusb_wrap_sys_device.argtypes = [c_void_p, c_int, POINTER(c_void_p)]
    lib.libusb_wrap_sys_device.restype  = c_int
    lib.libusb_get_device.argtypes      = [c_void_p]
    lib.libusb_get_device.restype       = c_void_p

    raw_handle = c_void_p()
    ret = lib.libusb_wrap_sys_device(ctx, fd, byref(raw_handle))
    if ret != 0:
        log.error(f"libusb_wrap_sys_device returned error {ret}")
        log.error("Make sure you tapped Allow on the USB permission dialog.")
        sys.exit(1)

    log.info("Opened printer via ctypes libusb_wrap_sys_device")

    # Detect bulk OUT endpoint from USB descriptors
    endpoint = _find_bulk_out_endpoint()
    log.info(f"Using bulk OUT endpoint: 0x{endpoint:02x}")

    class PrinterHandle:
        def __init__(self, lib, handle, endpoint):
            self.lib      = lib
            self.handle   = handle
            self.endpoint = endpoint

        def write(self, data):
            import ctypes
            buf         = (ctypes.c_ubyte * len(data))(*data)
            transferred = ctypes.c_int(0)
            ret = self.lib.libusb_bulk_transfer(
                self.handle,
                self.endpoint,
                buf,
                len(data),
                byref(transferred),
                5000   # 5 second timeout
            )
            if ret != 0:
                raise IOError(f"libusb_bulk_transfer failed: error {ret}")
            return transferred.value

        def close(self):
            self.lib.libusb_close(self.handle)

    return PrinterHandle(lib, raw_handle, endpoint), None


def _find_bulk_out_endpoint():
    """
    Parse USB descriptors from sysfs to find the bulk OUT endpoint address.
    Falls back to 0x01 (standard for USB printers) if parsing fails.
    """
    device_path = os.environ.get('TERMUX_USB_DEVICE', '')
    if device_path:
        try:
            parts  = device_path.rstrip('/').split('/')
            bus    = parts[-2].lstrip('0') or '0'
            devnum = parts[-1].lstrip('0') or '0'
            desc_path = f'/proc/bus/usb/{bus.zfill(3)}/{devnum.zfill(3)}'
            with open(desc_path, 'rb') as f:
                data = f.read()
            i = 0
            while i < len(data) - 7:
                length = data[i] if data[i] > 0 else 1
                if data[i+1] == 0x05:            # ENDPOINT descriptor
                    addr = data[i+2]
                    attr = data[i+3]
                    if (addr & 0x80) == 0 and (attr & 0x03) == 0x02:  # OUT + BULK
                        log.info(f"Detected bulk OUT endpoint: 0x{addr:02x}")
                        return addr
                i += length
        except Exception as e:
            log.debug(f"Endpoint auto-detect failed: {e}")
    log.info("Using default bulk OUT endpoint: 0x01")
    return 0x01


# ── Document rendering (PDF/PS → CAPT) ───────────────────────────────────────

def render_to_capt(doc_data, doc_format='application/pdf'):
    """
    Convert incoming document to Canon CAPT format.
    Pipeline: PDF/PS → Ghostscript (PWG raster) → rastertocapt → CAPT bytes
    """
    rastertocapt = os.path.expanduser('~/../usr/lib/cups/filter/rastertocapt')
    ppd_file     = os.path.expanduser('~/../usr/share/cups/model/Canon-LBP2900.ppd')

    if not os.path.exists(rastertocapt):
        raise RuntimeError(
            f"rastertocapt not found at {rastertocapt}. "
            "Re-run setup.sh to rebuild captdriver."
        )

    with tempfile.TemporaryDirectory(prefix='capt_') as tmpdir:
        input_file  = os.path.join(tmpdir, 'input')
        raster_file = os.path.join(tmpdir, 'raster.pwg')
        capt_file   = os.path.join(tmpdir, 'output.capt')

        with open(input_file, 'wb') as f:
            f.write(doc_data)

        log.info(f"Rendering {len(doc_data)} bytes ({doc_format}) → CAPT...")

        # Step 1: PDF/PS → PWG raster via Ghostscript
        gs_cmd = [
            'gs',
            '-dBATCH', '-dNOPAUSE', '-dSAFER', '-dQUIET',
            '-sDEVICE=pwgraster',
            '-r600',
            '-dDEVICEWIDTHPOINTS=595',   # A4
            '-dDEVICEHEIGHTPOINTS=842',
            f'-sOutputFile={raster_file}',
            input_file
        ]
        log.info("Running Ghostscript...")
        result = subprocess.run(gs_cmd, capture_output=True, timeout=120)

        if result.returncode != 0 or not _nonempty(raster_file):
            log.warning(f"pwgraster device failed, trying cups device fallback...")
            gs_cmd_fallback = [
                'gs',
                '-dBATCH', '-dNOPAUSE', '-dSAFER', '-dQUIET',
                '-sDEVICE=cups',
                '-r600x600',
                f'-sOutputFile={raster_file}',
                input_file
            ]
            result = subprocess.run(gs_cmd_fallback, capture_output=True, timeout=120)
            if result.returncode != 0 or not _nonempty(raster_file):
                raise RuntimeError(
                    f"Ghostscript failed (both devices):\n"
                    f"{result.stderr.decode(errors='replace')}"
                )

        log.info(f"Ghostscript raster: {os.path.getsize(raster_file)} bytes")

        # Step 2: PWG raster → CAPT via rastertocapt
        # rastertocapt expects CUPS filter calling convention:
        #   job-id user title copies options [filename]
        env = os.environ.copy()
        env['PPD'] = ppd_file

        log.info("Running rastertocapt...")
        with open(capt_file, 'wb') as out_f:
            result = subprocess.run(
                [rastertocapt, '1', 'termux', 'print-job', '1', '', raster_file],
                stdout=out_f,
                stderr=subprocess.PIPE,
                env=env,
                timeout=180
            )

        if result.stderr:
            log.debug(f"rastertocapt stderr: {result.stderr.decode(errors='replace')}")

        if not _nonempty(capt_file):
            raise RuntimeError("rastertocapt produced empty output.")

        log.info(f"CAPT data ready: {os.path.getsize(capt_file)} bytes")

        with open(capt_file, 'rb') as f:
            return f.read()


def _nonempty(path):
    return os.path.exists(path) and os.path.getsize(path) > 0


# ── USB write ─────────────────────────────────────────────────────────────────

def send_to_printer(capt_data, printer_handle):
    """Send rendered CAPT data to printer in chunks."""
    CHUNK = 4096
    total = len(capt_data)
    sent  = 0

    log.info(f"Sending {total} bytes to printer...")

    for i in range(0, total, CHUNK):
        chunk = capt_data[i:i+CHUNK]
        try:
            written = printer_handle.write(chunk)
            sent   += written
            if sent % (CHUNK * 10) == 0 or sent == total:
                pct = 100 * sent // total
                log.info(f"  {sent}/{total} bytes ({pct}%)")
        except Exception as e:
            log.error(f"USB write error at byte {sent}: {e}")
            raise

    log.info(f"Print complete: {sent}/{total} bytes sent.")


# ── Minimal IPP/HTTP server ───────────────────────────────────────────────────
#
# IPP (Internet Printing Protocol) runs over HTTP on port 631.
# We implement just enough to accept print jobs from any OS:
#   - Get-Printer-Attributes  (0x000b) — used by clients for discovery
#   - Print-Job               (0x0002) — actual print submission
#
# IPP packet layout:
#   [0:2]  version        (0x0101 = IPP 1.1)
#   [2:4]  operation code or status code
#   [4:8]  request/response ID
#   [8:]   attribute groups (tag + name-len + name + val-len + value)
#          terminated by end-of-attributes tag 0x03
#   [after 0x03]  optional document data (Print-Job only)

IPP_VERSION       = b'\x01\x01'
IPP_OK            = b'\x00\x00'

TAG_OP_ATTRS      = b'\x01'
TAG_JOB_ATTRS     = b'\x02'
TAG_END           = b'\x03'
TAG_PRINTER_ATTRS = b'\x04'

TYPE_CHARSET      = b'\x47'
TYPE_NATURAL_LANG = b'\x48'
TYPE_URI          = b'\x45'
TYPE_KEYWORD      = b'\x44'
TYPE_BOOL         = b'\x22'
TYPE_INTEGER      = b'\x21'
TYPE_ENUM         = b'\x23'
TYPE_TEXT_NO_LANG = b'\x41'
TYPE_NAME_NO_LANG = b'\x42'
TYPE_MIME_TYPE    = b'\x49'


def ipp_attr(tag, name, value):
    name_b  = name.encode()  if isinstance(name,  str) else name
    value_b = value.encode() if isinstance(value, str) else value
    return (tag
            + struct.pack('>H', len(name_b))  + name_b
            + struct.pack('>H', len(value_b)) + value_b)


def ipp_integer(name, value):
    return ipp_attr(TYPE_INTEGER, name, struct.pack('>i', value))

def ipp_enum(name, value):
    return ipp_attr(TYPE_ENUM, name, struct.pack('>i', value))

def ipp_bool(name, value):
    return ipp_attr(TYPE_BOOL, name, b'\x01' if value else b'\x00')


def make_printer_attrs_response(request_id, printer_uri):
    return (
        IPP_VERSION + IPP_OK + request_id +
        TAG_OP_ATTRS +
        ipp_attr(TYPE_CHARSET,      'attributes-charset',           'utf-8') +
        ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language',  'en') +
        TAG_PRINTER_ATTRS +
        ipp_attr(TYPE_URI,          'printer-uri-supported',        printer_uri) +
        ipp_attr(TYPE_KEYWORD,      'uri-security-supported',       'none') +
        ipp_attr(TYPE_KEYWORD,      'uri-authentication-supported', 'none') +
        ipp_attr(TYPE_NAME_NO_LANG, 'printer-name',                 'Canon LBP2900B') +
        ipp_enum(                   'printer-state',                3) +   # idle
        ipp_attr(TYPE_KEYWORD,      'printer-state-reasons',        'none') +
        ipp_attr(TYPE_KEYWORD,      'ipp-versions-supported',       '1.1') +
        ipp_attr(TYPE_KEYWORD,      'operations-supported',
                 struct.pack('>i', 0x0002) + struct.pack('>i', 0x000b)) +
        ipp_bool(                   'multiple-document-jobs-supported', False) +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-supported',    'application/pdf') +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-supported',    'application/postscript') +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-default',      'application/pdf') +
        ipp_bool(                   'printer-is-accepting-jobs',    True) +
        ipp_integer(                'queued-job-count',             0) +
        ipp_attr(TYPE_TEXT_NO_LANG, 'printer-info',
                 'Canon LBP2900B via Termux') +
        ipp_attr(TYPE_KEYWORD,      'pdl-override-supported',       'not-attempted') +
        TAG_END
    )


def make_print_job_response(request_id, job_id=1):
    return (
        IPP_VERSION + IPP_OK + request_id +
        TAG_OP_ATTRS +
        ipp_attr(TYPE_CHARSET,      'attributes-charset',          'utf-8') +
        ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language', 'en') +
        TAG_JOB_ATTRS +
        ipp_integer(                'job-id',                      job_id) +
        ipp_attr(TYPE_URI,          'job-uri',
                 f'ipp://localhost:631/jobs/{job_id}') +
        ipp_enum(                   'job-state',                   5) +    # processing
        TAG_END
    )


def make_error_response(request_id, status_code):
    return (
        IPP_VERSION +
        struct.pack('>H', status_code) +
        request_id +
        TAG_OP_ATTRS +
        ipp_attr(TYPE_CHARSET,      'attributes-charset',          'utf-8') +
        ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language', 'en') +
        TAG_END
    )


def parse_ipp_request(data):
    """Extract operation code, request ID, and document-format from IPP request."""
    if len(data) < 8:
        return None, None, None

    operation  = struct.unpack('>H', data[2:4])[0]
    request_id = data[4:8]
    doc_format = 'application/pdf'   # default

    i = 8
    while i < len(data) - 3:
        tag = data[i]
        if tag == 0x03:              # end-of-attributes
            break
        if tag in (0x01, 0x02, 0x04, 0x05):   # group tags — no payload
            i += 1
            continue
        if i + 3 > len(data):
            break
        name_len = struct.unpack('>H', data[i+1:i+3])[0]
        name_end = i + 3 + name_len
        if name_end + 2 > len(data):
            break
        name    = data[i+3:name_end].decode('utf-8', errors='ignore')
        val_len = struct.unpack('>H', data[name_end:name_end+2])[0]
        val_end = name_end + 2 + val_len
        if val_end > len(data):
            break
        value = data[name_end+2:val_end].decode('utf-8', errors='ignore')
        if name == 'document-format':
            doc_format = value
        i = val_end

    return operation, request_id, doc_format


class IPPHandler:
    """Handle a single HTTP/IPP connection."""

    def __init__(self, conn, addr, printer_handle, printer_ip):
        self.conn           = conn
        self.addr           = addr
        self.printer_handle = printer_handle
        self.printer_ip     = printer_ip
        self.printer_uri    = f'ipp://{printer_ip}:631/printers/LBP2900'

    def handle(self):
        try:
            self._process()
        except Exception as e:
            log.error(f"Handler error from {self.addr}: {e}", exc_info=True)
        finally:
            self.conn.close()

    def _recv_http(self):
        """Read full HTTP request (headers + body)."""
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = self.conn.recv(4096)
            if not chunk:
                return None, None
            raw += chunk

        sep        = raw.index(b'\r\n\r\n')
        header_raw = raw[:sep].decode('utf-8', errors='ignore')
        body       = raw[sep+4:]

        headers = {}
        lines   = header_raw.split('\r\n')
        request_line = lines[0]
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        content_length = int(headers.get('content-length', 0))
        while len(body) < content_length:
            chunk = self.conn.recv(min(65536, content_length - len(body)))
            if not chunk:
                break
            body += chunk

        return request_line, body

    def _process(self):
        request_line, body = self._recv_http()
        if not request_line or body is None:
            return

        parts  = request_line.split(' ')
        method = parts[0] if parts else ''
        path   = parts[1] if len(parts) > 1 else '/'

        log.info(f"{method} {path} from {self.addr[0]}")

        if method == 'GET' and path in ('/', '/printers/LBP2900'):
            self._send_http(200, b'Canon LBP2900B Print Server (Termux)\n')
            return

        if method != 'POST':
            self._send_http(405, b'Method Not Allowed')
            return

        if len(body) < 8:
            self._send_http(400, b'Request too short')
            return

        operation, request_id, doc_format = parse_ipp_request(body)
        if operation is None:
            self._send_http(400, b'Bad IPP request')
            return

        op_name = {
            0x0002: 'Print-Job',
            0x000b: 'Get-Printer-Attributes',
            0x0004: 'Validate-Job',
            0x000a: 'Get-Jobs',
        }.get(operation, f'0x{operation:04x}')
        log.info(f"IPP operation: {op_name}")

        if operation == 0x000b:   # Get-Printer-Attributes
            self._send_ipp(make_printer_attrs_response(request_id, self.printer_uri))

        elif operation in (0x0002, 0x0004):   # Print-Job or Validate-Job
            # Find document data: it starts after the end-of-attributes tag (0x03)
            end_tag = body.find(b'\x03', 8)
            if end_tag == -1:
                self._send_http(400, b'Malformed IPP: no end-of-attributes tag')
                return

            doc_data = body[end_tag+1:]

            if operation == 0x0004 or not doc_data:
                # Validate-Job or empty body — just acknowledge
                self._send_ipp(make_print_job_response(request_id, job_id=0))
                return

            log.info(f"Document received: {len(doc_data)} bytes, format={doc_format}")

            # Acknowledge immediately so the client doesn't time out
            job_id = int(time.time()) % 99999
            self._send_ipp(make_print_job_response(request_id, job_id=job_id))

            # Render and print in background
            threading.Thread(
                target=self._print_job,
                args=(doc_data, doc_format),
                daemon=True
            ).start()

        else:
            # Unsupported operation
            self._send_ipp(make_error_response(
                request_id,
                0x0501   # server-error-operation-not-supported
            ))

    def _print_job(self, doc_data, doc_format):
        try:
            capt_data = render_to_capt(doc_data, doc_format)
            send_to_printer(capt_data, self.printer_handle)
        except Exception as e:
            log.error(f"Print job failed: {e}", exc_info=True)

    def _send_http(self, status, body, content_type='text/plain'):
        reason = {200: 'OK', 400: 'Bad Request', 405: 'Method Not Allowed'}.get(status, 'Error')
        header = (
            f'HTTP/1.1 {status} {reason}\r\n'
            f'Content-Type: {content_type}\r\n'
            f'Content-Length: {len(body)}\r\n'
            f'Connection: close\r\n'
            f'\r\n'
        ).encode()
        self.conn.sendall(header + body)

    def _send_ipp(self, ipp_body):
        header = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Type: application/ipp\r\n'
            f'Content-Length: {len(ipp_body)}\r\n'
            'Connection: close\r\n'
            '\r\n'
        ).encode()
        self.conn.sendall(header + ipp_body)


# ── Main ──────────────────────────────────────────────────────────────────────

def get_wifi_ip():
    """
    Try every available network interface to find a non-loopback IPv4 address.
    Android phones may use wlan0, wlan1, wlan2, rmnet_data0, etc. depending
    on the ROM — never assume wlan0.
    """
    # Method 1: parse 'ip addr' for all interfaces, prefer wlan* addresses
    try:
        result = subprocess.run(['ip', 'addr'], capture_output=True, text=True)
        wlan_ip   = None
        other_ip  = None
        iface     = ''
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and line[0].isdigit():          # interface header line
                iface = line.split(':')[1].strip() if ':' in line else ''
            if line.startswith('inet ') and '127.' not in line:
                ip = line.split()[1].split('/')[0]
                if iface.startswith('wlan'):
                    wlan_ip = ip
                elif not iface.startswith('lo'):
                    other_ip = ip
        if wlan_ip:
            return wlan_ip
        if other_ip:
            return other_ip
    except Exception as e:
        log.debug(f"ip addr parse failed: {e}")

    # Method 2: UDP connect trick — OS picks the right source address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith('127.'):
            return ip
    except Exception as e:
        log.debug(f"UDP connect trick failed: {e}")

    return '0.0.0.0'


def bind_server(server, preferred_port):
    """
    Try to bind to preferred_port (631). If that fails for any reason
    (PermissionError on non-rooted, OSError if CUPS already holds it, etc.)
    fall back to 6310. Returns the port actually bound.
    """
    for port in (preferred_port, 6310, 16310):
        try:
            server.bind(('0.0.0.0', port))
            return port
        except OSError as e:
            log.warning(f"Cannot bind port {port}: {e}")
    log.error("Could not bind to any port. Is another instance running?")
    sys.exit(1)


def main():
    log.info("=" * 55)
    log.info("  Canon LBP2900B Termux IPP Print Server")
    log.info("=" * 55)

    # Open printer — requires TERMUX_USB_FD set by termux-usb
    log.info("Opening USB printer via termux-usb fd...")
    printer_handle, usb_ctx = get_printer_device()
    log.info("Printer opened successfully.")

    wifi_ip = get_wifi_ip()
    log.info(f"WiFi IP: {wifi_ip}")
    if wifi_ip == '0.0.0.0':
        log.warning("Could not detect WiFi IP. Is WiFi connected?")
        log.warning("Run 'ip addr' to find your IP, then use it manually.")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = bind_server(server, 631)

    if port != 631:
        log.warning(f"Using fallback port {port} — update printer URL on other devices.")

    log.info(f"Printer URI: ipp://{wifi_ip}:{port}/printers/LBP2900")
    log.info("")
    log.info("Add printer on other devices:")
    log.info(f"  Windows : http://{wifi_ip}:{port}/printers/LBP2900")
    log.info(f"  Linux   : lpadmin -p LBP2900 -v ipp://{wifi_ip}:{port}/printers/LBP2900 -E")
    log.info(f"  Android : NokoPrint > Network > IPP > {wifi_ip}:{port}/printers/LBP2900")
    log.info("")

    server.listen(10)
    log.info(f"IPP server listening on 0.0.0.0:{port} — waiting for print jobs...")

    try:
        while True:
            conn, addr = server.accept()
            log.info(f"Connection from {addr[0]}:{addr[1]}")
            handler = IPPHandler(conn, addr, printer_handle, wifi_ip)
            threading.Thread(target=handler.handle, daemon=True).start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        server.close()
        if usb_ctx:
            try:
                usb_ctx.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
