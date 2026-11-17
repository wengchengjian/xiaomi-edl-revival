# -*- coding: utf-8 -*-
"""单会话: 读 LUN4 GPT → 备份 → 把 boot_a slot 属性改为 active(priority=15,tries=7)
→ 重算 entries CRC + header CRC → 写回 sector1-5 → 回读校验 → reset
最小侵入: 只改 boot_a entry 的 attributes 8 字节与两个 CRC32。"""
import sys, os, time, zlib, struct, threading, logging
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
BAK = r"F:/Temp/munch_backup"
ROM = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images"
LOADER = os.environ.get("EDL_LOADER", f"{ROM}/prog_ufs_firehose_sm8250_ddr_5.elf")
TMP = f"{FIX}/v_gpt.bin"
GPT_BAK = f"{BAK}/gpt_lun4_before.bin"
PATCHED = f"{FIX}/gpt_lun4_patched.bin"

ATTR_ACTIVE = (0xF << 48) | (0x7 << 52)   # priority=15, tries=7, successful=0
P = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def pipeline():
    sys.argv = ["edl.py", f"--loader={LOADER}", "--memory=UFS",
                "rs", "0", "1", "dummy.bin", "--sectorsize=4096"]
    import edl
    from edlclient.Library.firehose_client import firehose_client
    from edlclient.Library.Connection.usblib import usb_class
    from edlclient.Library.sahara import sahara
    from edlclient.Config.usb_ids import default_ids

    captured = []
    def cap_print(s):
        try: print(s, flush=True)
        except Exception: pass
        captured.append(str(s))
    class _CapHandler(logging.Handler):
        def emit(self, record):
            try: captured.append(record.getMessage())
            except Exception: pass
    _h = _CapHandler(); _h.setLevel(logging.INFO)
    _root = logging.getLogger(); _root.setLevel(logging.INFO); _root.addHandler(_h)

    P("等待 9008（15 分钟）... 请长按电源键12秒断电，黑屏后按住工程线按钮插线，叮咚松开")
    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    _t0 = time.time()
    while time.time() - _t0 < 900:
        if _lb and usb.core.find(idVendor=0x05C6, idProduct=0x9008, backend=_b):
            break
        time.sleep(0.5)
    else:
        P("!!! 15 分钟未见 9008, 退出"); os._exit(2)
    time.sleep(1.0)
    cdc = usb_class(portconfig=default_ids, loglevel=logging.WARNING, serial_number=None)
    for _i in range(5):
        if cdc.connect(): break
        time.sleep(1.0)
    else:
        P("!!! USB 打不开"); os._exit(2)

    mode = None; sah_obj = None
    probe = cdc.read(0xC * 4, timeout=1)
    if probe and len(probe) > 1 and probe[0] == 0x01:
        P("sahara → 上传 loader")
        sah_obj = sahara(cdc, loglevel=logging.WARNING)
        sah_obj.programmer = LOADER
        v = 2
        try: v = sah_obj.ch.pkt_hello_req(probe).version
        except Exception: pass
        if sah_obj.cmd_info(version=v) is None: os._exit(2)
        r = sah_obj.connect()
        if r.get("mode") != "sahara": os._exit(2)
        mode = sah_obj.upload_loader(version=v)
    else:
        cdc.write(b'<?xml version="1.0" ?><data><nop /></data>')
        res = cdc.read(2048, timeout=1)
        mode = "firehose" if b"<?xml" in res else None
        if not mode:
            P("!!! 会话死, 断电重来"); os._exit(2)
    class SahStub:
        programmer = LOADER; serial = None; bit64 = True
    _sah = sah_obj if sah_obj else SahStub
    _cr = {}
    def _conn():
        try:
            _f = firehose_client(edl.args, cdc, _sah, logging.INFO, cap_print)
            _cr["fhc"] = _f; _cr["ok"] = _f.connect(_sah)
        except Exception as e:
            _cr["err"] = f"{type(e).__name__}: {str(e)[:150]}"
    _t = threading.Thread(target=_conn, daemon=True); _t.start(); _t.join(90)
    if _t.is_alive(): P("!!! UFS 楔死, 断电10秒重来"); os._exit(2)
    if "err" in _cr: P(f"!!! {_cr['err']}"); os._exit(2)
    time.sleep(1.0)
    if not any(k in c for c in captured for k in ("SKhynix","Supported Functions","Authenticated")):
        P("!!! loader 不健康"); os._exit(2)
    fh = _cr["fhc"].firehose
    P("=== 会话就绪 ✓ ===")

    def sc(desc, fn):
        try:
            r = fn(); P(f"{desc} ✓"); return r
        except Exception as e:
            P(f"{desc} 失败: {type(e).__name__}: {str(e)[:120]}"); return None

    # 1) 读 LUN4 GPT: sector 1-5 (header + entries), sector0 MBR 也读留档
    P("读取 LUN4 GPT (sector 0-5)...")
    if os.path.exists(TMP): os.remove(TMP)
    if sc("读 GPT", lambda: fh.cmd_read(4, 0, 6, TMP, True)) is None: os._exit(2)
    if os.path.getsize(TMP) < 6 * 4096:
        P("!!! 读回不完整"); os._exit(2)
    data = open(TMP, "rb").read()
    open(GPT_BAK, "wb").write(data)
    P(f"原始 GPT 已备份 → {GPT_BAK}")
    if data[4096:4104] != b"EFI PART":
        P("!!! LBA1 不是 GPT header, 异常退出"); os._exit(2)

    # 2) 解析 entries (sector 2-5, 32 entries/sector, 128B each)
    entries = []
    for i in range(128):
        off = 8192 + i * 128
        e = data[off:off + 128]
        if e[:16] == b"\x00" * 16: continue
        name = e[0x38:0x80].decode("utf-16-le").rstrip("\x00")
        attrs = struct.unpack("<Q", e[0x30:0x38])[0]
        entries.append((i, off, name, attrs))
    for i, off, name, attrs in entries:
        if any(k in name for k in ("boot", "dtbo", "vbmeta")):
            pri = (attrs >> 48) & 0xF
            tri = (attrs >> 52) & 0x7
            suc = (attrs >> 54) & 1
            P(f"  {name:16s} entry#{i:3d} attrs=0x{attrs:016x} pri={pri} tries={tri} success={suc}")

    boot_a = next((e for e in entries if e[2] == "boot_a"), None)
    if boot_a is None:
        P("!!! 找不到 boot_a, 退出"); os._exit(2)
    i, off, name, attrs = boot_a
    old = attrs
    new = ATTR_ACTIVE
    P(f"boot_a: 0x{old:016x} → 0x{new:016x}")
    data = bytearray(data)
    data[off + 0x30:off + 0x38] = struct.pack("<Q", new)

    # 3) 重算 entries CRC32 → header
    num_entries = struct.unpack("<I", data[4096 + 0x50:4096 + 0x54])[0]
    entry_size = struct.unpack("<I", data[4096 + 0x54:4096 + 0x58])[0]
    arr = bytes(data[8192:8192 + num_entries * entry_size])
    arr_crc = zlib.crc32(arr) & 0xFFFFFFFF
    data[4096 + 0x58:4096 + 0x5C] = struct.pack("<I", arr_crc)
    P(f"entries CRC32 → 0x{arr_crc:08x} ({num_entries} x {entry_size}B)")
    hdr = bytearray(data[4096:4096 + 92])
    hdr[0x10:0x14] = b"\x00\x00\x00\x00"
    hdr_crc = zlib.crc32(bytes(hdr)) & 0xFFFFFFFF
    data[4096 + 0x10:4096 + 0x14] = struct.pack("<I", hdr_crc)
    P(f"header CRC32 → 0x{hdr_crc:08x}")
    open(PATCHED, "wb").write(bytes(data))

    # 4) 写回 sector 1-5 (不动 sector0 MBR), 再全量回读校验
    wr = f"{FIX}/w_gpt.bin"
    open(wr, "wb").write(bytes(data[4096:]))
    if sc("写 GPT sector1-5", lambda: fh.cmd_program(4, 1, wr)) is None:
        P("!!! 写入失败, GPT 未被破坏(只读了), 退出"); os._exit(2)
    if os.path.exists(TMP): os.remove(TMP)
    if sc("回读校验", lambda: fh.cmd_read(4, 0, 6, TMP, True)) is None: os._exit(2)
    rb = open(TMP, "rb").read()
    if rb == bytes(data):
        P("✓✓ GPT 回读与预期完全一致 (6 sectors)")
    else:
        P("!!! 回读不一致, 退出"); os._exit(2)

    P("发送 reset 重启...")
    sc("reset", lambda: fh.cmd_reset())
    P(f"===== 完成 ✓ boot_a 已设为 active, 重启中 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=1800)
if t.is_alive():
    P("!!! 全局超时"); os._exit(3)
