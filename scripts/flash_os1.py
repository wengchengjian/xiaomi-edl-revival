# -*- coding: utf-8 -*-
"""OS1.0.15.0 全量刷写: 32 分区(官方语义, 大分区512MB分块) → 恢复devinfo锁定态 → reset
等待9008 → 握手 → loader健康检测 → 逐分区写+回读MD5校验(失败重试一轮)"""
import sys, os, time, hashlib, threading, logging, json
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
BAK = r"F:/Temp/munch_backup"
LOADER = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images/prog_ufs_firehose_sm8250_ddr_5.elf"
TMP = f"{FIX}/v_seg.bin"
CHUNK = f"{FIX}/w_seg.img"
PARTS = json.load(open(f"{FIX}/os1_flash_list.json"))
DEVINFO_SEC = 12288
SEG_SECTORS = 131072          # 512MB per segment
SEG_BYTES = SEG_SECTORS * 4096
DEVINFO_BAK = f"{BAK}/devinfo_backup.bin"

P = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def md5_file(path, nbytes=None, offset=0):
    h = hashlib.md5()
    left = nbytes
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            c = f.read(1 << 24) if left is None else f.read(min(1 << 24, left))
            if not c: break
            h.update(c)
            if left is not None:
                left -= len(c)
                if left <= 0: break
    return h.hexdigest()

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

    P("等待 9008（20 分钟）... 长按电源键12秒断电 → 黑屏后按住工程线按钮插线 → 叮咚松开")
    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    _t0 = time.time()
    while time.time() - _t0 < 1200:
        if _lb and usb.core.find(idVendor=0x05C6, idProduct=0x9008, backend=_b):
            break
        time.sleep(0.5)
    else:
        P("!!! 20 分钟未见 9008"); os._exit(2)
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
    if _t.is_alive(): P("!!! UFS 楔死, 断电重来"); os._exit(2)
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

    def read_segs(lun, start, sectors):
        if os.path.exists(TMP): os.remove(TMP)
        return sc(f"  读 LUN{lun}@{start} x{sectors}",
                  lambda: fh.cmd_read(lun, start, sectors, TMP, True))

    def verify_range(lun, sec, img, size, label):
        """回读 [sec, sec+size) 与 img 前 size 字节比对 MD5; 大于512MB分段"""
        left = size; off = 0; base = sec
        while left > 0:
            nsec = min(SEG_SECTORS, (left + 4095) // 4096)
            if read_segs(lun, base, nsec) is None: return False
            if not os.path.exists(TMP) or os.path.getsize(TMP) < min(left, nsec * 4096):
                P(f"  ✗ {label}@{off} 读回异常"); return False
            n = min(left, nsec * 4096)
            if md5_file(TMP, n) != md5_file(img, n, off):
                P(f"  ✗ {label}@{off} MD5 不匹配"); return False
            left -= n; off += n; base += nsec
        return True

    t_all = time.time(); failed = []
    for part in PARTS:
        name, lun, sec, img, sz = part["name"], part["lun"], part["sec"], part["img"], part["size"]
        P(f"--- {name}: LUN{lun} @sec {sec}, {sz/1048576:.1f}MB ---")
        for attempt in (1, 2):
            if sz > SEG_BYTES:
                # 分块写入
                ok = True; left = sz; off = 0; base = sec
                while left > 0:
                    nsec = min(SEG_SECTORS, (left + 4095) // 4096)
                    with open(img, "rb") as src, open(CHUNK, "wb") as dst:
                        src.seek(off); dst.write(src.read(nsec * 4096))
                    if sc(f"  写段@{off//(1<<20)}MB", lambda b=base, c=CHUNK: fh.cmd_program(lun, b, c)) is None:
                        ok = False; break
                    left -= nsec * 4096; off += nsec * 4096; base += nsec
                if not ok or not verify_range(lun, sec, img, sz, name):
                    P(f"  第{attempt}轮 {name} 失败" + (", 重试" if attempt == 1 else ""))
                    continue
                P(f"  ✓ {name} (分块校验通过)")
                break
            else:
                nsec = (sz + 4095) // 4096
                if sc(f"  写", lambda: fh.cmd_program(lun, sec, img)) is None:
                    continue
                if read_segs(lun, sec, nsec) is None: continue
                if os.path.exists(TMP) and os.path.getsize(TMP) >= min(sz, nsec*4096) \
                   and md5_file(TMP, sz) == md5_file(img):
                    P(f"  ✓ {name}")
                    break
                P(f"  第{attempt}轮 {name} 校验失败" + (", 重试" if attempt == 1 else ""))
        else:
            P(f"  ✗✗ {name} 两轮均失败"); failed.append(name)

    # ---- 恢复 devinfo 锁定态 ----
    if not failed:
        P("--- devinfo: 恢复原版锁定态 @LUN0 sec 12288 ---")
        nsec = 16  # 64KB
        if sc("写 devinfo", lambda: fh.cmd_program(0, DEVINFO_SEC, DEVINFO_BAK)) is None or \
           read_segs(0, DEVINFO_SEC, nsec) is None or \
           md5_file(TMP, nsec * 4096) != md5_file(DEVINFO_BAK, nsec * 4096):
            P("✗✗ devinfo 恢复失败"); failed.append("devinfo")
        else:
            P("✓ devinfo 锁定态已恢复并校验")

    P(f"===== 汇总: 失败 {len(failed)}/{len(PARTS)+1}: {failed if failed else '无'} =====")
    if failed:
        P("!!! 有失败分区, 不重启"); os._exit(2)
    sc("reset", lambda: fh.cmd_reset())
    P(f"===== 完成 ✓ 总用时 {time.time()-t_all:.0f}s, 重启中 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=2700)
if t.is_alive():
    P("!!! 全局超时"); os._exit(3)
