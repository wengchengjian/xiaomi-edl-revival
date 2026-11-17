# -*- coding: utf-8 -*-
"""单会话补刷固件分区: 读 fw_parts.json → 逐分区写+回读校验 → 失败重试一轮 → reset
清单来自官方 rawprogram XML 精确 sector 偏移。不碰 GPT/devinfo/EFS。"""
import sys, os, time, hashlib, threading, logging, json
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
BAK = r"F:/Temp/munch_backup"
ROM = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images"
LOADER = os.environ.get("EDL_LOADER", f"{ROM}/prog_ufs_firehose_sm8250_ddr_5.elf")
TMP = f"{FIX}/v_seg.bin"
PARTS = json.load(open(f"{FIX}/fw_parts.json"))

P = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def md5_file(path, nbytes=None):
    h = hashlib.md5()
    left = nbytes
    with open(path, "rb") as f:
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

    P("等待 9008（15 分钟）... 长按电源键12秒断电 → 黑屏后按住工程线按钮插线 → 叮咚松开")
    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    _t0 = time.time()
    while time.time() - _t0 < 900:
        if _lb and usb.core.find(idVendor=0x05C6, idProduct=0x9008, backend=_b):
            break
        time.sleep(0.5)
    else:
        P("!!! 15 分钟未见 9008"); os._exit(2)
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
        return sc(f"读 LUN{lun}@{start} x{sectors}",
                  lambda: fh.cmd_read(lun, start, sectors, TMP, True))

    t_all = time.time(); failed = []
    for part in PARTS:
        name, lun, sec, img = part["name"], part["lun"], part["sec"], part["img"]
        nsec = (part["size"] + 4095) // 4096
        sz = part["size"]
        expect = md5_file(img)
        P(f"--- {name}: LUN{lun} @sec {sec}, {part['size']/1048576:.2f}MB ---")
        t0 = time.time()
        if sc(f"写 {name}", lambda: fh.cmd_program(lun, sec, img)) is None:
            failed.append(name); continue
        P(f"    写 {time.time()-t0:.0f}s")
        if read_segs(lun, sec, nsec) is None: failed.append(name); continue
        if not os.path.exists(TMP) or os.path.getsize(TMP) < nsec * 4096:
            P("    ✗ 读回异常"); failed.append(name); continue
        if md5_file(TMP, sz) == expect:
            P(f"    ✓ {name}")
        else:
            P(f"    ✗ {name} 校验失败 → 重试")
            if sc(f"重写 {name}", lambda: fh.cmd_program(lun, sec, img)) is None:
                failed.append(name); continue
            if read_segs(lun, sec, nsec) is None or md5_file(TMP, sz) != expect:
                P(f"    ✗ {name} 仍失败"); failed.append(name)
            else:
                P(f"    ✓ {name} 重试通过")

    P(f"===== 汇总: 失败 {len(failed)}/{len(PARTS)}: {failed if failed else '无'} =====")
    if failed:
        P("!!! 有失败分区, 不重启"); os._exit(2)
    sc("reset", lambda: fh.cmd_reset())
    P(f"===== 完成 ✓ 总用时 {time.time()-t_all:.0f}s, 重启中 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=1800)
if t.is_alive():
    P("!!! 全局超时"); os._exit(3)
