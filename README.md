# LAN Chat & File Sharing (P2P — TCP & UDP Sockets)

Ứng dụng nhắn tin và chia sẻ tệp ngang hàng (**Peer-to-Peer**) trên mạng LAN, kết hợp:
- **UDP Broadcast (port 9999)**: Tự động phát hiện người dùng trên mạng LAN mỗi 3 giây, tự động đánh dấu offline sau 10 giây timeout, cập nhật trạng thái `online` / `away`.
- **TCP Socket (port tùy chọn, mặc định 5000+)**: Nhắn tin 1–1 trực tiếp với nhiều peer đồng thời (hỗ trợ đếm tin nhắn chưa đọc, xác nhận `ACK`), truyền tệp/hình ảnh song song trên kết nối TCP riêng có tiến độ `%`, tự lưu vào thư mục `downloads/` và tự xóa tệp tải dở nếu kết nối gián đoạn.

---

## 1. Cấu trúc dự án

```text
LANChatP2P/
├── protocol.py   # Giao thức UDP HELLO và TCP framing ([4B len][JSON])
├── network.py    # Lõi mạng P2P 6 luồng (UDP Discovery, TCP Chat, TCP File Streaming)
├── lanchat.py    # Chương trình chính + Giao diện Tkinter (hỗ trợ cả lệnh /...)
├── downloads/    # Thư mục tự tạo lưu tệp tin đã nhận
└── README.md     # Tài liệu hướng dẫn cài đặt, chạy và kiểm thử
```

---

## 2. Yêu cầu cài đặt

- **Python 3.8+** (dùng thư viện chuẩn: `socket`, `threading`, `queue`, `struct`, `json`, `tkinter`).
- **Tùy chọn (khuyên cài để xem trước ảnh bo góc)**:
  ```bash
  pip install pillow
  ```

---

## 3. Cách chạy

### A. Chạy nhiều cửa sổ trên CÙNG 1 máy tính (Demo / Test nhanh)

Mở 2–3 cửa sổ Terminal tại thư mục `LANChatP2P`:

```bash
# Cửa sổ 1 (Quan)
python lanchat.py --name Quan --port 5001

# Cửa sổ 2 (Minh)
python lanchat.py --name Minh --port 5002

# Cửa sổ 3 (Huy)
python lanchat.py --name Huy --port 5003
```

> **Lưu ý nếu máy tính đang tắt Wi-Fi / không kết nối mạng LAN (offline)**: thêm cờ `--bcast 127.255.255.255`:
> ```bash
> python lanchat.py --name Quan --port 5001 --bcast 127.255.255.255
> python lanchat.py --name Minh --port 5002 --bcast 127.255.255.255
> ```

### B. Chạy trên 2+ máy tính trong cùng mạng LAN / Wi-Fi

Trên mỗi máy chỉ cần chạy:
```bash
python lanchat.py
```
(Hoặc `python lanchat.py --name <TenCuaBan>` — ứng dụng tự dùng cổng TCP `5000`, UDP `9999`, broadcast `255.255.255.255`.)

> **Lưu ý Windows Firewall**: Khi Windows hiện hộp thoại *Windows Defender Firewall*, bấm **Allow access** cho Python (chọn cả Private/Public network nếu dùng Wi-Fi trường) để các gói UDP broadcast và TCP đi qua được.

---

## 4. Các lệnh hỗ trợ ngay trong ô chat

Ngoài thao tác trực tiếp bằng chuột (chọn người ở cột trái, bật/tắt trạng thái `Online`/`Away`, chuyển chế độ Sáng/Tối, nút `Ảnh`, `Tệp`, `Icon`), ứng dụng hỗ trợ đầy đủ bộ lệnh:

| Lệnh | Chức năng |
|---|---|
| `/list` | Hiển thị danh sách người dùng đang online trên mạng LAN |
| `/chat <tên\|số>` | Mở cuộc trò chuyện TCP với người dùng tương ứng |
| `/send <đường_dẫn>` | Gửi tệp tin cho người đang trò chuyện |
| `/leave` | Đóng cuộc trò chuyện TCP hiện tại (vẫn online UDP) |
| `/status <online\|away>` | Đổi trạng thái và phát UDP broadcast ngay lập tức |
| `/help` | Xem danh sách lệnh |
| `/quit` | Đóng các kết nối và thoát chương trình |

---

## 5. Hướng dẫn chụp ảnh Wireshark cho báo cáo (Deliverable #3)

1. Mở **Wireshark**, chọn card mạng đang dùng (Wi-Fi nếu test 2 máy, hoặc **Adapter for loopback traffic capture** nếu test trên 1 máy với `127.255.255.255`).
2. **Chụp gói UDP Broadcast**:
   - Nhập bộ lọc: `udp.port == 9999`
   - Quan sát các gói đến `255.255.255.255` (hoặc `127.255.255.255`) mỗi 3 giây.
   - Bấm vào 1 gói → mở mục **Data** để thấy nội dung rõ ràng gồm 5 trường: `HELLO|Quan|5001|online|a1b2c3` (trong đó 6 ký tự cuối là `instance_id` phân biệt từng tiến trình).
3. **Chụp bắt tay 3 bước TCP (Three-way Handshake)**:
   - Nhập bộ lọc: `tcp.port == 5002` (cổng TCP của máy nhận).
   - Bấm chọn **Minh** ở cửa sổ của **Quan** để mở kết nối TCP.
   - Chụp lại 3 gói đầu tiên: `[SYN]` → `[SYN, ACK]` → `[ACK]`, kèm gói dữ liệu `HELLO_TCP` ngay phía sau.
