#!/usr/bin/env python3
"""
ace_console.py - Standalone ACE Pro diagnostic tool.

Usage:
    python3 ace_console.py /dev/ttyACM0
    python3 ace_console.py /dev/ttyACM1 --baud 115200 --interval 2.0
"""

import argparse
import json
import serial
import struct
import sys
import time


# ========== Framing ==========

def calc_crc(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        data = byte
        data ^= crc & 0xFF
        data ^= (data & 0x0F) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc


def build_frame(request: dict, request_id: int) -> bytes:
    request["id"] = request_id
    payload = json.dumps(request).encode("utf-8")
    frame = bytearray([0xFF, 0xAA])
    frame += struct.pack("<H", len(payload))
    frame += payload
    frame += struct.pack("<H", calc_crc(payload))
    frame += b"\xFE"
    return bytes(frame)


def read_frame(ser: serial.Serial, buf: bytearray, timeout: float = 5.0) -> tuple[dict | None, bytearray]:
    """Read and parse one complete frame. Returns (parsed_dict, updated_buf)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk

        while True:
            if len(buf) < 7:
                break

            # Sync to header
            if not (buf[0] == 0xFF and buf[1] == 0xAA):
                hdr = buf.find(bytes([0xFF, 0xAA]))
                if hdr == -1:
                    buf = bytearray()
                    break
                buf = buf[hdr:]
                if len(buf) < 7:
                    break

            payload_len = struct.unpack("<H", buf[2:4])[0]
            frame_len = 2 + 2 + payload_len + 2 + 1

            if len(buf) < frame_len:
                break

            terminator_idx = 4 + payload_len + 2
            if buf[terminator_idx] != 0xFE:
                next_hdr = buf.find(bytes([0xFF, 0xAA]), 1)
                buf = buf[next_hdr:] if next_hdr != -1 else bytearray()
                continue

            frame = bytes(buf[:frame_len])
            buf = bytearray(buf[frame_len:])

            payload = frame[4:4 + payload_len]
            crc_rx = frame[4 + payload_len:4 + payload_len + 2]
            crc_calc = struct.pack("<H", calc_crc(payload))

            if crc_rx != crc_calc:
                print(f"  [!] CRC mismatch - dropping frame", file=sys.stderr)
                continue

            try:
                return json.loads(payload.decode("utf-8")), buf
            except Exception as e:
                print(f"  [!] JSON decode error: {e}", file=sys.stderr)

        time.sleep(0.02)

    return None, buf


def send_and_receive(ser: serial.Serial, buf: bytearray, request: dict, request_id: int, timeout: float = 5.0) -> tuple[dict | None, bytearray]:
    frame = build_frame(request, request_id)
    ser.write(frame)
    return read_frame(ser, buf, timeout=timeout)


# ========== Main ==========

def main():
    parser = argparse.ArgumentParser(description="ACE Pro diagnostic console")
    parser.add_argument("port", help="Serial port, e.g. /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--interval", type=float, default=1.0, help="get_status poll interval in seconds (default: 1.0)")
    parser.add_argument("--count", type=int, default=0, help="Number of status polls before exit (0 = infinite)")
    parser.add_argument("--full", action="store_true", help="Dump full JSON on EVERY poll (default: only on first poll + on field-set changes)")
    parser.add_argument("--feed-test", type=int, default=-1, help="Run feed_assist on this slot index in parallel (provokes tangle response when filament blocked)")
    parser.add_argument("--push-slot", type=int, default=-1, help="Issue feed_filament on this slot — ACE actively pumps regardless of buffer state, exposing tangle response")
    parser.add_argument("--push-length", type=int, default=500, help="Length in mm for --push-slot (default 500)")
    parser.add_argument("--push-speed", type=int, default=15, help="Speed in mm/s for --push-slot (default 15)")
    args = parser.parse_args()

    print(f"Connecting to {args.port} @ {args.baud} baud...")
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            timeout=0,
            write_timeout=1.0
        )
    except Exception as e:
        print(f"Failed to open port: {e}", file=sys.stderr)
        sys.exit(1)

    # Flush stale data
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    print(f"Port open. Sending get_info...\n")

    buf = bytearray()
    req_id = 0

    # --- get_info ---
    response, buf = send_and_receive(ser, buf, {"method": "get_info"}, req_id, timeout=5.0)
    req_id += 1
    if response:
        print(f"[get_info] {json.dumps(response, indent=2)}\n")
    else:
        print("[get_info] No response (timeout)\n", file=sys.stderr)

    # --- optionally start feed_assist on a slot ---
    if args.feed_test >= 0:
        print(f"\n*** Enabling feed_assist on slot {args.feed_test} ***")
        feed_req = {"method": "start_feed_assist", "params": {"index": args.feed_test}}
        response, buf = send_and_receive(ser, buf, feed_req, req_id, timeout=5.0)
        req_id += 1
        print(f"[start_feed_assist] {json.dumps(response, indent=2) if response else 'TIMEOUT'}\n")
        print("Provoke the tangle now (block the spool / clamp the bowden).")
        print("Watch for slot.status changes or new fields below.\n")

    # --- optionally issue an active feed_filament push on a slot ---
    # This is the tangle-probe path: ACE actively tries to push X mm of
    # filament regardless of any buffer state, so if the spool is blocked
    # or the gear can't bite, the firmware MUST report something.
    if args.push_slot >= 0:
        print(f"\n*** Issuing feed_filament: slot={args.push_slot} length={args.push_length}mm speed={args.push_speed}mm/s ***")
        push_req = {
            "method": "feed_filament",
            "params": {
                "index": args.push_slot,
                "length": args.push_length,
                "speed": args.push_speed,
            },
        }
        response, buf = send_and_receive(ser, buf, push_req, req_id, timeout=5.0)
        req_id += 1
        print(f"[feed_filament] {json.dumps(response, indent=2) if response else 'TIMEOUT'}\n")
        print("Block the filament path NOW — clamp the bowden or hold the spool.")
        print("ACE should attempt to push and (hopefully) report tangle/stuck status.\n")

    # --- cyclic get_status ---
    print(f"Polling get_status every {args.interval}s  (Ctrl+C to stop)\n")
    poll = 0
    last_keys_signature = None  # tracks what fields are present
    last_values = {}  # for change detection per field
    try:
        while args.count == 0 or poll < args.count:
            t0 = time.time()
            response, buf = send_and_receive(ser, buf, {"method": "get_status"}, req_id, timeout=5.0)
            req_id += 1
            elapsed = time.time() - t0
            poll += 1

            ts = time.strftime("%H:%M:%S")
            if response:
                result = response.get("result", {})
                status = result.get("status", "?")
                action = result.get("action", "?")
                temp = result.get("temp", "?")
                code = response.get("code", "?")
                msg = response.get("msg", "?")
                slots = result.get("slots", [])

                slot_summary = "  ".join(
                    f"S{s.get('index','?')}:{s.get('status','?')}"
                    for s in slots
                )
                print(f"[{ts}] #{poll:4d}  code={code} msg={msg} status={status}  action={action}  temp={temp}  |  {slot_summary}  ({elapsed*1000:.0f}ms)")

                # Compute a key-set signature so we detect NEW or REMOVED fields anywhere in result
                def collect_keys(obj, prefix=""):
                    keys = set()
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            full_key = f"{prefix}.{k}" if prefix else k
                            keys.add(full_key)
                            keys.update(collect_keys(v, full_key))
                    elif isinstance(obj, list):
                        for i, item in enumerate(obj):
                            keys.update(collect_keys(item, f"{prefix}[{i}]"))
                    return keys

                key_signature = frozenset(collect_keys(response))

                # First poll OR keys changed OR --full → dump everything
                if args.full or poll == 1 or key_signature != last_keys_signature:
                    if poll != 1 and last_keys_signature is not None:
                        added = key_signature - last_keys_signature
                        removed = last_keys_signature - key_signature
                        if added:
                            print(f"  *** NEW FIELDS: {sorted(added)} ***")
                        if removed:
                            print(f"  *** REMOVED FIELDS: {sorted(removed)} ***")
                    print(f"\n  Full response:\n{json.dumps(response, indent=4)}\n")
                    last_keys_signature = key_signature
                else:
                    # Still flag value changes on known interesting fields
                    interesting = {"code": code, "msg": msg, "result.status": status, "result.action": action}
                    for slot in slots:
                        idx = slot.get("index", "?")
                        interesting[f"slot[{idx}].status"] = slot.get("status")
                        interesting[f"slot[{idx}].rfid"] = slot.get("rfid")
                    changes = []
                    for k, v in interesting.items():
                        if k in last_values and last_values[k] != v:
                            changes.append(f"{k}: {last_values[k]!r} → {v!r}")
                        last_values[k] = v
                    if changes:
                        print(f"  *** VALUE CHANGES: {changes} ***")
            else:
                print(f"[{ts}] #{poll:4d}  TIMEOUT ({elapsed*1000:.0f}ms)", file=sys.stderr)

            # Sleep remaining interval
            spent = time.time() - t0
            remaining = args.interval - spent
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        print(f"\nStopped after {poll} polls.")
    finally:
        # Best-effort: stop feed_assist if we started one
        if args.feed_test >= 0:
            try:
                print(f"\nStopping feed_assist on slot {args.feed_test}...")
                stop_req = {"method": "stop_feed_assist", "params": {"index": args.feed_test}}
                response, buf = send_and_receive(ser, buf, stop_req, req_id, timeout=2.0)
                print(f"[stop_feed_assist] {json.dumps(response, indent=2) if response else 'TIMEOUT'}")
            except Exception:
                pass
        # Best-effort: stop active push if we issued one
        if args.push_slot >= 0:
            try:
                print(f"\nStopping feed_filament on slot {args.push_slot}...")
                stop_req = {"method": "stop_feed_filament", "params": {"index": args.push_slot}}
                response, buf = send_and_receive(ser, buf, stop_req, req_id, timeout=2.0)
                print(f"[stop_feed_filament] {json.dumps(response, indent=2) if response else 'TIMEOUT'}")
            except Exception:
                pass
        ser.close()


if __name__ == "__main__":
    main()
