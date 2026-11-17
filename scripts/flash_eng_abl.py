# -*- coding: utf-8 -*-
"""Step1: 刷入工程 abl 到 abl_a (LUN4 @127878, 208,896B) → 回读校验 → reset
不碰其他任何分区。失败/成功均可由 EDL 刷回官方 abl 完全恢复。"""
import sys, os, time, hashlib, threading, logging
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
LOADER = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images/prog_ufs_firehose_sm8250_ddr_5.elf"
IMG = f"{FIX}/eng_abl.elf"
TMP = f"{FIX}/v_engabl.bin"
ABL_LUN, ABL_SEC = 4, 127878

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

    P("等待 9008（20 分钟）... 长按电源12秒断电 → 黑屏后按住工程线按钮插线 → 叮咚松开")
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

    sah_obj = None
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
        sah_obj.upload_loader(version=v)
    else:
        cdc.write(b'<?xml version="1.0" ?><data><nop /></data>')
        res = cdc.read(2048, timeout=1)
        if b"<?xml" not in res:
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

    sz = os.path.getsize(IMG)
    nsec = (sz + 4095) // 4096
    expect = md5_file(IMG)
    P(f"--- eng abl: LUN{ABL_LUN} @sec {ABL_SEC}, {sz}B, {nsec} sectors ---")
    ok = False
    for attempt in (1, 2):
        try:
            fh.cmd_program(ABL_LUN, ABL_SEC, IMG)
            P("写 ✓")
        except Exception as e:
            P(f"写失败: {str(e)[:100]}"); continue
        if os.path.exists(TMP): os.remove(TMP)
        try:
            fh.cmd_read(ABL_LUN, ABL_SEC, nsec, TMP, True)
        except Exception as e:
            P(f"读失败: {str(e)[:100]}"); continue
        if os.path.exists(TMP) and os.path.getsize(TMP) >= sz and md5_file(TMP, sz) == expect:
            P("✓✓ 回读 MD5 校验通过"); ok = True; break
        P(f"第{attempt}轮校验失败, 重试")
    if not ok:
        P("!!! 两轮失败, 不重启"); os._exit(2)
    fh.cmd_reset()
    P("===== 完成 ✓ 手机重启, 应进 Fastboot(工程abl) =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=1500)
if t.is_alive():
    P("!!! 全局超时"); os._exit(3)
