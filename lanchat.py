#!/usr/bin/env python3
"""Ứng dụng LAN Chat & File Sharing (Peer-to-Peer) dùng TCP + UDP Socket với giao diện Tkinter.

Cách chạy nhanh:
    python lanchat.py
    python lanchat.py --name Quan --port 5001
    python lanchat.py --name Minh --port 5002 --bcast 127.255.255.255
"""
import argparse
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from network import PeerNode, find_free_tcp_port, now_str
from protocol import (
    DEFAULT_BCAST,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
    MAX_FILE_SIZE,
    OFFLINE_BCAST,
)

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

MAX_NAME_LENGTH = 30

# 24 emoji phổ biến cho bảng chọn (ký tự Unicode UTF-8 truyền trực tiếp qua gói MSG)
EMOJIS = [
    "😀", "😂", "😍", "😎", "😢", "😡",
    "🤔", "😴", "🥳", "🤗", "👍", "👎",
    "👏", "🙏", "🤝", "👋", "💖", "🔥",
    "🎉", "💯", "✅", "❌", "⭐", "🚀",
]

# Hai bảng màu tinh gọn, tiết chế cho ứng dụng desktop (Sáng & Tối)
THEMES = {
    "light": {
        "label": "Sáng",
        "bg": "#FFFFFF",
        "sidebar": "#F6F7F9",
        "surface": "#FFFFFF",
        "surface_alt": "#ECEFF3",
        "border": "#E4E7EC",
        "text": "#111827",
        "text_muted": "#667085",
        "accent": "#1D4ED8",
        "accent_hover": "#1E40AF",
        "accent_soft": "#EBF2FF",
        "out_bg": "#1D4ED8",
        "out_fg": "#FFFFFF",
        "in_bg": "#F2F4F7",
        "in_fg": "#111827",
        "in_border": "#E4E7EC",
        "sys_bg": "#F2F4F7",
        "sys_fg": "#667085",
        "bar_track": "#93C5FD",
        "bar_fill": "#FFFFFF",
        "in_bar_track": "#D1D5DB",
        "in_bar_fill": "#1D4ED8",
        "danger": "#DC2626",
        "danger_soft": "#FEF2F2",
        "danger_text": "#991B1B",
        "online": "#16A34A",
        "away": "#D97706",
        "offline": "#9CA3AF",
    },
    "dark": {
        "label": "Tối",
        "bg": "#14161A",
        "sidebar": "#1A1D23",
        "surface": "#1A1D23",
        "surface_alt": "#252A33",
        "border": "#2A2F3A",
        "text": "#F3F4F6",
        "text_muted": "#9CA3AF",
        "accent": "#3B82F6",
        "accent_hover": "#2563EB",
        "accent_soft": "#222B3B",
        "out_bg": "#2563EB",
        "out_fg": "#FFFFFF",
        "in_bg": "#22262F",
        "in_fg": "#F3F4F6",
        "in_border": "#2E3440",
        "sys_bg": "#1E222B",
        "sys_fg": "#9CA3AF",
        "bar_track": "#60A5FA",
        "bar_fill": "#FFFFFF",
        "in_bar_track": "#374151",
        "in_bar_fill": "#60A5FA",
        "danger": "#EF4444",
        "danger_soft": "#3B181B",
        "danger_text": "#FCA5A5",
        "online": "#22C55E",
        "away": "#F59E0B",
        "offline": "#6B7280",
    },
}


def format_size(num_bytes):
    """Định dạng kích thước tệp (B, KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def create_rounded_rect(canvas, x1, y1, x2, y2, radius=14, **kwargs):
    """Vẽ hình chữ nhật bo góc mượt trên Tkinter Canvas."""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + r, y1, x1 + r, y1,
        x2 - r, y1, x2 - r, y1,
        x2, y1, x2, y1 + r, x2, y1 + r,
        x2, y2 - r, x2, y2 - r,
        x2, y2, x2 - r, y2, x2 - r, y2,
        x1 + r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y2 - r,
        x1, y1 + r, x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def make_rounded_thumbnail(filepath, max_size=(230, 230), radius=12):
    """Tạo ảnh thu nhỏ bo góc bằng Pillow từ đường dẫn tệp (tự đóng file sau khi đọc)."""
    if not HAS_PIL or not filepath or not os.path.exists(filepath):
        return None
    try:
        with Image.open(filepath) as src:
            img = src.convert("RGBA")
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img.thumbnail(max_size, resample)

        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, img.size[0], img.size[1]), radius=radius, fill=255)
        img.putalpha(mask)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


def open_folder(path):
    """Mở thư mục downloads/ bằng trình quản lý tệp của hệ điều hành."""
    os.makedirs(path, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        messagebox.showerror("Không thể mở thư mục", str(exc))


def bind_tree(widget, event_name, callback):
    """Gắn cùng 1 sự kiện chuột cho widget cha và toàn bộ widget con bên trong."""
    widget.bind(event_name, callback)
    for child in widget.winfo_children():
        bind_tree(child, event_name, callback)


class LANChatApp:
    """Giao diện P2P LAN Chat & File Sharing kết nối trực tiếp với PeerNode."""

    def __init__(self, root, cli_args=None):
        self.root = root
        self.root.title("LAN Chat")
        self.root.geometry("920x660")
        self.root.minsize(720, 500)

        self.node = None
        self.in_main_screen = False
        self._is_closing = False

        # Trạng thái trò chuyện nhiều peer
        self.online_peers = {}       # {peer_key: peer_dict}
        self.known_peers = {}        # Giữ tên/IP của peer kể cả khi họ vừa offline
        self.peer_order = []         # Thứ tự peer_key đang hiển thị ở cột trái
        self.current_peer_key = None
        self.histories = {}          # {peer_key: [msg_dict, ...]}
        self.unread_counts = {}      # {peer_key: int}

        # Tra cứu nhanh để cập nhật trạng thái ACK & tiến độ % truyền file
        self._msg_seq = 0
        self.msg_by_id = {}          # {msg_id: (peer_key, msg_dict)}
        self.transfer_by_id = {}     # {transfer_id: (peer_key, msg_dict)}

        # Trạng thái giao diện
        self.current_theme_key = "light"
        self.theme = THEMES[self.current_theme_key]
        self.cursor_y = 16
        self._resize_job = None
        self.emoji_popup = None
        self.text_menu = tk.Menu(self.root, tearoff=0)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if cli_args and cli_args.name:
            tcp_port = (
                cli_args.port
                if cli_args.port is not None
                else find_free_tcp_port(DEFAULT_TCP_PORT)
            )
            ok = self.start_peer_node(
                cli_args.name, tcp_port, cli_args.udp_port, cli_args.bcast
            )
            if not ok:
                self.build_start_screen(
                    default_name=cli_args.name,
                    default_port=tcp_port,
                    default_udp=cli_args.udp_port,
                    default_bcast=cli_args.bcast,
                )
        else:
            suggested_port = (
                cli_args.port
                if (cli_args and cli_args.port is not None)
                else find_free_tcp_port(DEFAULT_TCP_PORT)
            )
            self.build_start_screen(
                default_port=suggested_port,
                default_udp=cli_args.udp_port if cli_args else DEFAULT_UDP_PORT,
                default_bcast=cli_args.bcast if cli_args else DEFAULT_BCAST,
            )

    # ==================== TIỆN ÍCH AN TOÀN LUỒNG TKINTER ====================
    def run_on_ui(self, func, *args):
        """Đẩy callback từ các luồng mạng nền về luồng chính Tkinter."""
        if not self._is_closing:
            try:
                self.root.after(0, func, *args)
            except tk.TclError:
                pass

    def next_msg_id(self):
        self._msg_seq += 1
        return self._msg_seq

    # ==================== MÀN HÌNH KHỞI ĐỘNG ====================
    def build_start_screen(
        self,
        default_name="",
        default_port=DEFAULT_TCP_PORT,
        default_udp=DEFAULT_UDP_PORT,
        default_bcast=DEFAULT_BCAST,
    ):
        t = self.theme
        self.start_frame = tk.Frame(self.root, bg=t["sidebar"])
        self.start_frame.pack(fill=tk.BOTH, expand=True)

        border_wrap = tk.Frame(self.start_frame, bg=t["border"])
        border_wrap.place(relx=0.5, rely=0.5, anchor=tk.CENTER, width=396)

        card = tk.Frame(border_wrap, bg=t["surface"], padx=28, pady=28)
        card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        tk.Label(
            card,
            text="LAN Chat",
            font=("Segoe UI", 17, "bold"),
            bg=t["surface"],
            fg=t["text"],
            anchor="w",
        ).pack(fill=tk.X)
        tk.Label(
            card,
            text="Nhắn tin và truyền tệp ngang hàng",
            font=("Segoe UI", 9),
            bg=t["surface"],
            fg=t["text_muted"],
            anchor="w",
        ).pack(fill=tk.X, pady=(2, 20))

        self.name_var = tk.StringVar(value=default_name)
        self.tcp_port_var = tk.StringVar(value=str(default_port))
        self.udp_port_var = tk.StringVar(value=str(default_udp))
        self.bcast_var = tk.StringVar(value=default_bcast)
        self.offline_mode_var = tk.BooleanVar(value=(default_bcast == OFFLINE_BCAST))

        def make_field(parent, label_text, var, focus=False):
            tk.Label(
                parent,
                text=label_text,
                font=("Segoe UI", 9),
                bg=t["surface"],
                fg=t["text_muted"],
                anchor="w",
            ).pack(fill=tk.X, pady=(0, 4))
            box = tk.Frame(parent, bg=t["border"])
            box.pack(fill=tk.X)
            entry = tk.Entry(
                box,
                textvariable=var,
                font=("Segoe UI", 10),
                bg=t["bg"],
                fg=t["text"],
                insertbackground=t["text"],
                relief=tk.FLAT,
                bd=0,
            )
            entry.pack(fill=tk.X, padx=1, pady=1, ipady=6, ipadx=6)
            if focus:
                entry.focus_set()
                entry.bind("<Return>", lambda e: self.on_start_clicked())
            return entry

        make_field(card, "Tên hiển thị", self.name_var, focus=True)

        # Hàng ngang gọn chứa Cổng TCP và Cổng UDP
        port_row = tk.Frame(card, bg=t["surface"])
        port_row.pack(fill=tk.X, pady=(12, 0))
        left_col = tk.Frame(port_row, bg=t["surface"])
        left_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        right_col = tk.Frame(port_row, bg=t["surface"])
        right_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        make_field(left_col, "Cổng TCP lắng nghe", self.tcp_port_var)
        make_field(right_col, "Cổng UDP Discovery", self.udp_port_var)

        bcast_wrap = tk.Frame(card, bg=t["surface"])
        bcast_wrap.pack(fill=tk.X, pady=(12, 0))
        make_field(bcast_wrap, "Địa chỉ UDP Broadcast", self.bcast_var)

        tk.Checkbutton(
            card,
            text=f"Chạy thử trên 1 máy / không có Wi-Fi ({OFFLINE_BCAST})",
            variable=self.offline_mode_var,
            bg=t["surface"],
            fg=t["text_muted"],
            activebackground=t["surface"],
            activeforeground=t["text"],
            selectcolor=t["bg"],
            font=("Segoe UI", 9),
            anchor="w",
            command=self.on_toggle_offline_bcast,
        ).pack(fill=tk.X, pady=(10, 0))

        tk.Button(
            card,
            text="Bắt đầu",
            font=("Segoe UI", 10, "bold"),
            bg=t["accent"],
            fg="#FFFFFF",
            activebackground=t["accent_hover"],
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            cursor="hand2",
            command=self.on_start_clicked,
        ).pack(fill=tk.X, pady=(20, 0), ipady=7)

    def on_toggle_offline_bcast(self):
        self.bcast_var.set(
            OFFLINE_BCAST if self.offline_mode_var.get() else DEFAULT_BCAST
        )

    def on_start_clicked(self):
        name = self.name_var.get().strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            messagebox.showwarning(
                "Tên không hợp lệ",
                f"Vui lòng nhập tên hiển thị (1–{MAX_NAME_LENGTH} ký tự).",
            )
            return
        try:
            tcp_port = int(self.tcp_port_var.get().strip())
            udp_port = int(self.udp_port_var.get().strip())
            if not (1 <= tcp_port <= 65535 and 1 <= udp_port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Lỗi cổng", "Cổng TCP và UDP phải là số từ 1 đến 65535.")
            return

        bcast = self.bcast_var.get().strip() or DEFAULT_BCAST
        if self.start_peer_node(name, tcp_port, udp_port, bcast):
            self.start_frame.destroy()

    def start_peer_node(self, name, tcp_port, udp_port, bcast_addr):
        """Khởi động PeerNode (mở TCP + UDP) và dựng giao diện chính."""
        node = PeerNode(
            name=name,
            tcp_port=tcp_port,
            udp_port=udp_port,
            bcast_addr=bcast_addr,
            on_event=lambda ev, payload: self.run_on_ui(
                self.handle_network_event, ev, payload
            ),
        )
        try:
            node.start()
        except OSError as exc:
            messagebox.showerror(
                "Không thể khởi động",
                f"Lỗi mở cổng TCP {tcp_port} hoặc UDP {udp_port}:\n{exc}\n\n"
                "Nếu chạy nhiều cửa sổ trên 1 máy, hãy chọn cổng TCP khác nhau (vd. 5001, 5002).",
            )
            return False

        self.node = node
        self.build_main_screen()
        self.set_notice(
            f"TCP :{node.tcp_port}  ·  UDP :{node.udp_port} ({node.bcast_addr})"
        )
        return True

    # ==================== MÀN HÌNH CHÍNH 2 CỘT ====================
    def build_main_screen(self):
        self.root.title(f"LAN Chat - {self.node.name} (:{self.node.tcp_port})")

        self.body_frame = tk.Frame(self.root)
        self.body_frame.pack(fill=tk.BOTH, expand=True)

        # ---------- CỘT TRÁI: THÔNG TIN BẢN THÂN + DANH SÁCH PEER ----------
        self.peer_sidebar = tk.Frame(self.body_frame, width=264)
        self.peer_sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.peer_sidebar.pack_propagate(False)

        self.sidebar_body = tk.Frame(self.peer_sidebar)
        self.sidebar_body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sidebar_divider = tk.Frame(self.peer_sidebar, width=1)
        self.sidebar_divider.pack(side=tk.RIGHT, fill=tk.Y)

        # Khối thông tin người dùng hiện tại (gọn ở đầu cột trái)
        self.me_header = tk.Frame(self.sidebar_body, padx=14, pady=12)
        self.me_header.pack(fill=tk.X)

        me_top = tk.Frame(self.me_header)
        me_top.pack(fill=tk.X)
        self.me_top = me_top

        self.me_name_lbl = tk.Label(
            me_top, text=self.node.name, font=("Segoe UI", 12, "bold"), anchor="w"
        )
        self.me_name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_theme = tk.Button(
            me_top,
            text="Chế độ tối",
            font=("Segoe UI", 8),
            relief=tk.FLAT,
            cursor="hand2",
            padx=7,
            pady=2,
            command=self.toggle_theme,
        )
        self.btn_theme.pack(side=tk.RIGHT)

        me_sub = tk.Frame(self.me_header)
        me_sub.pack(fill=tk.X, pady=(6, 0))
        self.me_sub = me_sub

        self.btn_status = tk.Button(
            me_sub,
            text="●  Online",
            font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            padx=7,
            pady=2,
            command=self.toggle_status,
        )
        self.btn_status.pack(side=tk.LEFT)

        self.me_port_lbl = tk.Label(
            me_sub,
            text=f"TCP :{self.node.tcp_port}",
            font=("Segoe UI", 8),
            anchor="e",
        )
        self.me_port_lbl.pack(side=tk.RIGHT)

        self.me_divider = tk.Frame(self.sidebar_body, height=1)
        self.me_divider.pack(fill=tk.X)

        # Tiêu đề danh sách peer
        self.peer_section_bar = tk.Frame(self.sidebar_body, padx=14, pady=8)
        self.peer_section_bar.pack(fill=tk.X)

        self.sidebar_title = tk.Label(
            self.peer_section_bar,
            text="TRÊN MẠNG LAN  (0)",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.sidebar_title.pack(side=tk.LEFT)

        # Danh sách peer dạng hàng tùy biến (có thanh cuộn + chấm màu trạng thái + badge chưa đọc)
        self.peer_list_wrap = tk.Frame(self.sidebar_body)
        self.peer_list_wrap.pack(fill=tk.BOTH, expand=True)

        self.peer_canvas = tk.Canvas(self.peer_list_wrap, highlightthickness=0, bd=0)
        self.peer_scrollbar = ttk.Scrollbar(
            self.peer_list_wrap, orient=tk.VERTICAL, command=self.peer_canvas.yview
        )
        self.peer_canvas.configure(yscrollcommand=self.peer_scrollbar.set)
        self.peer_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.peer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.peer_rows_frame = tk.Frame(self.peer_canvas)
        self.peer_window_id = self.peer_canvas.create_window(
            (0, 0), window=self.peer_rows_frame, anchor="nw"
        )
        self.peer_rows_frame.bind(
            "<Configure>",
            lambda e: self.peer_canvas.configure(scrollregion=self.peer_canvas.bbox("all")),
        )
        self.peer_canvas.bind(
            "<Configure>",
            lambda e: self.peer_canvas.itemconfigure(self.peer_window_id, width=e.width),
        )

        # Chân cột trái: Mở downloads/ và Thoát
        self.footer_divider = tk.Frame(self.sidebar_body, height=1)
        self.footer_divider.pack(fill=tk.X)

        self.sidebar_footer = tk.Frame(self.sidebar_body, padx=10, pady=10)
        self.sidebar_footer.pack(fill=tk.X)

        self.btn_downloads = tk.Button(
            self.sidebar_footer,
            text="Thư mục downloads",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=8,
            pady=5,
            command=lambda: open_folder(self.node.downloads_dir),
        )
        self.btn_downloads.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.btn_quit = tk.Button(
            self.sidebar_footer,
            text="Thoát",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=10,
            pady=5,
            command=self.on_close,
        )
        self.btn_quit.pack(side=tk.RIGHT)

        # ---------- CỘT PHẢI: HEADER NGƯỜI CHAT + KHUNG CHAT + NHẬP LIỆU ----------
        self.chat_area = tk.Frame(self.body_frame)
        self.chat_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.partner_bar = tk.Frame(self.chat_area, padx=18, pady=11)
        self.partner_bar.pack(fill=tk.X)

        partner_info = tk.Frame(self.partner_bar)
        partner_info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.partner_info = partner_info

        self.partner_name_lbl = tk.Label(
            partner_info,
            text="Chưa chọn cuộc trò chuyện",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        )
        self.partner_name_lbl.pack(fill=tk.X)

        self.partner_sub_lbl = tk.Label(
            partner_info,
            text="Chọn một người ở cột bên trái hoặc gõ /help để xem lệnh",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.partner_sub_lbl.pack(fill=tk.X, pady=(1, 0))

        self.btn_leave_chat = tk.Button(
            self.partner_bar,
            text="Đóng chat",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=10,
            pady=4,
            command=self.leave_current_chat,
        )

        self.partner_divider = tk.Frame(self.chat_area, height=1)
        self.partner_divider.pack(fill=tk.X)

        # Dải trạng thái mạng tinh tế (1 dòng gọn, không màu mè)
        self.notice_bar = tk.Label(
            self.chat_area,
            text="",
            font=("Segoe UI", 8),
            anchor="w",
            padx=18,
            pady=4,
        )
        self.notice_bar.pack(fill=tk.X)

        self.notice_divider = tk.Frame(self.chat_area, height=1)
        self.notice_divider.pack(fill=tk.X)

        # Khung Canvas hiển thị tin nhắn
        self.chat_container = tk.Frame(self.chat_area)
        self.chat_container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.chat_container, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(
            self.chat_container, orient=tk.VERTICAL, command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<Button-1>", lambda e: self.close_emoji_picker())
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.scroll_under_pointer(e, -2))
        self.canvas.bind_all("<Button-5>", lambda e: self.scroll_under_pointer(e, 2))

        # Thanh nhập liệu gọn gàng
        self.composer_divider = tk.Frame(self.chat_area, height=1)
        self.composer_divider.pack(fill=tk.X)

        self.bottom_bar = tk.Frame(self.chat_area, padx=14, pady=10)
        self.bottom_bar.pack(fill=tk.X)

        self.btn_img = tk.Button(
            self.bottom_bar,
            text="Ảnh",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=9,
            pady=5,
            command=self.pick_and_send_image,
        )
        self.btn_img.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_file = tk.Button(
            self.bottom_bar,
            text="Tệp",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=9,
            pady=5,
            command=self.pick_and_send_file,
        )
        self.btn_file.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_emoji = tk.Button(
            self.bottom_bar,
            text="Icon",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            padx=9,
            pady=5,
            command=self.toggle_emoji_picker,
        )
        self.btn_emoji.pack(side=tk.LEFT, padx=(0, 8))

        self.entry_border = tk.Frame(self.bottom_bar)
        self.entry_border.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.msg_var = tk.StringVar()
        self.msg_entry = tk.Entry(
            self.entry_border,
            textvariable=self.msg_var,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            bd=0,
        )
        self.msg_entry.pack(fill=tk.X, padx=1, pady=1, ipady=6, ipadx=8)
        self.msg_entry.bind("<Return>", lambda e: self.on_send_input())
        self.msg_entry.bind("<Button-1>", lambda e: self.close_emoji_picker())
        self.msg_entry.focus_set()

        self.btn_send = tk.Button(
            self.bottom_bar,
            text="Gửi",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            padx=16,
            pady=6,
            command=self.on_send_input,
        )
        self.btn_send.pack(side=tk.RIGHT)

        self.in_main_screen = True
        self.apply_theme(self.current_theme_key)
        self.refresh_peer_list()

    # ==================== THEME (SÁNG / TỐI) & TRẠNG THÁI ====================
    def toggle_theme(self):
        next_key = "dark" if self.current_theme_key == "light" else "light"
        self.apply_theme(next_key)

    def toggle_status(self):
        if not self.node:
            return
        next_status = "away" if self.node.status == "online" else "online"
        self.set_my_status(next_status)

    def set_my_status(self, new_status):
        if not self.node or not self.node.set_status(new_status):
            return
        self._style_status_button()
        self.set_notice(f"Đã chuyển trạng thái sang {new_status} (đã phát UDP).")

    def _style_status_button(self):
        t = self.theme
        status = self.node.status if self.node else "online"
        if status == "online":
            self.btn_status.configure(
                text="●  Online", fg=t["online"], bg=t["surface_alt"], activebackground=t["border"]
            )
        else:
            self.btn_status.configure(
                text="●  Away", fg=t["away"], bg=t["surface_alt"], activebackground=t["border"]
            )

    def apply_theme(self, theme_key):
        self.current_theme_key = theme_key
        self.theme = THEMES[theme_key]
        t = self.theme

        self.body_frame.configure(bg=t["bg"])

        # Cột trái
        self.peer_sidebar.configure(bg=t["sidebar"])
        self.sidebar_body.configure(bg=t["sidebar"])
        self.sidebar_divider.configure(bg=t["border"])
        self.me_header.configure(bg=t["sidebar"])
        self.me_top.configure(bg=t["sidebar"])
        self.me_sub.configure(bg=t["sidebar"])
        self.me_name_lbl.configure(bg=t["sidebar"], fg=t["text"])
        self.me_port_lbl.configure(bg=t["sidebar"], fg=t["text_muted"])
        self.btn_theme.configure(
            text="Chế độ sáng" if theme_key == "dark" else "Chế độ tối",
            bg=t["surface_alt"],
            fg=t["text_muted"],
            activebackground=t["border"],
            activeforeground=t["text"],
        )
        self._style_status_button()
        self.me_divider.configure(bg=t["border"])

        self.peer_section_bar.configure(bg=t["sidebar"])
        self.sidebar_title.configure(bg=t["sidebar"], fg=t["text_muted"])
        self.peer_list_wrap.configure(bg=t["sidebar"])
        self.peer_canvas.configure(bg=t["sidebar"])
        self.peer_rows_frame.configure(bg=t["sidebar"])

        self.footer_divider.configure(bg=t["border"])
        self.sidebar_footer.configure(bg=t["sidebar"])
        self.btn_downloads.configure(
            bg=t["surface_alt"],
            fg=t["text"],
            activebackground=t["border"],
            activeforeground=t["text"],
        )
        self.btn_quit.configure(
            bg=t["surface_alt"],
            fg=t["danger"],
            activebackground=t["danger_soft"],
            activeforeground=t["danger"],
        )

        # Cột phải
        self.chat_area.configure(bg=t["bg"])
        self.partner_bar.configure(bg=t["surface"])
        self.partner_info.configure(bg=t["surface"])
        self.partner_name_lbl.configure(bg=t["surface"], fg=t["text"])
        self.partner_sub_lbl.configure(bg=t["surface"], fg=t["text_muted"])
        self.btn_leave_chat.configure(
            bg=t["surface_alt"],
            fg=t["text"],
            activebackground=t["danger_soft"],
            activeforeground=t["danger"],
        )
        self.partner_divider.configure(bg=t["border"])

        self.notice_bar.configure(bg=t["sidebar"], fg=t["text_muted"])
        self.notice_divider.configure(bg=t["border"])

        self.chat_container.configure(bg=t["bg"])
        self.canvas.configure(bg=t["bg"])

        self.composer_divider.configure(bg=t["border"])
        self.bottom_bar.configure(bg=t["surface"])
        for btn in (self.btn_img, self.btn_file, self.btn_emoji):
            btn.configure(
                bg=t["surface_alt"],
                fg=t["text"],
                activebackground=t["border"],
                activeforeground=t["text"],
            )
        self.entry_border.configure(bg=t["border"])
        self.msg_entry.configure(bg=t["bg"], fg=t["text"], insertbackground=t["text"])
        self.btn_send.configure(
            bg=t["accent"],
            fg="#FFFFFF",
            activebackground=t["accent_hover"],
            activeforeground="#FFFFFF",
        )

        self.close_emoji_picker()
        self.refresh_peer_list()
        self.redraw_messages()

    # ==================== BẢNG CHỌN EMOJI (TRONG KHUNG CHAT) ====================
    def close_emoji_picker(self):
        if self.emoji_popup is not None:
            try:
                self.emoji_popup.destroy()
            except tk.TclError:
                pass
            self.emoji_popup = None
        try:
            self.root.unbind("<Escape>")
        except tk.TclError:
            pass

    def toggle_emoji_picker(self):
        """Mở/đóng bảng chọn 24 emoji nổi ngay trong khung chat (ổn định trên Windows Tk 8.6)."""
        if not self.in_main_screen:
            return
        if self.emoji_popup is not None and self.emoji_popup.winfo_exists():
            self.close_emoji_picker()
            return

        t = self.theme
        popup = tk.Frame(self.chat_container, bg=t["border"], bd=0)
        self.emoji_popup = popup

        grid_frame = tk.Frame(popup, bg=t["surface"], padx=6, pady=6)
        grid_frame.pack(padx=1, pady=1)

        cols = 6
        for idx, emoji_char in enumerate(EMOJIS):
            btn = tk.Button(
                grid_frame,
                text=emoji_char,
                font=("Segoe UI Emoji", 12),
                bg=t["surface"],
                fg=t["text"],
                activebackground=t["surface_alt"],
                activeforeground=t["text"],
                relief=tk.FLAT,
                cursor="hand2",
                width=2,
                command=lambda ch=emoji_char: self.insert_emoji(ch),
            )
            btn.grid(row=idx // cols, column=idx % cols, padx=2, pady=2)

        popup.place(x=14, rely=1.0, y=-8, anchor="sw")
        popup.lift()
        self.root.bind("<Escape>", lambda e: self.close_emoji_picker())

    def insert_emoji(self, emoji_char):
        self.msg_entry.insert(tk.INSERT, emoji_char)
        self.msg_entry.focus_set()
        self.close_emoji_picker()

    # ==================== XỬ LÝ SỰ KIỆN MẠNG TỪ PEERNODE ====================
    def set_notice(self, text):
        if self.in_main_screen:
            self.notice_bar.configure(text=text)

    def handle_network_event(self, event_type, payload):
        """Nhận mọi sự kiện từ PeerNode trên luồng chính Tkinter."""
        if not self.in_main_screen:
            return

        if event_type == "notice":
            self.set_notice(payload)

        elif event_type == "peers_updated":
            self.online_peers = {p["peer_key"]: p for p in payload}
            for p in payload:
                self.known_peers[p["peer_key"]] = dict(p)
            self.refresh_peer_list()
            self.update_partner_header()

        elif event_type == "chat_connected":
            peer_key = payload["peer_key"]
            self.known_peers.setdefault(peer_key, payload)
            self.histories.setdefault(peer_key, [])
            self.set_notice(
                f"Đã kết nối TCP với {payload['name']} ({payload['ip']}:{payload['tcp_port']})."
            )
            if peer_key == self.current_peer_key:
                self.update_partner_header()

        elif event_type == "chat_disconnected":
            peer_key = payload["peer_key"]
            reason = payload.get("reason", "Đã ngắt kết nối.")
            self.append_message(peer_key, {"type": "SYSTEM", "content": reason})
            self.set_notice(reason)
            if peer_key == self.current_peer_key:
                self.update_partner_header()

        elif event_type == "msg_received":
            peer_key = payload["peer_key"]
            sender = payload["sender"]
            msg = {
                "type": "TEXT",
                "sender": sender,
                "is_me": False,
                "content": payload["content"],
                "time": payload["time"],
            }
            self.append_message(peer_key, msg)

            if peer_key != self.current_peer_key:
                self.unread_counts[peer_key] = self.unread_counts.get(peer_key, 0) + 1
                unread = self.unread_counts[peer_key]
                self.set_notice(f"Tin nhắn mới từ {sender} ({unread} chưa đọc)")
                self.refresh_peer_list()

        elif event_type == "msg_ack":
            self.update_message_status(payload.get("msg_id"), "sent")

        elif event_type == "msg_failed":
            self.update_message_status(payload.get("msg_id"), "failed")
            err = payload.get("error")
            if err:
                self.set_notice(f"Gửi tin nhắn thất bại: {err}")

        elif event_type == "file_progress":
            self.handle_file_progress(payload)

    def handle_file_progress(self, payload):
        """Tạo hoặc cập nhật tiến độ % (kèm thanh tiến độ Canvas) trực tiếp trong khung chat."""
        transfer_id = payload["transfer_id"]
        peer_key = payload["peer_key"]
        direction = payload["direction"]
        status = payload["status"]
        percent = payload["percent"]
        filename = payload["filename"]
        filesize = payload["filesize"]
        is_image = payload.get("is_image", False)
        filepath = payload.get("filepath")
        error = payload.get("error")

        existing = self.transfer_by_id.get(transfer_id)
        if existing is None:
            msg = {
                "type": "IMAGE" if is_image else "FILE",
                "transfer_id": transfer_id,
                "sender": self.node.name if direction == "send" else payload.get("sender", "Peer"),
                "is_me": (direction == "send"),
                "filename": filename,
                "filesize": filesize,
                "filepath": filepath,
                "percent": percent,
                "transfer_status": status,
                "error": error,
                "time": now_str(),
            }
            self.transfer_by_id[transfer_id] = (peer_key, msg)
            self.append_message(peer_key, msg)
            if direction == "recv" and peer_key != self.current_peer_key:
                self.set_notice(f"{msg['sender']} đang gửi tệp {filename}...")
            return

        _, msg = existing
        old_status = msg.get("transfer_status")
        msg["percent"] = percent
        msg["transfer_status"] = status
        if filepath:
            msg["filepath"] = filepath
        if error:
            msg["error"] = error

        if peer_key == self.current_peer_key:
            if status == "running" and msg.get("body_canvas_id"):
                try:
                    self.canvas.itemconfigure(
                        msg["body_canvas_id"], text=self.format_file_bubble_text(msg)
                    )
                    if msg.get("bar_fill_id") and msg.get("bar_box"):
                        bx, by, bw, bh = msg["bar_box"]
                        fill_w = max(4, int(bw * max(0, min(100, percent)) / 100))
                        self.canvas.coords(
                            msg["bar_fill_id"], bx, by, bx + fill_w, by + bh
                        )
                except tk.TclError:
                    pass
            elif status != old_status:
                self.redraw_messages()

        if status == "done" and old_status != "done":
            if direction == "send":
                self.set_notice(f"Đã gửi xong tệp {filename} (100%).")
            else:
                self.set_notice(f"Đã nhận {filename} từ {msg['sender']} — lưu tại downloads/")
                if peer_key != self.current_peer_key:
                    self.unread_counts[peer_key] = self.unread_counts.get(peer_key, 0) + 1
                    self.refresh_peer_list()
        elif status == "error" and old_status != "error":
            self.set_notice(error or f"Lỗi truyền tệp {filename}.")

    # ==================== DANH SÁCH PEER DẠNG HÀNG GỌN SẠCH ====================
    def refresh_peer_list(self):
        """Vẽ lại cột danh sách peer bằng các hàng tùy biến (chấm màu + badge chưa đọc)."""
        t = self.theme
        all_keys = set(self.online_peers.keys())
        for key, count in self.unread_counts.items():
            if count > 0:
                all_keys.add(key)
        if self.current_peer_key:
            all_keys.add(self.current_peer_key)

        def sort_key(k):
            is_online = 0 if k in self.online_peers else 1
            info = self.online_peers.get(k) or self.known_peers.get(k) or {"name": k}
            return (is_online, info["name"].lower(), k)

        self.peer_order = sorted(all_keys, key=sort_key)
        self.sidebar_title.configure(text=f"TRÊN MẠNG LAN  ({len(self.online_peers)})")

        for child in self.peer_rows_frame.winfo_children():
            child.destroy()

        if not self.peer_order:
            tk.Label(
                self.peer_rows_frame,
                text="Chưa thấy ai khác trên mạng LAN.\nĐang lắng nghe UDP...",
                font=("Segoe UI", 9),
                bg=t["sidebar"],
                fg=t["text_muted"],
                justify=tk.LEFT,
                anchor="w",
                padx=14,
                pady=12,
            ).pack(fill=tk.X)
            return

        for key in self.peer_order:
            self._build_peer_row(key)

    def _build_peer_row(self, peer_key):
        t = self.theme
        online_info = self.online_peers.get(peer_key)
        info = online_info or self.known_peers.get(peer_key) or {"name": peer_key, "ip": "", "tcp_port": ""}
        is_selected = (peer_key == self.current_peer_key)
        unread = self.unread_counts.get(peer_key, 0)

        row_bg = t["accent_soft"] if is_selected else t["sidebar"]
        hover_bg = t["accent_soft"] if is_selected else t["surface_alt"]

        if online_info:
            dot_color = t["online"] if online_info["status"] == "online" else t["away"]
            sub_text = f"{online_info['ip']}:{online_info['tcp_port']}"
            if online_info["status"] == "away":
                sub_text += "  ·  away"
        else:
            dot_color = t["offline"]
            sub_text = f"{peer_key}  ·  offline"

        row = tk.Frame(self.peer_rows_frame, bg=row_bg, padx=12, pady=8, cursor="hand2")
        row.pack(fill=tk.X, padx=6, pady=1)

        # Chấm trạng thái sắc nét vẽ bằng Canvas (không dùng emoji màu lệch font)
        dot = tk.Canvas(row, width=10, height=10, bg=row_bg, highlightthickness=0, bd=0)
        dot.create_oval(1, 1, 9, 9, fill=dot_color, outline="")
        dot.pack(side=tk.LEFT, padx=(0, 9), pady=(3, 0), anchor="n")

        text_col = tk.Frame(row, bg=row_bg)
        text_col.pack(side=tk.LEFT, fill=tk.X, expand=True)

        name_lbl = tk.Label(
            text_col,
            text=info["name"],
            font=("Segoe UI", 10, "bold" if (is_selected or unread > 0) else "normal"),
            bg=row_bg,
            fg=t["text"],
            anchor="w",
        )
        name_lbl.pack(fill=tk.X)

        sub_lbl = tk.Label(
            text_col,
            text=sub_text,
            font=("Segoe UI", 8),
            bg=row_bg,
            fg=t["text_muted"],
            anchor="w",
        )
        sub_lbl.pack(fill=tk.X)

        if unread > 0:
            tk.Label(
                row,
                text=str(unread),
                font=("Segoe UI", 8, "bold"),
                bg=t["accent"],
                fg="#FFFFFF",
                padx=6,
                pady=1,
            ).pack(side=tk.RIGHT, padx=(6, 0))

        def set_bg(color):
            for w in (row, dot, text_col, name_lbl, sub_lbl):
                w.configure(bg=color)

        def on_leave(event):
            target = row.winfo_containing(event.x_root, event.y_root)
            if target is None or not str(target).startswith(str(row)):
                set_bg(row_bg)

        bind_tree(row, "<Button-1>", lambda e, k=peer_key: self.select_peer(k))
        bind_tree(row, "<Enter>", lambda e: set_bg(hover_bg))
        row.bind("<Leave>", on_leave)

    def select_peer(self, peer_key):
        """Chuyển khung chat sang peer_key, xóa số tin chưa đọc và mở TCP nếu cần."""
        self.close_emoji_picker()
        self.current_peer_key = peer_key
        self.histories.setdefault(peer_key, [])

        unread = self.unread_counts.pop(peer_key, 0)
        info = self.online_peers.get(peer_key) or self.known_peers.get(peer_key) or {"name": peer_key}
        if unread > 0:
            self.set_notice(f"Đã chuyển sang {info['name']} ({unread} tin chưa đọc).")

        self.refresh_peer_list()
        self.update_partner_header()
        self.redraw_messages()
        self.msg_entry.focus_set()

        if peer_key in self.online_peers and not self.node.is_chat_connected(peer_key):
            self.set_notice(f"Đang kết nối TCP tới {info['name']} ({peer_key})...")

            def connect_worker():
                ok, err = self.node.connect_chat(peer_key)
                if not ok and err:
                    self.run_on_ui(self.set_notice, err)
                    self.run_on_ui(
                        self.append_message,
                        peer_key,
                        {"type": "SYSTEM", "content": err},
                    )

            threading.Thread(target=connect_worker, daemon=True).start()

    def update_partner_header(self):
        if not self.current_peer_key:
            self.partner_name_lbl.configure(text="Chưa chọn cuộc trò chuyện")
            self.partner_sub_lbl.configure(
                text="Chọn một người ở cột bên trái hoặc gõ /help để xem lệnh"
            )
            self.btn_leave_chat.pack_forget()
            return

        info = (
            self.online_peers.get(self.current_peer_key)
            or self.known_peers.get(self.current_peer_key)
            or {"name": self.current_peer_key}
        )
        connected = self.node.is_chat_connected(self.current_peer_key)
        if connected:
            state_txt = "Đã kết nối TCP"
            self.btn_leave_chat.pack(side=tk.RIGHT)
        elif self.current_peer_key in self.online_peers:
            state_txt = f"{self.online_peers[self.current_peer_key]['status']} · chưa kết nối TCP"
            self.btn_leave_chat.pack_forget()
        else:
            state_txt = "Đang offline"
            self.btn_leave_chat.pack_forget()

        self.partner_name_lbl.configure(text=info["name"])
        self.partner_sub_lbl.configure(text=f"{self.current_peer_key}  ·  {state_txt}")

    def leave_current_chat(self):
        if not self.current_peer_key:
            self.set_notice("Chưa có cuộc trò chuyện nào đang mở.")
            return
        self.node.leave_chat(self.current_peer_key, notify_ui=True)
        self.update_partner_header()

    # ==================== NHẬP LIỆU, GỬI TIN, GỬI TỆP & SLASH COMMANDS ====================
    def on_send_input(self):
        self.close_emoji_picker()
        raw = self.msg_var.get().strip()
        if not raw:
            return
        self.msg_var.set("")

        if raw.startswith("/"):
            self.handle_slash_command(raw)
            return

        if not self.current_peer_key:
            messagebox.showinfo(
                "Chưa chọn người nhận",
                "Hãy chọn một người ở cột bên trái (hoặc gõ /chat <tên>) để bắt đầu nhắn tin.",
            )
            return

        msg_id = self.next_msg_id()
        msg = {
            "type": "TEXT",
            "msg_id": msg_id,
            "sender": self.node.name,
            "is_me": True,
            "content": raw,
            "time": now_str(),
            "status": "sending",
        }
        self.msg_by_id[msg_id] = (self.current_peer_key, msg)
        self.append_message(self.current_peer_key, msg)
        # Xếp thẳng vào hàng đợi FIFO theo peer (không chặn UI, bảo toàn 100% thứ tự bấm Enter)
        self.node.enqueue_message(self.current_peer_key, raw, msg_id)

    def handle_slash_command(self, cmd_line):
        """Hỗ trợ đầy đủ lệnh /list, /chat, /send, /leave, /status, /help, /quit."""
        parts = cmd_line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        def show_sys(text):
            if self.current_peer_key:
                self.append_message(self.current_peer_key, {"type": "SYSTEM", "content": text})
            self.set_notice(text.replace("\n", " · "))

        if cmd == "/help":
            help_text = (
                "Danh sách lệnh:\n"
                "/list — Xem người đang online\n"
                "/chat <tên|số> — Mở chat với 1 người\n"
                "/send <đường_dẫn> — Gửi tệp cho người đang chat\n"
                "/leave — Đóng cuộc chat hiện tại\n"
                "/status <online|away> — Đổi trạng thái\n"
                "/quit — Thoát chương trình"
            )
            if self.current_peer_key:
                self.append_message(self.current_peer_key, {"type": "SYSTEM", "content": help_text})
            else:
                messagebox.showinfo("Hướng dẫn lệnh", help_text)

        elif cmd == "/list":
            peers = self.node.get_online_peers()
            if not peers:
                show_sys("Chưa phát hiện người dùng nào khác trên mạng LAN.")
            else:
                lines = [
                    f"{i}. {p['name']} ({p['ip']}:{p['tcp_port']}) — {p['status']}"
                    for i, p in enumerate(peers, start=1)
                ]
                text = "Người dùng đang online:\n" + "\n".join(lines)
                if self.current_peer_key:
                    self.append_message(self.current_peer_key, {"type": "SYSTEM", "content": text})
                else:
                    messagebox.showinfo("Danh sách online", text)

        elif cmd == "/chat":
            if not arg:
                show_sys("Cú pháp: /chat <tên hoặc số thứ tự>")
                return
            peer = self.node.find_peer(arg)
            if not peer:
                show_sys(f"Không tìm thấy người dùng '{arg}' đang online.")
                return
            self.select_peer(peer["peer_key"])

        elif cmd == "/send":
            if not self.current_peer_key:
                show_sys("Hãy chọn người nhận trước khi gửi tệp.")
                return
            if not arg:
                self.pick_and_send_file()
                return
            path = arg.strip('"').strip("'")
            ext = os.path.splitext(path)[1].lower()
            is_img = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            self.start_file_transfer(path, is_image=is_img)

        elif cmd == "/leave":
            self.leave_current_chat()

        elif cmd == "/status":
            if arg.lower() not in ("online", "away"):
                show_sys("Cú pháp: /status online hoặc /status away")
                return
            self.set_my_status(arg.lower())

        elif cmd == "/quit":
            self.on_close()

        else:
            show_sys(f"Lệnh không hợp lệ: '{cmd}'. Gõ /help để xem danh sách lệnh.")

    def pick_and_send_image(self):
        if not self.current_peer_key:
            messagebox.showinfo("Chưa chọn người nhận", "Hãy chọn một người ở cột bên trái trước.")
            return
        path = filedialog.askopenfilename(
            title="Chọn hình ảnh",
            filetypes=[("Hình ảnh", "*.png *.jpg *.jpeg *.gif *.webp *.bmp")],
        )
        if path:
            self.start_file_transfer(path, is_image=True)

    def pick_and_send_file(self):
        if not self.current_peer_key:
            messagebox.showinfo("Chưa chọn người nhận", "Hãy chọn một người ở cột bên trái trước.")
            return
        path = filedialog.askopenfilename(title="Chọn tệp tin")
        if path:
            self.start_file_transfer(path, is_image=False)

    def start_file_transfer(self, path, is_image=False):
        if not os.path.isfile(path):
            messagebox.showerror("Không tìm thấy tệp", f"Tệp không tồn tại:\n{path}")
            return
        if os.path.getsize(path) > MAX_FILE_SIZE:
            max_mb = MAX_FILE_SIZE // (1024 * 1024)
            messagebox.showwarning("Tệp quá lớn", f"Vui lòng chọn tệp nhỏ hơn {max_mb} MB.")
            return
        if self.current_peer_key not in self.online_peers:
            messagebox.showwarning("Người nhận đang offline", "Không thể gửi tệp cho người đang offline.")
            return

        self.node.send_file(self.current_peer_key, path, is_image=is_image)

    # ==================== VẼ BONG BÓNG CHAT & THANH TIẾN ĐỘ ====================
    def append_message(self, peer_key, msg_dict):
        history = self.histories.setdefault(peer_key, [])
        history.append(msg_dict)
        if peer_key == self.current_peer_key and self.in_main_screen:
            if len(history) == 1:
                self.canvas.delete("empty_state")
            self.render_single_message(len(history) - 1, msg_dict)
            self.canvas.configure(
                scrollregion=(0, 0, self.get_canvas_width(), self.cursor_y + 18)
            )
            self.canvas.yview_moveto(1.0)

    def get_canvas_width(self):
        w = self.canvas.winfo_width()
        return w if w > 100 else 600

    def on_canvas_resize(self, event):
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(80, self.redraw_messages)

    def scroll_under_pointer(self, event, units):
        """Cuộn đúng vùng con trỏ đang trỏ vào: cột danh sách peer hoặc khung chat."""
        if not self.in_main_screen:
            return
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        w_path = str(widget)
        if w_path.startswith(str(self.peer_list_wrap)):
            if self.peer_canvas.yview() != (0.0, 1.0):
                self.peer_canvas.yview_scroll(units, "units")
        elif w_path.startswith(str(self.chat_container)):
            if self.canvas.yview() != (0.0, 1.0):
                self.canvas.yview_scroll(units, "units")

    def on_mousewheel(self, event):
        if event.delta:
            self.scroll_under_pointer(event, int(-1 * (event.delta / 120)))

    def redraw_messages(self):
        if not self.in_main_screen:
            return
        messages = self.histories.get(self.current_peer_key, []) if self.current_peer_key else []
        y_top, y_bottom = self.canvas.yview()
        was_at_bottom = (y_bottom >= 0.98) or (len(messages) <= 1)

        self.canvas.delete("all")
        self.cursor_y = 16

        if not self.current_peer_key or not messages:
            cw = self.get_canvas_width()
            ch = max(self.canvas.winfo_height(), 360)
            if not self.current_peer_key:
                title = "Chọn một người ở danh sách bên trái"
                hint = f"Tự động tìm kiếm qua UDP :{self.node.udp_port}  ·  Gõ /help để xem danh sách lệnh"
            else:
                info = self.known_peers.get(self.current_peer_key, {"name": self.current_peer_key})
                title = f"Bắt đầu trò chuyện với {info['name']}"
                hint = "Tin nhắn và tệp tin được truyền trực tiếp qua TCP"
            self.canvas.create_text(
                cw / 2, ch * 0.44,
                text=title,
                font=("Segoe UI", 11, "bold"),
                fill=self.theme["text"],
                tags=("empty_state",),
            )
            self.canvas.create_text(
                cw / 2, ch * 0.44 + 24,
                text=hint,
                font=("Segoe UI", 9),
                fill=self.theme["text_muted"],
                tags=("empty_state",),
            )
            self.canvas.configure(scrollregion=(0, 0, cw, ch))
            return

        for idx, msg in enumerate(messages):
            self.render_single_message(idx, msg)

        self.canvas.configure(
            scrollregion=(0, 0, self.get_canvas_width(), self.cursor_y + 18)
        )
        if was_at_bottom:
            self.canvas.yview_moveto(1.0)
        else:
            self.canvas.yview_moveto(y_top)

    def show_text_menu(self, event, text):
        self.text_menu.delete(0, tk.END)
        self.text_menu.add_command(
            label="Sao chép", command=lambda: self.copy_text(text)
        )
        self.text_menu.tk_popup(event.x_root, event.y_root)

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def format_meta_text(self, msg):
        time_str = msg.get("time", "")
        if not msg.get("is_me"):
            return time_str
        status_map = {
            "sending": "Đang gửi...",
            "sent": "Đã gửi ✓",
            "failed": "Gửi lỗi",
        }
        label = status_map.get(msg.get("status"), "")
        return f"{time_str}  ·  {label}" if label else time_str

    def get_meta_color(self, msg):
        if msg.get("is_me") and msg.get("status") == "failed":
            return self.theme["danger"]
        return self.theme["text_muted"]

    def update_message_status(self, msg_id, status):
        entry = self.msg_by_id.get(msg_id)
        if not entry:
            return
        peer_key, msg = entry
        msg["status"] = status
        if peer_key == self.current_peer_key and msg.get("meta_canvas_id"):
            try:
                self.canvas.itemconfigure(
                    msg["meta_canvas_id"],
                    text=self.format_meta_text(msg),
                    fill=self.get_meta_color(msg),
                )
            except tk.TclError:
                pass

    def format_file_bubble_text(self, msg):
        fname = msg.get("filename", "tep_dinh_kem")
        fsize = format_size(msg.get("filesize", 0))
        t_status = msg.get("transfer_status", "done")
        pct = msg.get("percent", 100)
        verb = "Đang gửi" if msg.get("is_me") else "Đang nhận"

        if t_status == "running":
            return f"{fname}\n{fsize}  ·  {verb} {pct}%"
        if t_status == "error":
            err = msg.get("error") or "Truyền tệp thất bại"
            return f"{fname}\n{fsize}  ·  {err}"
        if msg.get("is_me"):
            return f"{fname}\n{fsize}  ·  Hoàn tất 100%  ·  Bấm để lưu bản sao"
        return f"{fname}\n{fsize}  ·  Đã lưu vào downloads/  ·  Bấm để lưu bản sao"

    def render_single_message(self, idx, msg):
        t = self.theme
        cw = self.get_canvas_width()
        max_bubble_w = int(cw * 0.66)
        msg_type = msg.get("type")
        is_me = msg.get("is_me", False)
        tag = f"msg_{idx}"

        # 1. Thông báo hệ thống căn giữa
        if msg_type == "SYSTEM":
            txt_id = self.canvas.create_text(
                cw / 2,
                self.cursor_y + 6,
                text=msg["content"],
                font=("Segoe UI", 8),
                fill=t["sys_fg"],
                width=cw - 100,
                justify=tk.CENTER,
            )
            bx1, by1, bx2, by2 = self.canvas.bbox(txt_id)
            pill = create_rounded_rect(
                self.canvas,
                bx1 - 12, by1 - 4, bx2 + 12, by2 + 4,
                radius=10, fill=t["sys_bg"], outline="",
            )
            self.canvas.tag_lower(pill, txt_id)
            self.cursor_y = by2 + 14
            return

        if msg_type not in ("TEXT", "IMAGE", "FILE"):
            return

        margin_x, pad_x, pad_y = 20, 13, 9
        bg_color = t["out_bg"] if is_me else t["in_bg"]
        fg_color = t["out_fg"] if is_me else t["in_fg"]
        outline_color = "" if is_me else t["in_border"]

        if msg.get("transfer_status") == "error":
            bg_color = t["danger_soft"]
            fg_color = t["danger_text"]
            outline_color = t["danger"]

        bottom_y = self.cursor_y

        # 2. Văn bản
        if msg_type == "TEXT":
            x_pos = (cw - margin_x - pad_x) if is_me else (margin_x + pad_x)
            anchor = "ne" if is_me else "nw"
            txt_id = self.canvas.create_text(
                x_pos, self.cursor_y + pad_y,
                text=msg["content"],
                font=("Segoe UI", 10),
                fill=fg_color,
                width=max_bubble_w,
                anchor=anchor,
                tags=(tag,),
            )
            bx1, by1, bx2, by2 = self.canvas.bbox(txt_id)
            bubble = create_rounded_rect(
                self.canvas,
                bx1 - pad_x, by1 - pad_y, bx2 + pad_x, by2 + pad_y,
                radius=14, fill=bg_color, outline=outline_color, tags=(tag,),
            )
            self.canvas.tag_lower(bubble, txt_id)
            self.canvas.tag_bind(
                tag, "<Button-3>", lambda e, txt=msg["content"]: self.show_text_menu(e, txt)
            )
            bottom_y = by2 + pad_y

        # 3. Hình ảnh (thumbnail bo góc khi hoàn tất 100%)
        elif msg_type == "IMAGE":
            photo = None
            if msg.get("transfer_status", "done") == "done":
                if "thumbnail" not in msg:
                    msg["thumbnail"] = make_rounded_thumbnail(msg.get("filepath", ""))
                photo = msg["thumbnail"]

            if photo:
                img_pad = 5
                x_pos = (cw - margin_x - img_pad) if is_me else (margin_x + img_pad)
                anchor = "ne" if is_me else "nw"
                img_id = self.canvas.create_image(
                    x_pos, self.cursor_y + img_pad, image=photo, anchor=anchor, tags=(tag,)
                )
                bx1, by1, bx2, by2 = self.canvas.bbox(img_id)
                bubble = create_rounded_rect(
                    self.canvas,
                    bx1 - img_pad, by1 - img_pad, bx2 + img_pad, by2 + img_pad,
                    radius=14, fill=bg_color, outline=outline_color, tags=(tag,),
                )
                self.canvas.tag_lower(bubble, img_id)
                bottom_y = by2 + img_pad
            else:
                msg_type = "FILE"

        # 4. Tệp đính kèm & thanh tiến độ thực (Canvas rectangle)
        if msg_type == "FILE":
            is_running = (msg.get("transfer_status") == "running")
            display_text = self.format_file_bubble_text(msg)
            x_pos = (cw - margin_x - pad_x) if is_me else (margin_x + pad_x)
            anchor = "ne" if is_me else "nw"
            txt_id = self.canvas.create_text(
                x_pos, self.cursor_y + pad_y,
                text=display_text,
                font=("Segoe UI", 9, "bold"),
                fill=fg_color,
                width=max_bubble_w,
                anchor=anchor,
                tags=(tag,),
            )
            msg["body_canvas_id"] = txt_id
            bx1, by1, bx2, by2 = self.canvas.bbox(txt_id)

            extra_bottom = 0
            if is_running:
                bar_w = max(180, bx2 - bx1)
                bar_h = 5
                bar_y = by2 + 8
                bar_x = (bx2 - bar_w) if is_me else bx1
                bx1 = min(bx1, bar_x)
                bx2 = max(bx2, bar_x + bar_w)

                track_color = t["bar_track"] if is_me else t["in_bar_track"]
                fill_color = t["bar_fill"] if is_me else t["in_bar_fill"]
                self.canvas.create_rectangle(
                    bar_x, bar_y, bar_x + bar_w, bar_y + bar_h,
                    fill=track_color, outline="", tags=(tag,),
                )
                pct = max(0, min(100, int(msg.get("percent", 0))))
                fill_w = max(4, int(bar_w * pct / 100))
                fill_id = self.canvas.create_rectangle(
                    bar_x, bar_y, bar_x + fill_w, bar_y + bar_h,
                    fill=fill_color, outline="", tags=(tag,),
                )
                msg["bar_fill_id"] = fill_id
                msg["bar_box"] = (bar_x, bar_y, bar_w, bar_h)
                extra_bottom = 14

            bubble = create_rounded_rect(
                self.canvas,
                bx1 - pad_x, by1 - pad_y, bx2 + pad_x, by2 + pad_y + extra_bottom,
                radius=14, fill=bg_color, outline=outline_color, tags=(tag,),
            )
            self.canvas.tag_lower(bubble, txt_id)
            bottom_y = by2 + pad_y + extra_bottom

        if msg.get("type") in ("IMAGE", "FILE") and msg.get("transfer_status", "done") == "done":
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, m=msg: self.save_attachment(m))
            self.canvas.tag_bind(tag, "<Enter>", lambda e: self.canvas.configure(cursor="hand2"))
            self.canvas.tag_bind(tag, "<Leave>", lambda e: self.canvas.configure(cursor=""))

        # Thời gian & trạng thái gửi nhỏ bên dưới bong bóng
        meta_x = (cw - margin_x - 4) if is_me else (margin_x + 4)
        meta_anchor = "ne" if is_me else "nw"
        meta_id = self.canvas.create_text(
            meta_x, bottom_y + 4,
            text=self.format_meta_text(msg),
            font=("Segoe UI", 7),
            fill=self.get_meta_color(msg),
            anchor=meta_anchor,
        )
        msg["meta_canvas_id"] = meta_id
        self.cursor_y = bottom_y + 22

    def save_attachment(self, msg):
        src_path = msg.get("filepath")
        if not src_path or not os.path.exists(src_path):
            messagebox.showerror("Không tìm thấy tệp", "Tệp không còn tồn tại trên máy.")
            return
        save_path = filedialog.asksaveasfilename(
            title="Lưu bản sao tệp", initialfile=msg.get("filename", "download.bin")
        )
        if not save_path:
            return
        try:
            if os.path.abspath(save_path) != os.path.abspath(src_path):
                shutil.copyfile(src_path, save_path)
            messagebox.showinfo("Thành công", f"Đã lưu tệp tại:\n{save_path}")
        except OSError as exc:
            messagebox.showerror("Lỗi lưu tệp", str(exc))

    # ==================== THOÁT ỨNG DỤNG ====================
    def on_close(self):
        self._is_closing = True
        self.close_emoji_picker()
        if self.node:
            self.node.stop()
            self.node = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="LAN Chat & File Sharing (P2P TCP + UDP Socket)"
    )
    parser.add_argument("--name", help="Tên hiển thị (bỏ qua để nhập trên giao diện)")
    parser.add_argument("--port", type=int, default=None, help="Cổng TCP lắng nghe (vd. 5001)")
    parser.add_argument(
        "--udp-port", type=int, default=DEFAULT_UDP_PORT, help="Cổng UDP discovery (mặc định 9999)"
    )
    parser.add_argument(
        "--bcast",
        default=DEFAULT_BCAST,
        help="Địa chỉ UDP broadcast (255.255.255.255 hoặc 127.255.255.255 khi offline)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    root = tk.Tk()
    app = LANChatApp(root, cli_args=args)
    root.mainloop()
