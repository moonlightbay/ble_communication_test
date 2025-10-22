from __future__ import annotations
from blecore import (
    get_local_info, connect, is_connected, disconnect, 
    get_characteristics, read_characteristic, write_characteristic, 
    start_notify, stop_notify
)
from time import sleep
from typing import Iterable
from decode import decode_stream, decode_frame

HEADER = 0x55
FOOTER = 0xAA
COMMAND_OFF = 0x00
COMMAND_ON = 0x01

def _checksum(command: int, payload: Iterable[int]) -> int:
    total = command + sum(payload)
    return total & 0xFF

def build_packet(command: int, intensity: int, duration_steps: int) -> bytes:
    """根据协议字段生成完整报文。"""

    if command not in (COMMAND_OFF, COMMAND_ON):
        raise ValueError(f"非法指令值: {command}")
    if not 0 <= intensity <= 100:
        raise ValueError("震动强度需位于 0~100")
    if not 0 <= duration_steps <= 255:
        raise ValueError("持续时间步进需位于 0~255")
    data = bytes([intensity, duration_steps])
    checksum = _checksum(command, data)
    return bytes([HEADER, command, *data, checksum, FOOTER])


device_address = "E8:13:73:A4:20:FB" 
service_uuid = "8653000a-43e6-47b7-9cb0-5fc21d4ae340"
characteristics = {
    "write_characteristic": "8653000c-43e6-47b7-9cb0-5fc21d4ae340",
    "notify_characteristic": "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
}

def main():
    connect(device_address, 3, 20)
    print("Connected to device.")
    packet = build_packet(COMMAND_ON, intensity=80, duration_steps=100)
    write_characteristic(device_address, characteristics["write_characteristic"], packet)
    print("Data written to characteristic.")
    # sleep(5)
    disconnect(device_address)
    print("Disconnected from device.")


if __name__ == "__main__":
    main()