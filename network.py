#!/usr/bin/env python3
"""Lõi mạng Peer-to-Peer (P2P) kết hợp UDP + TCP cho ứng dụng LAN Chat & File Sharing.

Mô hình đa luồng (khớp bảng 5.1 trong đề bài):
1. Main thread         : Chạy giao diện (lanchat.py).
2. UDP broadcaster     : Gửi HELLO|<name>|<tcp_port>|<status> mỗi 3 giây + kiểm tra timeout 10 giây.
3. UDP listener        : Lắng nghe UDP port 9999 để cập nhật danh sách peer online/away.
4. TCP acceptor        : Lắng nghe cổng TCP riêng, phân luồng kết nối CHAT và FILE_OFFER.
5. TCP chat threads    : Mỗi peer đang chat có 1 luồng nhận + 1 luồng gửi FIFO (queue.Queue).
6. File transfer thread: Mỗi lần gửi/nhận tệp chạy trên 1 kết nối TCP & luồng riêng -> không chặn chat.
"""
import os
import queue
import socket
import threading
import time
import uuid
from datetime import datetime

from protocol import (
    CHUNK_SIZE,
    DEFAULT_BCAST,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
    MAX_FILE_SIZE,
    PEER_TIMEOUT,
    UDP_BROADCAST_INTERVAL,
    VALID_STATUSES,
    ProtocolError,
    decode_udp_hello,
    encode_udp_hello,
    recv_json,
    send_json,
)

CONNECT_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 10.0
FILE_SOCKET_TIMEOUT = 20.0


def now_str():
    """Trả về chuỗi giờ:phút hiện tại (HH:MM)."""
    return datetime.now().strftime("%H:%M")


def safe_close(sock):
    """Đóng socket an toàn, bỏ qua lỗi nếu socket đã đóng trước đó."""
    if not sock:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def sanitize_filename(raw_name):
    """Làm sạch tên tệp: chống path traversal (../../) và ký tự cấm trên Windows."""
    base = os.path.basename(str(raw_name)).strip() or "file.bin"
    cleaned = "".join(c if c not in '<>:"/\\|?*' else "_" for c in base)
    return cleaned or "file.bin"


def unique_download_path(downloads_dir, filename):
    """Tạo đường dẫn lưu trong downloads/ không ghi đè tệp cũ đã tồn tại."""
    safe_name = sanitize_filename(filename)
    stem, ext = os.path.splitext(safe_name)
    candidate = os.path.join(downloads_dir, safe_name)
    counter = 1
    while os.path.exists(candidate) or os.path.exists(candidate + ".part"):
        candidate = os.path.join(downloads_dir, f"{stem} ({counter}){ext}")
        counter += 1
    return candidate


def find_free_tcp_port(start_port=DEFAULT_TCP_PORT, max_tries=50):
    """Tìm cổng TCP còn trống bắt đầu từ start_port (tiện khi chạy nhiều bản trên 1 máy)."""
    for port in range(start_port, start_port + max_tries):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return start_port


class PeerNode:
    """Một nút ngang hàng (Peer) vừa làm TCP Server lắng nghe, vừa làm TCP/UDP Client."""

    def __init__(
        self,
        name,
        tcp_port=DEFAULT_TCP_PORT,
        udp_port=DEFAULT_UDP_PORT,
        bcast_addr=DEFAULT_BCAST,
        downloads_dir=None,
        on_event=None,
        broadcast_interval=UDP_BROADCAST_INTERVAL,
        peer_timeout=PEER_TIMEOUT,
    ):
        self.name = name.replace("|", "_").strip()
        self.instance_id = uuid.uuid4().hex[:6]  # Mã tiến trình duy nhất tránh trùng tên + cổng trên LAN
        self.tcp_port = int(tcp_port)
        self.udp_port = int(udp_port)
        self.bcast_addr = bcast_addr.strip() or DEFAULT_BCAST
        self.broadcast_interval = float(broadcast_interval)
        self.peer_timeout = float(peer_timeout)

        if downloads_dir is None:
            downloads_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "downloads"
            )
        self.downloads_dir = downloads_dir
        os.makedirs(self.downloads_dir, exist_ok=True)

        self.on_event = on_event or (lambda event_type, payload: None)

        # Trạng thái chia sẻ giữa các luồng (bảo vệ bằng self.lock)
        self.lock = threading.Lock()
        self.status = "online"
        self.running = False
        self.peers = {}          # {peer_key: {peer_key, name, ip, tcp_port, status, instance_id, last_seen}}
        self.chat_sessions = {}  # {peer_key: {sock, queue, thread, name, direction, peer_instance_id}}

        # Khóa riêng cho thao tác kết nối từng peer, hàng đợi tin chờ trước khi nối TCP & tệp tải về
        self._connect_locks = {}
        self._pending_outbox = {}    # {peer_key: [(packet, msg_id), ...]}
        self._connecting_peers = set()
        self._download_lock = threading.Lock()
        self._status_event = threading.Event()
        self._bcast_error_reported = False
        self._transfer_seq = 0
        self.tcp_sock = None
        self.udp_sock = None

    def _emit(self, event_type, payload):
        """Gửi sự kiện lên tầng giao diện (luôn gọi NGOÀI self.lock để tránh deadlock)."""
        try:
            self.on_event(event_type, payload)
        except Exception:
            pass

    def _next_transfer_id(self):
        with self.lock:
            self._transfer_seq += 1
            return f"tf_{self._transfer_seq}"

    # ==================== KHỞI ĐỘNG & DỪNG PEER NODE ====================
    def start(self):
        """Kiểm tra địa chỉ broadcast, mở socket TCP + UDP và khởi chạy 3 luồng nền cố định."""
        try:
            socket.inet_aton(self.bcast_addr)
        except OSError as exc:
            raise OSError(
                f"Địa chỉ UDP Broadcast không hợp lệ: '{self.bcast_addr}'."
            ) from exc

        # 1. Tạo TCP listening socket (không bật SO_REUSEADDR trên Windows để tránh trùng cổng TCP)
        tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_sock.bind(("", self.tcp_port))
        tcp_sock.listen(16)
        tcp_sock.settimeout(1.0)
        self.tcp_port = tcp_sock.getsockname()[1]

        # 2. Tạo UDP broadcast socket (bật SO_REUSEADDR trước bind để nhiều app dùng chung port 9999)
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp_sock.bind(("", self.udp_port))
            udp_sock.settimeout(1.0)
        except OSError:
            safe_close(tcp_sock)
            safe_close(udp_sock)
            raise

        self.tcp_sock = tcp_sock
        self.udp_sock = udp_sock
        self.running = True
        self._bcast_error_reported = False
        self._status_event.clear()

        # Truyền trực tiếp biến cục bộ udp_sock / tcp_sock vào luồng để tránh AttributeError khi stop()
        threading.Thread(
            target=self._udp_listener_loop, args=(udp_sock,), daemon=True
        ).start()
        threading.Thread(
            target=self._udp_broadcaster_loop, args=(udp_sock,), daemon=True
        ).start()
        threading.Thread(
            target=self._tcp_acceptor_loop, args=(tcp_sock,), daemon=True
        ).start()

    def stop(self):
        """Gửi BYE cho các cuộc chat đang mở và đóng gọn mọi socket."""
        if not self.running:
            return
        self.running = False
        self._status_event.set()

        with self.lock:
            active_keys = list(self.chat_sessions.keys())
        for peer_key in active_keys:
            self.leave_chat(peer_key, notify_ui=False)

        tcp_sock = self.tcp_sock
        udp_sock = self.udp_sock
        self.tcp_sock = None
        self.udp_sock = None
        safe_close(tcp_sock)
        safe_close(udp_sock)

    # ==================== QUẢN LÝ TRẠNG THÁI & DANH SÁCH PEER ====================
    def set_status(self, new_status):
        """Đổi trạng thái (online/away) và phát gói UDP HELLO ngay lập tức."""
        new_status = new_status.strip().lower()
        if new_status not in VALID_STATUSES:
            return False
        with self.lock:
            self.status = new_status
        # Đánh thức luồng UDP broadcaster gửi ngay không phải chờ hết 3 giây
        self._status_event.set()
        return True

    def get_online_peers(self):
        """Trả về bản sao danh sách các peer đang online (sắp xếp theo tên)."""
        with self.lock:
            peers_copy = [dict(p) for p in self.peers.values()]
        peers_copy.sort(key=lambda p: (p["name"].lower(), p["peer_key"]))
        return peers_copy

    def find_peer(self, query):
        """Tìm peer theo peer_key, số thứ tự (1-based) hoặc tên (không phân biệt hoa/thường)."""
        peers = self.get_online_peers()
        q = str(query).strip()
        if not q:
            return None
        for p in peers:
            if p["peer_key"] == q:
                return p
        if q.isdigit():
            idx = int(q) - 1
            if 0 <= idx < len(peers):
                return peers[idx]
        for p in peers:
            if p["name"].lower() == q.lower():
                return p
        return None

    def _resolve_peer_key(self, ip, tcp_port, sender_name, peer_instance_id=""):
        """Ánh xạ địa chỉ TCP sang đúng peer_key trong bảng UDP (ưu tiên instance_id để không nhầm)."""
        direct_key = f"{ip}:{tcp_port}"
        with self.lock:
            if direct_key in self.peers:
                if peer_instance_id and not self.peers[direct_key].get("instance_id"):
                    self.peers[direct_key]["instance_id"] = peer_instance_id
                return direct_key
            # Nếu máy có nhiều card mạng (UDP đi Wi-Fi, TCP đi loopback), ghép theo instance_id
            if peer_instance_id:
                for key, info in self.peers.items():
                    if info.get("instance_id") == peer_instance_id:
                        return key
            else:
                for key, info in self.peers.items():
                    if info["tcp_port"] == tcp_port and info["name"].lower() == sender_name.lower():
                        return key
            # Nếu TCP đến trước gói UDP đầu tiên -> tạo bản ghi tạm để nhận tin ngay
            self.peers[direct_key] = {
                "peer_key": direct_key,
                "name": sender_name,
                "ip": ip,
                "tcp_port": tcp_port,
                "status": "online",
                "instance_id": peer_instance_id,
                "last_seen": time.monotonic(),
            }
        self._emit("peers_updated", self.get_online_peers())
        return direct_key

    # ==================== LUỒNG UDP BROADCAST & DISCOVERY ====================
    def _send_udp_presence(self, udp_sock):
        if not udp_sock or not self.running:
            return
        with self.lock:
            current_status = self.status
        packet = encode_udp_hello(
            self.name, self.tcp_port, current_status, self.instance_id
        )
        try:
            udp_sock.sendto(packet, (self.bcast_addr, self.udp_port))
            self._bcast_error_reported = False
        except OSError as exc:
            if self.running and not self._bcast_error_reported:
                self._bcast_error_reported = True
                self._emit(
                    "notice",
                    f"Lỗi phát UDP broadcast tới {self.bcast_addr}:{self.udp_port}: {exc}",
                )

    def _udp_broadcaster_loop(self, udp_sock):
        """Luồng 2: Quảng bá HELLO mỗi 3 giây và kiểm tra peer timeout quá 10 giây."""
        last_sent = 0.0
        while self.running:
            now = time.monotonic()
            if (now - last_sent >= self.broadcast_interval) or self._status_event.is_set():
                self._status_event.clear()
                self._send_udp_presence(udp_sock)
                last_sent = time.monotonic()

            self._check_peer_timeouts()
            # Ngủ tối đa 0.5s (hoặc tỉnh ngay khi đổi trạng thái/dừng app)
            self._status_event.wait(timeout=min(0.5, self.broadcast_interval))

    def _check_peer_timeouts(self):
        """Xóa các peer quá PEER_TIMEOUT giây (10s) không nhận được gói UDP HELLO."""
        now = time.monotonic()
        expired = []
        with self.lock:
            for key, info in list(self.peers.items()):
                if now - info["last_seen"] > self.peer_timeout:
                    expired.append(self.peers.pop(key))

        if expired:
            for info in expired:
                self._emit("notice", f"{info['name']} ({info['peer_key']}) đã offline.")
            self._emit("peers_updated", self.get_online_peers())

    def _udp_listener_loop(self, udp_sock):
        """Luồng 3: Lắng nghe gói UDP HELLO từ các peer khác trên mạng LAN."""
        while self.running:
            try:
                data, addr = udp_sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            decoded = decode_udp_hello(data)
            if not decoded:
                continue

            name, tcp_port, status, peer_instance_id = decoded
            # Bỏ qua gói broadcast của chính tiến trình này (dùng instance_id nên 2 máy trùng tên vẫn thấy nhau)
            if peer_instance_id:
                if peer_instance_id == self.instance_id:
                    continue
            elif name == self.name and tcp_port == self.tcp_port:
                continue

            peer_key = f"{addr[0]}:{tcp_port}"
            is_new = False
            status_changed = False

            with self.lock:
                existing = self.peers.get(peer_key)
                if existing is None:
                    self.peers[peer_key] = {
                        "peer_key": peer_key,
                        "name": name,
                        "ip": addr[0],
                        "tcp_port": tcp_port,
                        "status": status,
                        "instance_id": peer_instance_id,
                        "last_seen": time.monotonic(),
                    }
                    is_new = True
                else:
                    if existing["status"] != status or existing["name"] != name:
                        status_changed = True
                    existing["name"] = name
                    existing["status"] = status
                    if peer_instance_id:
                        existing["instance_id"] = peer_instance_id
                    existing["last_seen"] = time.monotonic()

            if is_new:
                self._emit("notice", f"{name} ({peer_key}) đã online ({status}).")
                self._emit("peers_updated", self.get_online_peers())
            elif status_changed:
                self._emit("notice", f"{name} đã chuyển trạng thái sang {status}.")
                self._emit("peers_updated", self.get_online_peers())

    # ==================== LUỒNG TCP ACCEPTOR & PHÂN LOẠI KẾT NỐI ====================
    def _tcp_acceptor_loop(self, tcp_sock):
        """Luồng 4: Chấp nhận các kết nối TCP đến từ peer khác."""
        while self.running:
            try:
                conn, addr = tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_incoming_tcp, args=(conn, addr), daemon=True
            ).start()

    def _handle_incoming_tcp(self, conn, addr):
        """Đọc gói bắt tay đầu tiên để phân biệt kết nối CHAT hay truyền FILE."""
        try:
            conn.settimeout(HANDSHAKE_TIMEOUT)
            header = recv_json(conn)
            if not header:
                safe_close(conn)
                return

            pkt_type = header.get("type")
            sender_name = str(header.get("sender", f"Peer_{addr[1]}")).strip() or f"Peer_{addr[1]}"
            peer_tcp_port = int(header.get("tcp_port", addr[1]))
            peer_instance_id = str(header.get("instance_id", "")).strip()
            peer_key = self._resolve_peer_key(
                addr[0], peer_tcp_port, sender_name, peer_instance_id
            )

            if pkt_type == "HELLO_TCP":
                conn.settimeout(None)
                accepted = self._register_chat_session(
                    peer_key, conn, sender_name, direction="in", peer_instance_id=peer_instance_id
                )
                if not accepted:
                    # Trường hợp 2 bên mở kết nối song song và kết nối chủ động đã được giữ lại
                    safe_close(conn)
                    return
                self._emit(
                    "chat_connected",
                    {
                        "peer_key": peer_key,
                        "name": sender_name,
                        "ip": addr[0],
                        "tcp_port": peer_tcp_port,
                    },
                )
                self._chat_receive_loop(peer_key, conn, sender_name)
            elif pkt_type == "FILE_OFFER":
                conn.settimeout(FILE_SOCKET_TIMEOUT)
                self._receive_file_stream(conn, peer_key, sender_name, header)
            else:
                safe_close(conn)
        except (OSError, ProtocolError, ValueError):
            safe_close(conn)

    # ==================== QUẢN LÝ PHIÊN CHAT TCP (NHIỀU PEER) ====================
    def _get_connect_lock(self, peer_key):
        """Trả về khóa kết nối riêng cho từng peer (chống mở 2 socket trùng khi gửi nhanh)."""
        with self.lock:
            lock = self._connect_locks.get(peer_key)
            if lock is None:
                lock = threading.Lock()
                self._connect_locks[peer_key] = lock
            return lock

    def is_chat_connected(self, peer_key):
        with self.lock:
            return peer_key in self.chat_sessions

    def _register_chat_session(
        self, peer_key, sock, peer_name, direction="out", peer_instance_id=""
    ):
        """Lưu phiên chat + xử lý xung đột kết nối đồng thời (tie-breaker theo instance_id).

        Trả về True nếu socket mới được giữ lại, False nếu socket cũ thắng phân xử.
        """
        send_queue = queue.Queue()
        send_thread = threading.Thread(
            target=self._chat_send_loop,
            args=(peer_key, sock, send_queue),
            daemon=True,
        )
        old_session = None

        with self.lock:
            existing = self.chat_sessions.get(peer_key)
            if (
                existing
                and existing["sock"] is not sock
                and existing["direction"] != direction
                and peer_instance_id
                and existing.get("peer_instance_id") == peer_instance_id
            ):
                # Luật phân xử đồng nhất ở cả 2 bên: luôn giữ socket do peer có instance_id nhỏ hơn khởi tạo
                new_wins = (
                    (direction == "out")
                    if (self.instance_id < peer_instance_id)
                    else (direction == "in")
                )
                if not new_wins:
                    return False

            old_session = existing
            # Chuyển các tin nhắn đang chờ (nếu người dùng bấm gửi nhanh trước khi nối TCP xong) theo đúng thứ tự FIFO
            pending_items = self._pending_outbox.pop(peer_key, [])
            for item in pending_items:
                send_queue.put(item)

            self.chat_sessions[peer_key] = {
                "sock": sock,
                "queue": send_queue,
                "thread": send_thread,
                "name": peer_name,
                "direction": direction,
                "peer_instance_id": peer_instance_id,
            }

        # Nếu thay thế phiên cũ, chuyển mọi tin nhắn chưa kịp gửi sang hàng đợi mới
        if old_session and old_session["sock"] is not sock:
            while True:
                try:
                    pending = old_session["queue"].get_nowait()
                except queue.Empty:
                    break
                if pending is not None and pending[0].get("type") != "BYE":
                    send_queue.put(pending)
            old_session["queue"].put(None)
            safe_close(old_session["sock"])

        send_thread.start()
        return True

    def _remove_chat_session(self, peer_key, sock):
        """Gỡ phiên chat đúng với socket hiện tại (tránh luồng cũ xóa nhầm kết nối mới)."""
        session = None
        with self.lock:
            current = self.chat_sessions.get(peer_key)
            if current and current["sock"] is sock:
                session = self.chat_sessions.pop(peer_key)
        if session:
            session["queue"].put(None)
        safe_close(sock)
        return session is not None

    def connect_chat(self, peer_key):
        """Mở kết nối TCP chat trực tiếp tới 1 peer (có khóa theo peer tránh mở trùng)."""
        with self._get_connect_lock(peer_key):
            with self.lock:
                if peer_key in self.chat_sessions:
                    return True, None
                peer_info = self.peers.get(peer_key)

            if not peer_info:
                return False, "Người dùng không còn trực tuyến."

            ip = peer_info["ip"]
            tcp_port = peer_info["tcp_port"]
            peer_name = peer_info["name"]
            peer_instance_id = peer_info.get("instance_id", "")

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect((ip, tcp_port))
                send_json(
                    sock,
                    {
                        "type": "HELLO_TCP",
                        "sender": self.name,
                        "tcp_port": self.tcp_port,
                        "instance_id": self.instance_id,
                    },
                )
                sock.settimeout(None)
            except (OSError, ProtocolError) as exc:
                safe_close(sock)
                return False, f"Không thể kết nối tới {peer_name} ({ip}:{tcp_port}): {exc}"

            kept = self._register_chat_session(
                peer_key, sock, peer_name, direction="out", peer_instance_id=peer_instance_id
            )
            if not kept:
                # Kết nối đến từ peer đã thắng phân xử -> dùng chung phiên đó
                safe_close(sock)
                return True, None

            threading.Thread(
                target=self._chat_receive_loop,
                args=(peer_key, sock, peer_name),
                daemon=True,
            ).start()

        self._emit(
            "chat_connected",
            {"peer_key": peer_key, "name": peer_name, "ip": ip, "tcp_port": tcp_port},
        )
        return True, None

    def leave_chat(self, peer_key, notify_ui=True):
        """Gửi gói BYE và đóng kết nối TCP với peer hiện tại."""
        with self.lock:
            self._pending_outbox.pop(peer_key, None)
            session = self.chat_sessions.pop(peer_key, None)
        if not session:
            return

        session["queue"].put(({"type": "BYE"}, None))
        session["queue"].put(None)
        if session["thread"].is_alive():
            session["thread"].join(timeout=0.25)
        safe_close(session["sock"])

        if notify_ui:
            self._emit(
                "chat_disconnected",
                {
                    "peer_key": peer_key,
                    "name": session["name"],
                    "reason": "Bạn đã đóng cuộc trò chuyện.",
                },
            )

    def enqueue_message(self, peer_key, content, msg_id=None):
        """Xếp tin nhắn ngay trên luồng gọi (không chặn, đúng 100% thứ tự FIFO kể cả khi chưa nối TCP)."""
        packet = {
            "type": "MSG",
            "content": content,
            "msg_id": msg_id,
            "time": now_str(),
        }
        need_connect = False
        with self.lock:
            session = self.chat_sessions.get(peer_key)
            if session:
                session["queue"].put((packet, msg_id))
                return True
            self._pending_outbox.setdefault(peer_key, []).append((packet, msg_id))
            if peer_key not in self._connecting_peers:
                self._connecting_peers.add(peer_key)
                need_connect = True

        if need_connect:
            threading.Thread(
                target=self._connect_and_flush, args=(peer_key,), daemon=True
            ).start()
        return True

    def _connect_and_flush(self, peer_key):
        """Kết nối TCP 1 lần duy nhất cho các tin nhắn đang chờ trong _pending_outbox."""
        ok, err = self.connect_chat(peer_key)
        failed_items = []
        with self.lock:
            self._connecting_peers.discard(peer_key)
            if ok and peer_key in self.chat_sessions:
                # Đẩy nốt mọi tin nhắn mới vào (nếu có) đúng thứ tự FIFO
                session_q = self.chat_sessions[peer_key]["queue"]
                for item in self._pending_outbox.pop(peer_key, []):
                    session_q.put(item)
            else:
                failed_items = self._pending_outbox.pop(peer_key, [])

        for _, mid in failed_items:
            if mid is not None:
                self._emit(
                    "msg_failed",
                    {
                        "peer_key": peer_key,
                        "msg_id": mid,
                        "error": err or "Không thể kết nối tới người nhận.",
                    },
                )

    def send_message(self, peer_key, content, msg_id=None):
        """Gửi tin nhắn đồng bộ (kết nối nếu cần rồi đẩy vào hàng đợi FIFO)."""
        with self.lock:
            session = self.chat_sessions.get(peer_key)

        if not session:
            ok, err = self.connect_chat(peer_key)
            if not ok:
                self._emit(
                    "msg_failed",
                    {"peer_key": peer_key, "msg_id": msg_id, "error": err},
                )
                return False
            with self.lock:
                session = self.chat_sessions.get(peer_key)

        if not session:
            self._emit(
                "msg_failed",
                {
                    "peer_key": peer_key,
                    "msg_id": msg_id,
                    "error": "Mất kết nối tới người nhận.",
                },
            )
            return False

        packet = {
            "type": "MSG",
            "content": content,
            "msg_id": msg_id,
            "time": now_str(),
        }
        session["queue"].put((packet, msg_id))
        return True

    def _chat_send_loop(self, peer_key, sock, send_queue):
        """Luồng gửi FIFO của 1 phiên chat: đảm bảo thứ tự tin nhắn & không trộn byte."""
        while True:
            item = send_queue.get()
            if item is None:
                break
            packet, msg_id = item
            try:
                send_json(sock, packet)
            except (OSError, ProtocolError) as exc:
                if msg_id is not None:
                    self._emit(
                        "msg_failed",
                        {"peer_key": peer_key, "msg_id": msg_id, "error": str(exc)},
                    )
                self._remove_chat_session(peer_key, sock)
                break
            if packet.get("type") == "BYE":
                break

    def _chat_receive_loop(self, peer_key, sock, peer_name):
        """Luồng 5: Nhận tin nhắn liên tục từ 1 peer qua socket TCP."""
        reason = f"{peer_name} đã ngắt kết nối."
        try:
            while self.running:
                header = recv_json(sock)
                if not header:
                    break

                pkt_type = header.get("type")
                if pkt_type == "BYE":
                    reason = f"{peer_name} đã rời cuộc trò chuyện."
                    break
                elif pkt_type == "MSG":
                    content = header.get("content", "")
                    msg_id = header.get("msg_id")
                    msg_time = header.get("time") or now_str()

                    # Gửi xác nhận ACK lại cho người gửi (để hiển thị ✓ Đã gửi)
                    if msg_id is not None:
                        with self.lock:
                            session = self.chat_sessions.get(peer_key)
                        if session:
                            session["queue"].put(({"type": "ACK", "msg_id": msg_id}, None))

                    self._emit(
                        "msg_received",
                        {
                            "peer_key": peer_key,
                            "sender": peer_name,
                            "content": str(content),
                            "time": msg_time,
                        },
                    )
                elif pkt_type == "ACK":
                    self._emit(
                        "msg_ack",
                        {"peer_key": peer_key, "msg_id": header.get("msg_id")},
                    )
        except (OSError, ProtocolError):
            pass
        finally:
            removed = self._remove_chat_session(peer_key, sock)
            if removed and self.running:
                self._emit(
                    "chat_disconnected",
                    {"peer_key": peer_key, "name": peer_name, "reason": reason},
                )

    # ==================== LUỒNG TRUYỀN TỆP TCP RIÊNG (CÓ % & XÓA TỆP DỞ) ====================
    def send_file(self, peer_key, filepath, is_image=False):
        """Khởi chạy 1 luồng riêng để gửi tệp (không làm nghẽn luồng chat)."""
        transfer_id = self._next_transfer_id()
        threading.Thread(
            target=self._send_file_worker,
            args=(transfer_id, peer_key, filepath, is_image),
            daemon=True,
        ).start()
        return transfer_id

    def _send_file_worker(self, transfer_id, peer_key, filepath, is_image):
        """Luồng 6 (gửi): Mở kết nối TCP riêng, gửi tệp theo khối 64 KB và báo tiến độ %."""
        filename = os.path.basename(filepath) if filepath else "file"

        if not filepath or not os.path.isfile(filepath):
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "direction": "send",
                    "filename": filename,
                    "filesize": 0,
                    "is_image": is_image,
                    "percent": 0,
                    "status": "error",
                    "filepath": filepath,
                    "error": f"Không tìm thấy tệp: {filepath}",
                },
            )
            return

        filesize = os.path.getsize(filepath)
        if filesize > MAX_FILE_SIZE:
            max_mb = MAX_FILE_SIZE // (1024 * 1024)
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "direction": "send",
                    "filename": filename,
                    "filesize": filesize,
                    "is_image": is_image,
                    "percent": 0,
                    "status": "error",
                    "filepath": filepath,
                    "error": f"Tệp vượt quá giới hạn {max_mb} MB.",
                },
            )
            return

        with self.lock:
            peer_info = self.peers.get(peer_key)

        if not peer_info:
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "direction": "send",
                    "filename": filename,
                    "filesize": filesize,
                    "is_image": is_image,
                    "percent": 0,
                    "status": "error",
                    "filepath": filepath,
                    "error": "Người nhận hiện không trực tuyến.",
                },
            )
            return

        ip, tcp_port = peer_info["ip"], peer_info["tcp_port"]

        def emit_state(percent, status, error=None):
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "direction": "send",
                    "filename": filename,
                    "filesize": filesize,
                    "is_image": is_image,
                    "percent": percent,
                    "status": status,
                    "filepath": filepath,
                    "error": error,
                },
            )

        emit_state(0, "running")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(FILE_SOCKET_TIMEOUT)
            sock.connect((ip, tcp_port))
            send_json(
                sock,
                {
                    "type": "FILE_OFFER",
                    "sender": self.name,
                    "tcp_port": self.tcp_port,
                    "instance_id": self.instance_id,
                    "filename": filename,
                    "filesize": filesize,
                    "is_image": bool(is_image),
                },
            )

            sent = 0
            last_percent = 0
            with open(filepath, "rb") as f:
                while sent < filesize:
                    if not self.running:
                        raise OSError("Ứng dụng đang đóng.")
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
                    percent = int(sent * 100 / filesize) if filesize > 0 else 100
                    # Cập nhật giao diện mượt khi phần trăm tăng thêm ít nhất 1%
                    if percent > last_percent and percent < 100:
                        last_percent = percent
                        emit_state(percent, "running")

            if sent != filesize:
                raise OSError("Tệp gốc bị thay đổi kích thước trong lúc đang gửi.")

            # Chờ bên nhận xác nhận đã ghi hoàn chỉnh tệp vào downloads/
            ack = recv_json(sock)
            if not ack or ack.get("type") != "FILE_OK":
                raise OSError("Bên nhận không xác nhận hoàn tất tệp.")

            emit_state(100, "done")
        except (OSError, ProtocolError) as exc:
            emit_state(0, "error", f"Gửi tệp thất bại: {exc}")
        finally:
            safe_close(sock)

    def _reserve_download_file(self, filename):
        """Chọn tên duy nhất trong downloads/ và tạo tệp .part độc quyền trong khóa đồng bộ."""
        with self._download_lock:
            os.makedirs(self.downloads_dir, exist_ok=True)
            final_path = unique_download_path(self.downloads_dir, filename)
            part_path = final_path + ".part"
            file_obj = open(part_path, "xb")
            return final_path, part_path, file_obj

    def _receive_file_stream(self, conn, peer_key, sender_name, header):
        """Luồng 6 (nhận): Lưu trực tiếp từng khối 64 KB vào downloads/, lỗi sẽ xóa tệp dở."""
        transfer_id = self._next_transfer_id()
        filename = sanitize_filename(header.get("filename", "file.bin"))
        filesize = header.get("filesize", -1)
        is_image = bool(header.get("is_image", False))

        if isinstance(filesize, bool) or not isinstance(filesize, int) or filesize < 0 or filesize > MAX_FILE_SIZE:
            safe_close(conn)
            return

        try:
            final_path, part_path, file_obj = self._reserve_download_file(filename)
        except OSError as exc:
            safe_close(conn)
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "sender": sender_name,
                    "direction": "recv",
                    "filename": filename,
                    "filesize": filesize,
                    "is_image": is_image,
                    "percent": 0,
                    "status": "error",
                    "filepath": None,
                    "error": f"Không thể tạo tệp lưu trong downloads/: {exc}",
                },
            )
            return

        saved_name = os.path.basename(final_path)

        def emit_state(percent, status, filepath=None, error=None):
            self._emit(
                "file_progress",
                {
                    "transfer_id": transfer_id,
                    "peer_key": peer_key,
                    "sender": sender_name,
                    "direction": "recv",
                    "filename": saved_name,
                    "filesize": filesize,
                    "is_image": is_image,
                    "percent": percent,
                    "status": status,
                    "filepath": filepath,
                    "error": error,
                },
            )

        emit_state(0, "running")
        received = 0
        last_percent = 0
        try:
            with file_obj as f:
                while received < filesize:
                    if not self.running:
                        raise OSError("Ứng dụng đang đóng.")
                    to_read = min(CHUNK_SIZE, filesize - received)
                    chunk = conn.recv(to_read)
                    if not chunk:
                        raise OSError("Kết nối bị đứt trước khi nhận đủ tệp.")
                    f.write(chunk)
                    received += len(chunk)
                    percent = int(received * 100 / filesize) if filesize > 0 else 100
                    if percent > last_percent and percent < 100:
                        last_percent = percent
                        emit_state(percent, "running")

            # Chỉ đổi tên từ .part sang tên chính thức khi đã nhận ĐỦ 100% byte
            os.replace(part_path, final_path)
            send_json(conn, {"type": "FILE_OK"})
            emit_state(100, "done", filepath=final_path)
        except (OSError, ProtocolError) as exc:
            # Yêu cầu 5.3: xóa tệp tải dở nếu quá trình truyền bị gián đoạn
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            emit_state(0, "error", error=f"Nhận tệp bị gián đoạn (đã xóa tệp tải dở): {exc}")
        finally:
            safe_close(conn)
