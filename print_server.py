#!/usr/bin/env python3
"""
Canon LBP2900B IPP Print Server for Termux
===========================================
Accepts print jobs over WiFi (IPP protocol) and sends them to the
Canon LBP2900B via USB using Android's USB Host API through termux-usb.

No usblp kernel module required.

Usage (called by start.sh via termux-usb):
    termux-usb -e "python print_server.py" -r /dev/bus/usb/001/002
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
    In Termux, Android passes the USB file descriptor via the environment
    variable TERMUX_USB_FD. We use libusb_wrap_sys_device to get a handle
    from it, bypassing the missing usblp kernel module entirely.
    """
    fd_str = os.environ.get('TERMUX_USB_FD')
    if not fd_str:
        log.error("TERMUX_USB_FD not set. Run via: termux-usb -e 'python print_server.py' -r /dev/bus/usb/XXX/YYY")
        sys.exit(1)

    fd = int(fd_str)
    log.info(f"Got USB file descriptor: {fd}")

    try:
        import usb1
        ctx = usb1.USBContext()
        # libusb_wrap_sys_device: wrap an existing fd into a libusb handle
        # This is the Android-specific path that bypasses kernel driver
        handle = ctx.openByFd(fd)
        log.info("Successfully opened printer via libusb fd wrapping")
        return handle, ctx
    except AttributeError:
        # Older usb1 without openByFd — use ctypes directly
        return _get_device_ctypes(fd)
    except Exception as e:
        log.warning(f"usb1.openByFd failed ({e}), trying ctypes fallback...")
        return _get_device_ctypes(fd)


def _get_device_ctypes(fd):
    """
    Manual ctypes approach for libusb_wrap_sys_device.
    Based on: github.com/Querela/termux-usb-python
    """
    import ctypes
    import usb.core
    import usb.backend.libusb1 as libusb1

    backend = libusb1.get_backend()
    lib = backend.lib
    ctx = backend.ctx

    # Teach ctypes about the Android-specific function
    lib.libusb_wrap_sys_device.argtypes = [
        c_void_p, c_int,
        POINTER(c_void_p)
    ]
    lib.libusb_wrap_sys_device.restype = c_int
    lib.libusb_get_device.argtypes = [c_void_p]
    lib.libusb_get_device.restype = c_void_p

    handle = c_void_p()
    ret = lib.libusb_wrap_sys_device(ctx, fd, byref(handle))
    if ret != 0:
        log.error(f"libusb_wrap_sys_device failed: {ret}")
        sys.exit(1)

    log.info("Opened printer via ctypes libusb_wrap_sys_device")

    # Wrap into a usable write function
    class PrinterHandle:
        def __init__(self, lib, handle):
            self.lib = lib
            self.handle = handle
            # Find bulk OUT endpoint
            self.endpoint = 0x01  # Standard for USB printers (EP1 OUT)

        def write(self, data):
            buf = (ctypes.c_ubyte * len(data))(*data)
            transferred = ctypes.c_int(0)
            ret = self.lib.libusb_bulk_transfer(
                self.handle,
                self.endpoint,  # bulk OUT endpoint
                buf,
                len(data),
                byref(transferred),
                5000  # 5 second timeout
            )
            if ret != 0:
                raise IOError(f"USB write failed: {ret}")
            return transferred.value

        def close(self):
            self.lib.libusb_close(self.handle)

    return PrinterHandle(lib, handle), None


def find_bulk_out_endpoint(fd):
    """Parse USB descriptors to find the bulk OUT endpoint address."""
    try:
        import usb.core, usb.backend.libusb1 as libusb1
        # Can't use find() on Android, parse /dev/bus/usb directly
        device_path = os.environ.get('TERMUX_USB_DEVICE', '')
        if device_path:
            bus, dev = device_path.split('/')[-2:]
            # Read descriptor
            with open(f'/proc/bus/usb/{bus}/{dev}', 'rb') as f:
                # Parse for bulk OUT endpoint
                data = f.read()
                # Look for endpoint descriptor (0x05) with direction OUT (bit 7 = 0)
                i = 0
                while i < len(data) - 7:
                    if data[i+1] == 0x05:  # bDescriptorType = ENDPOINT
                        addr = data[i+2]
                        attr = data[i+3]
                        if (addr & 0x80) == 0 and (attr & 0x03) == 0x02:  # OUT + BULK
                            return addr
                    i += data[i] if data[i] > 0 else 1
    except Exception as e:
        log.debug(f"Endpoint detection failed: {e}")
    return 0x01  # Default EP1 OUT for most USB printers


# ── Document rendering (PDF/PS → CAPT) ───────────────────────────────────────

def render_to_capt(doc_data, doc_format='application/pdf'):
    """
    Convert incoming document to CAPT using rastertocapt filter.
    Pipeline: PDF → Ghostscript → raster → rastertocapt → CAPT bytes
    """
    with tempfile.TemporaryDirectory(prefix='capt_') as tmpdir:
        input_file = os.path.join(tmpdir, 'input')
        raster_file = os.path.join(tmpdir, 'raster.pwg')
        capt_file = os.path.join(tmpdir, 'output.capt')
        ppd_file = os.path.expanduser('~/../usr/share/cups/model/Canon-LBP2900.ppd')

        # Write input document
        with open(input_file, 'wb') as f:
            f.write(doc_data)

        log.info(f"Rendering {len(doc_data)} bytes ({doc_format}) to CAPT...")

        # Step 1: PDF/PS → PWG Raster via Ghostscript
        gs_cmd = [
            'gs',
            '-dBATCH', '-dNOPAUSE', '-dSAFER',
            '-dQUIET',
            '-sDEVICE=pwgraster',          # PWG raster output
            '-r600',                        # LBP2900 native resolution
            '-dDEVICEWIDTHPOINTS=595',     # A4 width
            '-dDEVICEHEIGHTPOINTS=842',    # A4 height
            '-sOutputFile=' + raster_file,
            input_file
        ]

        log.info("Running Ghostscript...")
        result = subprocess.run(gs_cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            log.error(f"Ghostscript failed: {result.stderr.decode()}")
            # Try with PS device as fallback
            gs_cmd[5] = '-sDEVICE=cups'
            gs_cmd[6] = '-r600x600'
            result = subprocess.run(gs_cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"Ghostscript failed: {result.stderr.decode()}")

        if not os.path.exists(raster_file) or os.path.getsize(raster_file) == 0:
            raise RuntimeError("Ghostscript produced empty output")

        log.info(f"Raster: {os.path.getsize(raster_file)} bytes")

        # Step 2: PWG Raster → CAPT via rastertocapt
        rastertocapt = os.path.expanduser('~/../usr/lib/cups/filter/rastertocapt')
        if not os.path.exists(rastertocapt):
            raise RuntimeError(f"rastertocapt not found at {rastertocapt}. Run setup.sh first.")

        # rastertocapt expects: job-id user title copies options [filename]
        capt_cmd = [rastertocapt, '1', 'user', 'print', '1', '', raster_file]
        env = os.environ.copy()
        env['PPD'] = ppd_file

        log.info("Running rastertocapt...")
        with open(capt_file, 'wb') as out:
            result = subprocess.run(
                capt_cmd,
                stdout=out,
                stderr=subprocess.PIPE,
                env=env,
                timeout=120
            )

        if result.returncode != 0:
            log.warning(f"rastertocapt stderr: {result.stderr.decode()}")

        if not os.path.exists(capt_file) or os.path.getsize(capt_file) == 0:
            raise RuntimeError("rastertocapt produced empty output")

        log.info(f"CAPT data: {os.path.getsize(capt_file)} bytes")

        with open(capt_file, 'rb') as f:
            return f.read()


# ── USB write ─────────────────────────────────────────────────────────────────

def send_to_printer(capt_data, printer_handle):
    """Send rendered CAPT data to printer in chunks."""
    CHUNK = 4096
    total = len(capt_data)
    sent = 0

    log.info(f"Sending {total} bytes to printer...")

    for i in range(0, total, CHUNK):
        chunk = capt_data[i:i+CHUNK]
        try:
            written = printer_handle.write(chunk)
            sent += written
            if sent % (CHUNK * 10) == 0:
                log.info(f"  {sent}/{total} bytes ({100*sent//total}%)")
        except Exception as e:
            log.error(f"USB write error at byte {sent}: {e}")
            raise

    log.info(f"Print job complete: {sent} bytes sent")


# ── Minimal IPP server ────────────────────────────────────────────────────────
#
# IPP (Internet Printing Protocol) runs over HTTP on port 631.
# We implement just enough to accept print jobs:
#   - Get-Printer-Attributes (discovery)
#   - Print-Job (actual printing)
#
# IPP packet format:
#   2 bytes: version (0x0101 = IPP 1.1)
#   2 bytes: operation/status code
#   4 bytes: request/response ID
#   attributes (tag-value pairs)
#   end-of-attributes tag (0x03)
#   optional data

IPP_VERSION        = b'\x01\x01'
IPP_OK             = b'\x00\x00'
IPP_GET_ATTRS      = b'\x00\x0b'
IPP_PRINT_JOB      = b'\x00\x02'

TAG_OP_ATTRS       = b'\x01'
TAG_JOB_ATTRS      = b'\x02'
TAG_END            = b'\x03'
TAG_PRINTER_ATTRS  = b'\x04'

TYPE_CHARSET       = b'\x47'
TYPE_NATURAL_LANG  = b'\x48'
TYPE_URI           = b'\x45'
TYPE_KEYWORD       = b'\x44'
TYPE_BOOL          = b'\x22'
TYPE_INTEGER       = b'\x21'
TYPE_ENUM          = b'\x23'
TYPE_TEXT_NO_LANG  = b'\x41'
TYPE_NAME_NO_LANG  = b'\x42'
TYPE_MIME_TYPE     = b'\x49'


def ipp_attr(tag, name, value):
    """Encode a single IPP attribute."""
    name_bytes = name.encode() if isinstance(name, str) else name
    val_bytes = value.encode() if isinstance(value, str) else value
    return (tag +
            struct.pack('>H', len(name_bytes)) + name_bytes +
            struct.pack('>H', len(val_bytes)) + val_bytes)


def ipp_integer(name, value):
    return ipp_attr(TYPE_INTEGER, name, struct.pack('>i', value))


def ipp_enum(name, value):
    return ipp_attr(TYPE_ENUM, name, struct.pack('>i', value))


def ipp_bool(name, value):
    return ipp_attr(TYPE_BOOL, name, b'\x01' if value else b'\x00')


def make_printer_attrs_response(request_id, printer_uri):
    """Build Get-Printer-Attributes response."""
    body = (
        IPP_VERSION + IPP_OK + request_id +
        TAG_OP_ATTRS +
        ipp_attr(TYPE_CHARSET,      'attributes-charset',          'utf-8') +
        ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language', 'en') +
        TAG_PRINTER_ATTRS +
        ipp_attr(TYPE_URI,          'printer-uri-supported',       printer_uri) +
        ipp_attr(TYPE_KEYWORD,      'uri-security-supported',      'none') +
        ipp_attr(TYPE_KEYWORD,      'uri-authentication-supported','none') +
        ipp_attr(TYPE_NAME_NO_LANG, 'printer-name',                'Canon LBP2900B') +
        ipp_enum(                   'printer-state',               3) +          # idle
        ipp_attr(TYPE_KEYWORD,      'printer-state-reasons',       'none') +
        ipp_attr(TYPE_KEYWORD,      'ipp-versions-supported',      '1.1') +
        ipp_attr(TYPE_KEYWORD,      'operations-supported',
                 struct.pack('>i', 0x0002) + struct.pack('>i', 0x000b)) +
        ipp_bool(                   'multiple-document-jobs-supported', False) +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-supported',   'application/pdf') +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-supported',   'application/postscript') +
        ipp_attr(TYPE_MIME_TYPE,    'document-format-default',     'application/pdf') +
        ipp_bool(                   'printer-is-accepting-jobs',   True) +
        ipp_integer(                'queued-job-count',            0) +
        ipp_attr(TYPE_TEXT_NO_LANG, 'printer-info',                'Canon LBP2900B via Termux') +
        ipp_attr(TYPE_KEYWORD,      'pdl-override-supported',      'not-attempted') +
        TAG_END
    )
    return body


def make_print_job_response(request_id, job_id=1):
    """Build Print-Job response."""
    body = (
        IPP_VERSION + IPP_OK + request_id +
        TAG_OP_ATTRS +
        ipp_attr(TYPE_CHARSET,      'attributes-charset',          'utf-8') +
        ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language', 'en') +
        TAG_JOB_ATTRS +
        ipp_integer(                'job-id',                      job_id) +
        ipp_attr(TYPE_URI,          'job-uri',
                 f'ipp://localhost:631/jobs/{job_id}') +
        ipp_enum(                   'job-state',                   5) +          # processing
        TAG_END
    )
    return body


def parse_ipp_request(data):
    """Extract operation code, request ID, and document format from IPP request."""
    if len(data) < 8:
        return None, None, None

    # version = data[0:2]  # unused
    operation = struct.unpack('>H', data[2:4])[0]
    request_id = data[4:8]

    # Scan for document-format attribute
    doc_format = 'application/pdf'
    i = 8
    while i < len(data) - 3:
        if data[i] == 0x03:  # end-of-attributes
            break
        if data[i] in (0x01, 0x02, 0x04, 0x05):  # group tags
            i += 1
            continue
        if i + 3 >= len(data):
            break
        name_len = struct.unpack('>H', data[i+1:i+3])[0]
        if i + 3 + name_len >= len(data):
            break
        name = data[i+3:i+3+name_len].decode('utf-8', errors='ignore')
        val_start = i + 3 + name_len
        if val_start + 2 >= len(data):
            break
        val_len = struct.unpack('>H', data[val_start:val_start+2])[0]
        val = data[val_start+2:val_start+2+val_len].decode('utf-8', errors='ignore')
        if name == 'document-format':
            doc_format = val
        i = val_start + 2 + val_len

    return operation, request_id, doc_format


class IPPHandler:
    """HTTP/IPP request handler."""

    def __init__(self, conn, addr, printer_handle, printer_ip):
        self.conn = conn
        self.addr = addr
        self.printer_handle = printer_handle
        self.printer_ip = printer_ip
        self.printer_uri = f'ipp://{printer_ip}:631/printers/LBP2900'

    def handle(self):
        try:
            self._process()
        except Exception as e:
            log.error(f"Handler error from {self.addr}: {e}", exc_info=True)
        finally:
            self.conn.close()

    def _recv_http(self):
        """Read full HTTP request including body."""
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = self.conn.recv(4096)
            if not chunk:
                return None, None
            raw += chunk

        header_end = raw.index(b'\r\n\r\n')
        headers_raw = raw[:header_end].decode('utf-8', errors='ignore')
        body = raw[header_end+4:]

        headers = {}
        lines = headers_raw.split('\r\n')
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
        if not request_line or not body:
            return

        method, path, _ = (request_line.split(' ') + ['', '', ''])[:3]
        log.info(f"Request: {method} {path} from {self.addr[0]}")

        if method == 'GET' and path == '/':
            self._send_http(200, b'Canon LBP2900B Print Server (Termux)\n')
            return

        if method != 'POST':
            self._send_http(405, b'Method Not Allowed')
            return

        # Parse IPP
        operation, request_id, doc_format = parse_ipp_request(body)
        if operation is None:
            self._send_http(400, b'Bad IPP request')
            return

        op_name = {0x0002: 'Print-Job', 0x000b: 'Get-Printer-Attributes'}.get(operation, f'0x{operation:04x}')
        log.info(f"IPP operation: {op_name}")

        if operation == 0x000b:  # Get-Printer-Attributes
            response = make_printer_attrs_response(request_id, self.printer_uri)
            self._send_ipp(response)

        elif operation == 0x0002:  # Print-Job
            # Extract document data (after IPP attributes end-tag 0x03)
            end_tag_pos = body.find(b'\x03', 8)
            if end_tag_pos == -1:
                self._send_http(400, b'Malformed IPP request')
                return

            doc_data = body[end_tag_pos+1:]
            if not doc_data:
                log.warning("No document data in Print-Job request")
                self._send_http(400, b'No document data')
                return

            log.info(f"Received document: {len(doc_data)} bytes, format: {doc_format}")

            # Send accepted response immediately
            response = make_print_job_response(request_id, job_id=int(time.time()) % 9999)
            self._send_ipp(response)

            # Process print job in background thread
            t = threading.Thread(
                target=self._print_job,
                args=(doc_data, doc_format),
                daemon=True
            )
            t.start()

        else:
            # Unsupported operation — return server-error-operation-not-supported
            response = (
                IPP_VERSION +
                b'\x05\x06' +  # server-error-operation-not-supported
                request_id +
                TAG_OP_ATTRS +
                ipp_attr(TYPE_CHARSET, 'attributes-charset', 'utf-8') +
                ipp_attr(TYPE_NATURAL_LANG, 'attributes-natural-language', 'en') +
                TAG_END
            )
            self._send_ipp(response)

    def _print_job(self, doc_data, doc_format):
        """Render and send print job to printer."""
        try:
            capt_data = render_to_capt(doc_data, doc_format)
            send_to_printer(capt_data, self.printer_handle)
        except Exception as e:
            log.error(f"Print job failed: {e}", exc_info=True)

    def _send_http(self, status, body, content_type='text/plain'):
        response = (
            f'HTTP/1.1 {status} {"OK" if status==200 else "Error"}\r\n'
            f'Content-Type: {content_type}\r\n'
            f'Content-Length: {len(body)}\r\n'
            f'Connection: close\r\n'
            f'\r\n'
        ).encode() + body
        self.conn.sendall(response)

    def _send_ipp(self, ipp_body):
        http_header = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Type: application/ipp\r\n'
            f'Content-Length: {len(ipp_body)}\r\n'
            'Connection: close\r\n'
            '\r\n'
        ).encode()
        self.conn.sendall(http_header + ipp_body)


# ── Main ──────────────────────────────────────────────────────────────────────

def get_wifi_ip():
    """Get the WiFi interface IP address."""
    try:
        result = subprocess.run(['ip', 'addr', 'show', 'wlan0'],
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('inet ') and not '127.' in line:
                return line.split()[1].split('/')[0]
    except Exception:
        pass
    # Fallback
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '0.0.0.0'


def main():
    log.info("=" * 50)
    log.info("Canon LBP2900B Termux Print Server")
    log.info("=" * 50)

    # Open printer via termux-usb fd
    log.info("Opening printer via USB...")
    printer_handle, usb_ctx = get_printer_device()
    log.info("Printer opened successfully")

    wifi_ip = get_wifi_ip()
    port = 631

    log.info(f"WiFi IP: {wifi_ip}")
    log.info(f"Printer URI: ipp://{wifi_ip}:{port}/printers/LBP2900")
    log.info("")
    log.info("Add this printer on other devices:")
    log.info(f"  Windows : http://{wifi_ip}:{port}/printers/LBP2900")
    log.info(f"  Linux   : lpadmin -p LBP2900 -v ipp://{wifi_ip}:{port}/printers/LBP2900 -E")
    log.info(f"  Android : NokoPrint > Network > IPP > {wifi_ip}:{port}/printers/LBP2900")
    log.info("")

    # Start IPP/HTTP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind(('0.0.0.0', port))
    except PermissionError:
        log.warning(f"Port {port} requires root. Trying port 6310...")
        port = 6310
        server.bind(('0.0.0.0', port))
        log.info(f"Listening on port {port} instead")

    server.listen(10)
    log.info(f"IPP server listening on 0.0.0.0:{port}")
    log.info("Waiting for print jobs...")

    try:
        while True:
            conn, addr = server.accept()
            log.info(f"Connection from {addr[0]}:{addr[1]}")
            handler = IPPHandler(conn, addr, printer_handle, wifi_ip)
            t = threading.Thread(target=handler.handle, daemon=True)
            t.start()
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
