#!/usr/bin/env python3
"""Read status from, and install official firmware on, Epson inkjets over USB.

Linux only (uses the usblp character device, e.g. /dev/usb/lp0). No third-party
dependencies. Tested on a WorkForce Pro WF-3820; see README.md before use.

    epsonfw.py info                    # firmware version + basic status (USB)
    epsonfw.py netinfo 192.168.1.86    # firmware version over SNMP (network)
    epsonfw.py extract FW.exe -o FW.rcx
    epsonfw.py flash FW.exe|FW.rcx     # enter update mode, send image, wait
"""
import argparse
import fnmatch
import io
import os
import select
import socket
import struct
import sys
import time
import zipfile

# ---------------------------------------------------------------------------
# IEEE 1284.4 ("Dot4") transport, minimal: one data channel at a time.
#
# Packet: psid u8, ssid u8, length u16be (incl. 6-byte header), credit u8,
#         control u8 (bit1 = end of message). Socket 0 is the transaction
#         channel; commands are one opcode byte, replies set bit 7 and carry a
#         result byte. Every packet sent needs one credit from the peer; the
#         credit byte in each packet grants credit back to the peer.
# ---------------------------------------------------------------------------

OP_INIT, OP_OPEN, OP_CLOSE, OP_CREDIT, OP_CREDIT_REQ, OP_EXIT, OP_SOCKET_ID = 0, 1, 2, 3, 4, 8, 9
OP_ERROR = 0x7F
D4_REVISION = 0x10
ENTER_D4 = b'\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL\n@EJL\n'


class Dot4Error(RuntimeError):
    pass


class Dot4:
    def __init__(self, fd, timeout=30.0):
        self.fd = fd
        self.timeout = timeout
        self.rx = b''
        self.credits = {0: 0}
        self.queues = {0: []}
        self._drain()
        self._write_raw(ENTER_D4)
        self._read_exact(8)  # EJL acknowledgement
        self.credits[0] = 1  # Init may be sent without granted credit
        rev = self._transact(OP_INIT, bytes([D4_REVISION]))
        if rev[:1] != bytes([D4_REVISION]):
            raise Dot4Error(f'unexpected Init reply {rev.hex()}')

    # -- raw I/O ------------------------------------------------------------
    def _drain(self):
        while select.select([self.fd], [], [], 0.2)[0]:
            if not os.read(self.fd, 65536):
                break

    def _write_raw(self, data):
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]

    def _read_exact(self, n):
        deadline = time.monotonic() + self.timeout
        while len(self.rx) < n:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                raise TimeoutError('printer did not answer')
            self.rx += os.read(self.fd, 65536)
        out, self.rx = self.rx[:n], self.rx[n:]
        return out

    # -- packets --------------------------------------------------------------
    def send(self, sid, payload, eom=True, grant=1):
        if self.credits.get(sid, 0) < 1:
            raise Dot4Error(f'no credit on socket {sid}')
        hdr = struct.pack('>BBHBB', sid, sid, 6 + len(payload), grant, 2 if eom else 0)
        self._write_raw(hdr + bytes(payload))
        self.credits[sid] -= 1

    def recv(self, sid):
        while not self.queues[sid]:
            psid, _, length, credit, _ = struct.unpack('>BBHBB', self._read_exact(6))
            payload = self._read_exact(length - 6)
            if psid in self.queues:
                self.credits[psid] += credit
                self.queues[psid].append(payload)
        return self.queues[sid].pop(0)

    def _transact(self, op, body=b''):
        self.send(0, bytes([op]) + body)
        reply = self.recv(0)
        if reply[0] == OP_ERROR:
            raise Dot4Error(f'transaction error {reply.hex()}')
        if reply[0] != op | 0x80 or reply[1] != 0:
            raise Dot4Error(f'op {op:#x} failed: {reply.hex()}')
        return reply[2:]

    # -- channel management ---------------------------------------------------
    def open(self, service):
        sid = self._transact(OP_SOCKET_ID, service.encode('ascii'))[0]
        reply = self._transact(OP_OPEN, struct.pack('>BBHHHH', sid, sid, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF))
        _, _, max_out, _, credit = struct.unpack('>BBHHH', reply[:8])
        self.credits[sid] = credit
        self.queues[sid] = []
        return Channel(self, sid, max_out)

    def close(self, sid):
        self._transact(OP_CLOSE, struct.pack('>BB', sid, sid))
        self.credits.pop(sid, None)
        self.queues.pop(sid, None)

    def grant(self, sid, n):
        self._transact(OP_CREDIT, struct.pack('>BBH', sid, sid, n))

    def request_credit(self, sid, n):
        # Ask for few credits at a time: the WF-3820 grants at most 4
        # outstanding and answers 0 to large requests (e.g. 0x100).
        got = struct.unpack('>BBH', self._transact(OP_CREDIT_REQ, struct.pack('>BBH', sid, sid, n))[:4])[2]
        self.credits[sid] += got
        return got


class Channel:
    def __init__(self, d4, sid, max_packet):
        self.d4, self.sid, self.chunk = d4, sid, max_packet - 6

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.d4.close(self.sid)

    def _need_credit(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        while self.d4.credits[self.sid] < 1:
            if self.d4.request_credit(self.sid, 4) < 1:
                if time.monotonic() > deadline:
                    raise TimeoutError('printer stopped granting credit')
                time.sleep(0.05)

    def write(self, data, progress=None):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            part = view[sent:sent + self.chunk]
            self._need_credit()
            self.d4.send(self.sid, part, eom=sent + len(part) >= len(view))
            sent += len(part)
            if progress:
                progress(sent, len(view))

    def command(self, name, arg):
        """EPSON-CTRL command: 2-letter name, u16le length, argument bytes."""
        payload = bytes([arg]) if isinstance(arg, int) else bytes(arg)
        self.write(name.encode('ascii') + struct.pack('<H', len(payload)) + payload)
        self.d4.grant(self.sid, 1)
        return self.d4.recv(self.sid)


def ctrl_text(reply, prefix):
    text = reply.decode('ascii', 'replace').rstrip('\x0c').rstrip(';')
    if not text.startswith(prefix):
        raise Dot4Error(f'unexpected reply {reply!r}')
    return text[len(prefix):]


# ---------------------------------------------------------------------------
# Firmware containers
# ---------------------------------------------------------------------------

def load_rcx(path):
    """Return (rcx bytes, header dict) from an .rcx or an Epson updater .exe."""
    data = open(path, 'rb').read()
    if not data.startswith(b'RCX\r\n'):
        with zipfile.ZipFile(io.BytesIO(data)) as exe:
            efu = next((n for n in exe.namelist() if n.lower().endswith('.efu')), None)
            if not efu:
                raise ValueError(f'{path}: no .efu inside; not an Epson firmware updater?')
            with zipfile.ZipFile(io.BytesIO(exe.read(efu))) as inner:
                rcx = next(n for n in inner.namelist() if n.lower().endswith('.rcx'))
                data = inner.read(rcx)
    end = data.index(b'\x0c') + 1
    lines = data[:end - 1].decode('ascii').split('\r\n')
    if lines[:2] != ['RCX', 'SEIKO EPSON EpsonNet Form']:
        raise ValueError(f'{path}: not an RCX firmware image')
    sections, cur = {}, ''
    for line in lines[2:]:
        if line.startswith('[') and line.endswith(']'):
            cur = line[1:-1]
            sections[cur] = {}
        elif '=' in line:
            k, v = line.split('=', 1)
            sections.setdefault(cur, {})[k] = v.strip('"')
    blocks = [int(s['5']) for n, s in sections.items() if n.startswith('Z_')]
    if len(data) - end != sum(blocks):
        raise ValueError(f'{path}: body is {len(data) - end} bytes, header says {sum(blocks)}')
    return data, end, sections


# ---------------------------------------------------------------------------
# Status decoding (subset of the ST2 reply)
# ---------------------------------------------------------------------------

INK_COLOURS = {0: 'black', 1: 'cyan', 2: 'magenta', 3: 'yellow'}


def decode_st2(reply):
    i = reply.index(b'ST2\r\n') + 5
    n = struct.unpack('<H', reply[i:i + 2])[0]
    body, j, fields = reply[i + 2:i + 2 + n], 0, {}
    while j + 2 <= len(body):
        t, ln = body[j], body[j + 1]
        fields[t] = body[j + 2:j + 2 + ln]
        j += 2 + ln
    out = {}
    if 0x01 in fields:
        out['status'] = {0: 'error', 3: 'idle', 4: 'printing'}.get(fields[0x01][0], f'code {fields[0x01][0]:#x}')
    if 0x02 in fields:
        out['error'] = f'{fields[0x02][0]:#04x}'
    if 0x0F in fields and fields[0x0F]:
        size, raw, inks = fields[0x0F][0], fields[0x0F][1:], []
        for k in range(0, len(raw) - size + 1, size):
            colour, level = raw[k + 1], raw[k + 2]
            reading = f'{level}%' if level <= 100 else f'not read ({level:#04x})'
            inks.append(f'{INK_COLOURS.get(colour, colour)}={reading}')
        out['ink'] = ', '.join(inks)
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def open_device(path):
    try:
        return os.open(path, os.O_RDWR)
    except PermissionError:
        sys.exit(f'{path}: permission denied (see README: udev rule or lp group)')
    except FileNotFoundError:
        sys.exit(f'{path}: not found; is the printer on and connected by USB?')


def query(device):
    fd = open_device(device)
    try:
        d4 = Dot4(fd)
        with d4.open('EPSON-CTRL') as c:
            version = ctrl_text(c.command('vi', 4), 'vi:04:')
            status = decode_st2(c.command('st', 1))
        return version, status
    finally:
        os.close(fd)


def cmd_info(args):
    version, status = query(args.device)
    print(f'firmware: {version}')
    for k, v in status.items():
        print(f'{k}: {v}')


def snmp_ctrl(host, name, arg, community=b'public', timeout=5.0):
    """Send an EPSON-CTRL command as an SNMPv1 GET (Epson OID 1248.1.2.2.44.1.1.2.1)."""
    def tlv(tag, body):
        n = len(body)
        ln = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
        return bytes([tag]) + ln + body

    def integer(v):
        return tlv(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), 'big', signed=True))

    def oid(parts):
        out = bytearray([parts[0] * 40 + parts[1]])
        for x in parts[2:]:
            enc = [x & 0x7F]
            x >>= 7
            while x:
                enc.append(0x80 | (x & 0x7F))
                x >>= 7
            out += bytes(reversed(enc))
        return tlv(0x06, bytes(out))

    def parse(buf, i=0):
        n, i = buf[i + 1], i + 2
        if n & 0x80:
            k = n & 0x7F
            n, i = int.from_bytes(buf[i:i + k], 'big'), i + k
        return buf[i:i + n], i + n

    arg = bytes([arg]) if isinstance(arg, int) else bytes(arg)
    target = (1, 3, 6, 1, 4, 1, 1248, 1, 2, 2, 44, 1, 1, 2, 1, ord(name[0]), ord(name[1]), len(arg) & 0xFF, len(arg) >> 8) + tuple(arg)
    pdu = tlv(0xA0, integer(1) + integer(0) + integer(0) + tlv(0x30, tlv(0x30, oid(target) + b'\x05\x00')))
    msg = tlv(0x30, integer(0) + tlv(0x04, community) + pdu)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, (host, 161))
        data = s.recv(65535)
    body, _ = parse(data)
    i = 0
    for _ in range(2):  # version, community
        _, i = parse(body, i)
    pdu_body, _ = parse(body, i)
    j = 0
    for _ in range(3):  # request id, error status, error index
        _, j = parse(pdu_body, j)
    varbinds, _ = parse(pdu_body, j)
    vb, _ = parse(varbinds)
    _, k = parse(vb)
    value, _ = parse(vb, k)
    return value.lstrip(b'\x00')


def cmd_netinfo(args):
    print('firmware:', ctrl_text(snmp_ctrl(args.host, 'vi', 4), 'vi:04:'))


def cmd_extract(args):
    data, _, sections = load_rcx(args.updater)
    open(args.output, 'wb').write(data)
    print(f'{args.output}: {sections["D"]["3"]}, {len(data)} bytes')


def wait_for_printer(device, timeout):
    """Wait for the device node to disappear (flash write + reboot) and return."""
    deadline = time.monotonic() + timeout
    while os.path.exists(device):
        if time.monotonic() > deadline:
            raise TimeoutError('printer never restarted')
        time.sleep(1)
    print('printer is writing the firmware and will restart by itself; do not power it off')
    while time.monotonic() < deadline:
        if os.path.exists(device):
            time.sleep(3)
            try:
                version, _ = query(device)
                if version != 'BUSY':
                    return version
            except (OSError, TimeoutError, Dot4Error):
                pass
        time.sleep(2)
    raise TimeoutError('printer did not come back; check its screen')


def cmd_flash(args):
    data, body_start, sections = load_rcx(args.image)
    target = sections['D']['3']
    model_mask = sections.get('A', {}).get('16', '')
    fd = open_device(args.device)
    try:
        d4 = Dot4(fd)
        with d4.open('EPSON-CTRL') as c:
            current = ctrl_text(c.command('vi', 4), 'vi:04:')
            print(f'printer firmware: {current}')
            print(f'image firmware:   {target} (model mask {model_mask})')
            if model_mask and not fnmatch.fnmatchcase(current, model_mask):
                sys.exit('image does not match this printer model; refusing')
            if not args.yes and input('Type "flash" to continue: ').strip() != 'flash':
                sys.exit('aborted')
            ctrl_text(c.command('fl', 1), 'fl:')
            state = ctrl_text(c.command('gm', 1), 'fl:01:')
            if state != 'OK':
                sys.exit(f'printer refused firmware update mode: {state}')
        print('printer is in update mode ("Preparing for firmware update"); sending image')
        body = memoryview(data)[body_start:]
        started, shown = time.monotonic(), [0]

        def progress(done, total):
            pct = done * 100 // total
            if pct >= shown[0] + 5 or done == total:
                shown[0] = pct
                print(f'  {pct:3d}%  {done / 1e6:5.1f} MB  {time.monotonic() - started:4.0f}s', flush=True)

        with d4.open('EPSON-DATA') as data_chan:
            data_chan.write(body, progress)
        with d4.open('EPSON-CTRL') as c:
            state = ctrl_text(c.command('gm', 3), 'fl:03:')
        if state != 'OK':
            sys.exit(f'printer did not accept the image: {state}')
    finally:
        os.close(fd)
    print('image accepted')
    print(f'firmware now: {wait_for_printer(args.device, args.timeout)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info', help='firmware version and status over USB')
    p.add_argument('--device', default='/dev/usb/lp0')
    p.set_defaults(func=cmd_info)
    p = sub.add_parser('netinfo', help='firmware version over SNMP')
    p.add_argument('host')
    p.set_defaults(func=cmd_netinfo)
    p = sub.add_parser('extract', help='pull the .rcx image out of an Epson updater .exe')
    p.add_argument('updater')
    p.add_argument('-o', '--output', required=True)
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser('flash', help='install an official .rcx/.exe image over USB')
    p.add_argument('image')
    p.add_argument('--device', default='/dev/usb/lp0')
    p.add_argument('--timeout', type=float, default=1200, help='seconds to wait for the restart')
    p.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
    p.set_defaults(func=cmd_flash)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
