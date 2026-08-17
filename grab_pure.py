import os
import socket
import struct
import sys
from datetime import datetime, timedelta

SCRIPT_ID = "BUILD-2026-08-17-v4.5.0-timeframe-batch100"

# Parse Hours Argument (Default to 24 hours if not provided)
HOURS_BACK = 24
if len(sys.argv) > 1:
    try:
        HOURS_BACK = float(sys.argv[1])
    except ValueError:
        print(f"[WARN] Invalid hours argument '{sys.argv[1]}'. Defaulting to 24 hours.")

CUTOFF_TIME = datetime.now() - timedelta(hours=HOURS_BACK)

print("==========================================")
print(" Executing: grab_pure.py (Timeframe + Batching)")
print(f" Build ID   : {SCRIPT_ID}")
print(f" Timeframe  : Past {HOURS_BACK} hour(s) (Since {CUTOFF_TIME.strftime('%Y-%m-%d %H:%M:%S')})")
print("==========================================\n")

HOST = "192.168.1.1"
PORT = 15740


def send_cmd(s, trans_id, opcode, params=(), data_phase=1):
    length = 18 + 4 * len(params)
    pkt = struct.pack("<IIIHI", length, 6, data_phase, opcode, trans_id)
    for p in params:
        pkt += struct.pack("<I", p)
    s.sendall(pkt)


def recv_packet(s, timeout=15.0):
    s.settimeout(timeout)
    try:
        header = b""
        while len(header) < 8:
            chunk = s.recv(8 - len(header))
            if not chunk:
                return None, b""
            header += chunk
        length, pkt_type = struct.unpack("<II", header)
        payload = b""
        remaining = length - 8
        while len(payload) < remaining:
            chunk = s.recv(remaining - len(payload))
            if not chunk:
                break
            payload += chunk
        return pkt_type, payload
    except socket.timeout:
        return -1, b""


def recv_ptp_data(s, timeout=30.0):
    data = b""
    res_code = 0
    while True:
        ptype, payload = recv_packet(s, timeout=timeout)
        if ptype == -1:
            break
        elif ptype == 9:
            data += payload[12:]
        elif ptype in (10, 12):
            data += payload[4:]
        elif ptype == 7:
            if len(payload) >= 2:
                res_code = struct.unpack("<H", payload[:2])[0]
            break
    return res_code, data


def parse_ptp_string(data, offset):
    if offset >= len(data):
        return "", offset
    num_chars = data[offset]
    if num_chars == 0:
        return "", offset + 1
    str_bytes_len = num_chars * 2
    end_offset = offset + 1 + str_bytes_len
    if end_offset > len(data):
        return "", offset + 1
    raw_str = data[offset + 1 : end_offset]
    val = raw_str.decode("utf-16le", errors="ignore").rstrip("\x00")
    return val, end_offset


def parse_object_info(data):
    if len(data) < 53:
        return None
    fmt = struct.unpack("<H", data[4:6])[0]
    size = struct.unpack("<I", data[8:12])[0]

    offset = 52
    filename, offset = parse_ptp_string(data, offset)
    capture_date_str, offset = parse_ptp_string(data, offset)

    dt = None
    if capture_date_str and len(capture_date_str) >= 15:
        try:
            # PTP date format: YYYYMMDDTHHMMSS
            clean_str = capture_date_str.split(".")[0]
            dt = datetime.strptime(clean_str, "%Y%m%dT%H%M%S")
        except ValueError:
            pass

    return {"format": fmt, "size": size, "filename": filename, "datetime": dt}


# 1. Local Deduplication Set
dcim_base = "/sdcard/DCIM" if os.path.exists("/sdcard") else "."
existing_files = set()

print("[1/4] Indexing previously downloaded files locally...")
for root, _, files in os.walk(dcim_base):
    for file in files:
        if file.upper().endswith(".JPG"):
            existing_files.add(file.upper())

print(f"      Found {len(existing_files)} existing JPEGs locally.")

# 2. Connect & Open PTP Session
print("\n[2/4] Connecting to camera (192.168.1.1)...")
s_cmd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s_cmd.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
s_cmd.settimeout(10.0)

try:
    s_cmd.connect((HOST, PORT))
except socket.timeout:
    print("[ERROR] Could not reach camera Wi-Fi.")
    sys.exit(1)

guid = b"\x00" * 16
dev_name = "Termux".encode("utf-16le") + b"\x00\x00"
length = 8 + 16 + len(dev_name) + 4
s_cmd.sendall(
    struct.pack("<II", length, 1) + guid + dev_name + struct.pack("<I", 0x00010000)
)

_, payload = recv_packet(s_cmd)
conn_num = struct.unpack("<I", payload[:4])[0]

s_evt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s_evt.settimeout(10.0)
s_evt.connect((HOST, PORT))
s_evt.sendall(struct.pack("<III", 12, 3, conn_num))
recv_packet(s_evt)

trans_id = 1
send_cmd(s_cmd, trans_id, opcode=0x1002, params=(1,), data_phase=1)
trans_id += 1
recv_packet(s_cmd)

# 3. Query Camera Handles & Filter JPEGs by Cutoff Time
print(f"\n[3/4] Scanning SD Card Slot 1 for JPEGs taken after {CUTOFF_TIME.strftime('%H:%M:%S')}...")
send_cmd(
    s_cmd,
    trans_id,
    opcode=0x1007,
    params=(0x00010001, 0x00000000, 0x00000000),
    data_phase=1,
)
trans_id += 1
res_code, handles_data = recv_ptp_data(s_cmd, timeout=10.0)

pending_downloads = []
if res_code == 0x2001 and len(handles_data) >= 4:
    count = struct.unpack("<I", handles_data[:4])[0]
    handles = struct.unpack(f"<{count}I", handles_data[4 : 4 + (count * 4)])

    # Scan in REVERSE (newest photos first) for fast early exit
    for h in reversed(handles):
        send_cmd(s_cmd, trans_id, opcode=0x1008, params=(h,), data_phase=1)
        trans_id += 1
        rc, info_bytes = recv_ptp_data(s_cmd, timeout=10.0)

        if rc == 0x2001 and len(info_bytes) > 0:
            info = parse_object_info(info_bytes)
            if not info:
                continue

            dt = info["datetime"]
            fname = info["filename"]

            # If photo date is older than CUTOFF_TIME, stop scanning older handles
            if dt and dt < CUTOFF_TIME:
                print(f"      Reached photo from {dt.strftime('%Y-%m-%d %H:%M:%S')} (Older than {HOURS_BACK}h cutoff). Stopping scan.")
                break

            if fname.upper().endswith(".JPG"):
                if fname.upper() in existing_files:
                    continue  # Already downloaded locally
                pending_downloads.append((h, fname, info["size"], dt))

# Restore chronological download order (oldest to newest among pending)
pending_downloads.reverse()

if not pending_downloads:
    print(f"\n[INFO] No new JPEGs found from the past {HOURS_BACK} hours.")
    s_cmd.close()
    s_evt.close()
    sys.exit(0)

print(f"      Found {len(pending_downloads)} new JPEGs to download.")

# 4. Create Session Directory & Download in Batches of 100
session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
session_dir = os.path.join(dcim_base, f"Session_{session_timestamp}")
os.makedirs(session_dir, exist_ok=True)

print(f"\n[4/4] Starting transfer session to: {session_dir}")

for idx, (h, fname, size, dt) in enumerate(pending_downloads):
    batch_num = (idx // 100) + 1
    batch_dir = os.path.join(session_dir, f"batch_{batch_num:03d}")
    os.makedirs(batch_dir, exist_ok=True)

    out_path = os.path.join(batch_dir, fname)
    time_str = dt.strftime("%H:%M:%S") if dt else "Unknown Time"
    print(
        f"  -> [{idx + 1}/{len(pending_downloads)}] Batch {batch_num:03d}: "
        f"Downloading {fname} ({size / 1024 / 1024:.2f} MB) [{time_str}]..."
    )

    send_cmd(s_cmd, trans_id, opcode=0x1009, params=(h,), data_phase=1)
    trans_id += 1
    rc, data = recv_ptp_data(s_cmd, timeout=30.0)

    if rc == 0x2001 and len(data) > 0:
        with open(out_path, "wb") as f:
            f.write(data)
        existing_files.add(fname.upper())
    else:
        print(f"     [ERROR] Failed to download {fname}")

s_cmd.close()
s_evt.close()

print(f"\n[SUCCESS] Saved {len(pending_downloads)} files across batch directories.")

