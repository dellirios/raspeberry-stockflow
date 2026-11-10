"""Read-only Zebra USB status probe; printing remains in CUPS."""
import ctypes as C
import json
import os
from pathlib import Path
import re
import sys
import time

SERIAL = os.environ.get("ZEBRA_USB_SERIAL", "").strip()
if not SERIAL:
    raise RuntimeError("Configure ZEBRA_USB_SERIAL antes de consultar a impressora")
matches = []
for entry in Path("/sys/bus/usb/devices").iterdir():
    try:
        if (entry.joinpath("idVendor").read_text().strip() == "0a5f"
                and entry.joinpath("idProduct").read_text().strip() == "0164"
                and entry.joinpath("serial").read_text().strip() == SERIAL):
            matches.append((int(entry.joinpath("busnum").read_text()), int(entry.joinpath("devnum").read_text())))
    except (FileNotFoundError, OSError):
        continue
if len(matches) != 1:
    raise RuntimeError("Zebra configurada desconectada ou identificacao ambigua")

u = C.CDLL("libusb-1.0.so.0")
signatures = {
    "libusb_init": ([C.POINTER(C.c_void_p)], C.c_int),
    "libusb_get_device_list": ([C.c_void_p, C.POINTER(C.POINTER(C.c_void_p))], C.c_ssize_t),
    "libusb_free_device_list": ([C.POINTER(C.c_void_p), C.c_int], None),
    "libusb_get_bus_number": ([C.c_void_p], C.c_uint8),
    "libusb_get_device_address": ([C.c_void_p], C.c_uint8),
    "libusb_open": ([C.c_void_p, C.POINTER(C.c_void_p)], C.c_int),
    "libusb_set_auto_detach_kernel_driver": ([C.c_void_p, C.c_int], C.c_int),
    "libusb_claim_interface": ([C.c_void_p, C.c_int], C.c_int),
    "libusb_release_interface": ([C.c_void_p, C.c_int], C.c_int),
    "libusb_close": ([C.c_void_p], None),
    "libusb_exit": ([C.c_void_p], None),
    "libusb_bulk_transfer": ([C.c_void_p, C.c_ubyte, C.c_void_p, C.c_int, C.POINTER(C.c_int), C.c_uint], C.c_int),
}
for name, (args, result) in signatures.items():
    getattr(u, name).argtypes = args
    getattr(u, name).restype = result

context = C.c_void_p()
devices = C.POINTER(C.c_void_p)()
handle = C.c_void_p()
claimed = False
if u.libusb_init(C.byref(context)) != 0:
    raise RuntimeError("USB init failed")
try:
    count = u.libusb_get_device_list(context, C.byref(devices))
    if count < 0:
        raise RuntimeError("USB enumeration failed")
    selected = [devices[i] for i in range(count)
                if (u.libusb_get_bus_number(devices[i]), u.libusb_get_device_address(devices[i])) == matches[0]]
    if len(selected) != 1 or u.libusb_open(selected[0], C.byref(handle)) != 0:
        raise RuntimeError("USB open failed")
    u.libusb_set_auto_detach_kernel_driver(handle, 1)
    result = u.libusb_claim_interface(handle, 0)
    if result == -6:
        sys.exit(75)  # CUPS currently owns USB; wait, never steal the interface.
    if result != 0:
        raise RuntimeError("USB claim failed: " + str(result))
    claimed = True
    replies = {"serial": SERIAL}
    for key, command in [
        ("hostStatus", b"~HS\r\n"),
        ("counter", b'! U1 getvar "odometer.total_label_count"\r\n'),
    ]:
        transferred = C.c_int()
        buffer = C.create_string_buffer(command)
        result = u.libusb_bulk_transfer(handle, 1, buffer, len(command), C.byref(transferred), 1000)
        if result or transferred.value != len(command):
            raise RuntimeError("USB status query failed")
        response = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            buffer = C.create_string_buffer(4096)
            result = u.libusb_bulk_transfer(handle, 0x81, buffer, 4096, C.byref(transferred), 200)
            response += buffer.raw[:transferred.value]
            if len(response) > 16384:
                raise RuntimeError("Oversized USB response")
            if key == "hostStatus" and response.count(b"\x03") >= 3:
                break
            if key == "counter" and re.fullmatch(rb'"[0-9]+"\s*', response):
                break
            if result not in (0, -7):
                raise RuntimeError("USB response failed: " + str(result))
        if not response:
            raise RuntimeError("Printer status response timeout")
        replies[key] = response.decode("ascii", errors="strict")
    print(json.dumps(replies), flush=True)
finally:
    if claimed:
        u.libusb_release_interface(handle, 0)
    if handle:
        u.libusb_close(handle)
    if devices:
        u.libusb_free_device_list(devices, 1)
    u.libusb_exit(context)
