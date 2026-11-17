# -*- coding: utf-8 -*-
"""Minimal fastboot client via pyusb (libusb backend) for munch"""
import usb.core, usb.util, sys, time

VID, PID = 0x18D1, 0xD00D

def find_dev():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return None
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    cfg = dev.get_active_configuration()
    intf = usb.util.find_descriptor(cfg, bInterfaceClass=0xFF)
    if intf is None:
        intf = cfg[0]
    # bulk endpoints
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    return dev, ep_out, ep_in

def fb_cmd(ep_out, ep_in, cmd, timeout=8000):
    """send command, read full response (INFO lines folded), return (status, payload)"""
    ep_out.write(cmd.encode() + b"\x00" if False else cmd.encode(), timeout)
    # NOTE: fastboot does NOT want trailing NUL; fix below
    return None

def fb_cmd2(dev, ep_out, ep_in, cmd, timeout=8000):
    data = cmd.encode()
    ep_out.write(data, timeout)
    resp = b""
    payload = b""
    status = None
    while True:
        try:
            chunk = ep_in.read(512, timeout)
        except usb.core.USBError as e:
            if status is None:
                return ("TIMEOUT", str(e))
            break
        if chunk is None or len(chunk) == 0:
            break
        head = bytes(chunk[:4])
        rest = bytes(chunk[4:])
        if head.startswith(b"INFO"):
            print("  [info] " + rest.decode(errors="replace"))
            continue
        elif head.startswith(b"DATA"):
            size = int(rest[:8], 16)
            payload += bytes(ep_in.read(size, timeout))
            status = "DATA"
            break
        elif head.startswith(b"OKAY"):
            status = "OKAY"
            payload += rest
            break
        elif head.startswith(b"FAIL"):
            status = "FAIL"
            payload += rest
            break
        else:
            # raw response (some bootloaders reply plain text)
            payload += bytes(chunk)
            status = "RAW"
            break
    return (status, payload.decode(errors="replace"))

def main():
    r = find_dev()
    if not r:
        print("NO DEVICE (18D1:D00D not found)")
        return 1
    dev, ep_out, ep_in = r
    print(f"Device: {dev.idVendor:04x}:{dev.idProduct:04x} bus={dev.bus} addr={dev.address}")
    vars_to_query = [
        "getvar:unlocked",
        "getvar:product",
        "getvar:current-slot",
        "getvar:slot-count",
        "getvar:max-download-size",
        "getvar:version-bootloader",
        "getvar:anti",
        "getvar:partition-type:boot",
        "getvar:partition-size:boot",
        "getvar:is-userspace",
        "getvar:secure",
        "getvar:off-mode-charge",
    ]
    results = {}
    for v in vars_to_query:
        st, pl = fb_cmd2(dev, ep_out, ep_in, v)
        results[v] = (st, pl)
        print(f"{v:38s} -> [{st}] {pl.strip()}")
    # Xiaomi device-info (read-only)
    for cmd in ["oem device-info"]:
        print(f"--- {cmd} ---")
        st, pl = fb_cmd2(dev, ep_out, ep_in, cmd, timeout=10000)
        print(f"  [{st}] {pl.strip()}")

if __name__ == "__main__":
    sys.exit(main())
