#!/usr/bin/env python3
"""Giao thức tầng ứng dụng dùng cho LAN Chat & File Sharing (UDP + TCP).

1. Giao thức UDP Discovery (1 datagram văn bản UTF-8, dễ đọc trên Wireshark):
       HELLO|<name>|<tcp_port>|<status>|<instance_id>
   Ví dụ:
       HELLO|Quan|5000|online|a1b2c3
   (Khi giải mã vẫn hỗ trợ cả dạng 4 trường không có instance_id để tương thích ngược.)

2. Giao thức TCP Framing (tiền tố độ dài 4 byte Big-Endian):
       [4 byte độ dài header (>I)] + [JSON header (UTF-8)]
   - Các gói điều khiển & chat: HELLO_TCP, MSG, ACK, BYE, FILE_OK.
   - Gói truyền tệp (chạy trên 1 kết nối TCP riêng):
       [4 byte độ dài header] + [JSON FILE_OFFER (có filesize)] + [dữ liệu nhị phân đọc theo khối 64 KB]
"""
import json
import struct

DEFAULT_UDP_PORT = 9999
DEFAULT_TCP_PORT = 5000
DEFAULT_BCAST = "255.255.255.255"
OFFLINE_BCAST = "127.255.255.255"

UDP_BROADCAST_INTERVAL = 3.0        # Gửi gói HELLO mỗi 3 giây (yêu cầu 3.1)
PEER_TIMEOUT = 10.0                 # Quá 10 giây không thấy HELLO -> offline (yêu cầu 3.1)

MAX_HEADER_SIZE = 64 * 1024         # Giới hạn JSON header 64 KB (chống tràn RAM)
MAX_FILE_SIZE = 500 * 1024 * 1024   # Hỗ trợ truyền tệp lớn tới 500 MB (streaming theo khối)
CHUNK_SIZE = 64 * 1024              # Kích thước mỗi khối đọc/ghi file: 64 KB

VALID_STATUSES = ("online", "away")


class ProtocolError(Exception):
    """Lỗi khi gói tin sai định dạng hoặc vượt quá giới hạn cho phép."""


# ==================== GIAO THỨC UDP (PEER DISCOVERY & STATUS) ====================
def encode_udp_hello(name, tcp_port, status="online", instance_id=""):
    """Đóng gói bản tin quảng bá hiện diện: HELLO|<name>|<tcp_port>|<status>|<instance_id>."""
    safe_name = str(name).replace("|", "_").strip()
    safe_status = status if status in VALID_STATUSES else "online"
    safe_id = str(instance_id).replace("|", "").strip()
    if safe_id:
        return f"HELLO|{safe_name}|{int(tcp_port)}|{safe_status}|{safe_id}".encode("utf-8")
    return f"HELLO|{safe_name}|{int(tcp_port)}|{safe_status}".encode("utf-8")


def decode_udp_hello(data):
    """Giải mã datagram UDP HELLO (hỗ trợ cả dạng 4 trường và 5 trường có instance_id).

    Trả về (name, tcp_port, status, instance_id) hoặc None nếu sai định dạng.
    """
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None

    parts = text.split("|")
    if len(parts) not in (4, 5) or parts[0] != "HELLO":
        return None

    name = parts[1].strip()
    status = parts[3].strip().lower()
    instance_id = parts[4].strip() if len(parts) == 5 else ""
    if not name or status not in VALID_STATUSES:
        return None

    try:
        tcp_port = int(parts[2])
        if not (1 <= tcp_port <= 65535):
            return None
    except ValueError:
        return None

    return name, tcp_port, status, instance_id


# ==================== GIAO THỨC TCP (FRAMING & RECVALL) ====================
def recvall(sock, n):
    """Đọc chính xác n byte từ luồng TCP (xử lý cắt gói & dính gói TCP)."""
    data = bytearray()
    while len(data) < n:
        to_read = min(CHUNK_SIZE, n - len(data))
        packet = sock.recv(to_read)
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)


def send_json(sock, header_dict):
    """Đóng gói và gửi 1 bản tin JSON có tiền tố 4 byte độ dài (>I)."""
    if not isinstance(header_dict, dict):
        raise ProtocolError("Header phải là kiểu dict.")

    header_bytes = json.dumps(header_dict, ensure_ascii=False).encode("utf-8")
    if len(header_bytes) == 0 or len(header_bytes) > MAX_HEADER_SIZE:
        raise ProtocolError("Header vượt quá kích thước cho phép.")

    length_prefix = struct.pack(">I", len(header_bytes))
    sock.sendall(length_prefix + header_bytes)


def recv_json(sock):
    """Nhận và giải mã 1 bản tin JSON hoàn chỉnh từ socket TCP.

    Trả về dict, hoặc None nếu đầu bên kia đã đóng kết nối gọn.
    Ném ProtocolError nếu gói tin sai cấu trúc hoặc vượt giới hạn kích thước.
    """
    raw_len = recvall(sock, 4)
    if not raw_len:
        return None

    header_len = struct.unpack(">I", raw_len)[0]
    if header_len == 0 or header_len > MAX_HEADER_SIZE:
        raise ProtocolError(f"Độ dài header không hợp lệ: {header_len} byte.")

    header_bytes = recvall(sock, header_len)
    if not header_bytes:
        return None

    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"Header JSON không hợp lệ: {exc}") from exc

    if not isinstance(header, dict):
        raise ProtocolError("Header JSON phải là đối tượng (dict).")

    return header
