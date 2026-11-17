# -*- coding: utf-8 -*-
"""单会话刷启动分区版: 等9008 → 握手+loader → 健康检测(90s熔断) →
逐分区写入 boot/vendor_boot/dtbo/vbmeta/vbmeta_system/misc (a+b双槽) →
每个分区写后立即回读校验 → 失败重试一轮 → reset。
不碰 devinfo (保持原版锁定态)。任何阶段失败快速退出。"""
import sys, os, time, hashlib, threading, logging
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
ROM = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images"
LOADER_DEFAULT = f"{ROM}/prog_ufs_firehose_sm8250_ddr_5.elf"
LOADER_ALT = "F:/Temp/munch_fix/loader_alt.bin"
LOADER = os.environ.get("EDL_LOADER", LOADER_DEFAULT)
TMP = f"{FIX}/v_seg.bin"

# (name, lun, start_sector, image_path)
PARTS = [
    ("misc",           0, 1024,    f"{ROM}/misc.img"),
    ("vbmeta_system_a",0, 2908160, f"{ROM}/vbmeta_system.img"),
    ("vbmeta_system_b",0, 2908192, f"{ROM}/vbmeta_system.img"),
    ("boot_a",         4, 145030,  f"{ROM}/boot.img"),
    ("vbmeta_a",       4, 194790,  f"{ROM}/vbmeta.img"),
    ("dtbo_a",         4, 194822,  f"{ROM}/dtbo.img"),
    ("vendor_boot_a",  4, 224054,  f"{ROM}/vendor_boot.img"),
    ("boot_b",         4, 393654,  f"{ROM}/boot.img"),
    ("vbmeta_b",       4, 443414,  f"{ROM}/vbmeta.img"),
    ("dtbo_b",         4, 443446,  f"{ROM}/dtbo.img"),
    ("vendor_boot_b",  4, 460390,  f"{ROM}/vendor_boot.img"),
]

P = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            c = f.read(1 << 24)
            if not c: break
            h.update(c)
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

    P(f"使用 loader: {os.path.basename(LOADER)}")
    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    P("等待 9008 接入（15 分钟内）... 请按住工程线按钮插线，叮咚后松开")
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
        P("!!! USB 打不开设备"); os._exit(2)
    P("USB 已打开，探测设备状态...")

    mode = None; sah_obj = None
    probe = cdc.read(0xC * 4, timeout=1)
    if probe and len(probe) > 1 and probe[0] == 0x01:
        P("状态: 全新 PBL (sahara) → 上传 loader")
        sah_obj = sahara(cdc, loglevel=logging.WARNING)
        sah_obj.programmer = LOADER
        v = 2
        try: v = sah_obj.ch.pkt_hello_req(probe).version
        except Exception: pass
        if sah_obj.cmd_info(version=v) is None:
            P("!!! cmd_info 失败"); os._exit(2)
        r = sah_obj.connect()
        if r.get("mode") != "sahara":
            P(f"!!! 二次握手异常: {r.get('mode')}"); os._exit(2)
        mode = sah_obj.upload_loader(version=v)
        P(f"upload_loader → {mode}")
    else:
        P("无 sahara hello，发 nop 探测 firehose...")
        try: cdc.write(b'<?xml version="1.0" ?><data><nop /></data>')
        except Exception as e:
            P(f"!!! nop 写失败: {e}"); os._exit(2)
        res = cdc.read(2048, timeout=1)
        if b"<?xml" in res:
            P("状态: loader 已加载 (firehose) → 复用会话")
            mode = "firehose"
        else:
            P("!!! 会话已死。请彻底断电(扣电池排线10秒)后短接重来。"); os._exit(2)

    if not mode or "firehose" not in str(mode):
        P(f"!!! 未能进入 firehose (mode={mode})"); os._exit(2)

    class SahStub:
        programmer = LOADER; serial = None; bit64 = True
    _sah = sah_obj if sah_obj else SahStub
    _conn_result = {}
    def _do_fh_connect():
        try:
            _f = firehose_client(edl.args, cdc, _sah, logging.INFO, cap_print)
            _r = _f.connect(_sah)
            _conn_result["fhc"] = _f; _conn_result["ok"] = _r
        except Exception as e:
            _conn_result["err"] = f"{type(e).__name__}: {str(e)[:150]}"
    _t = threading.Thread(target=_do_fh_connect, daemon=True); _t.start()
    _t.join(90)
    if _t.is_alive():
        P("!!! firehose 90 秒未建立 = UFS 楔死, 扣电池排线断电10秒重来"); os._exit(2)
    if "err" in _conn_result:
        P(f"!!! firehose 建立异常: {_conn_result['err']}"); os._exit(2)
    fhc = _conn_result["fhc"]; ok = _conn_result["ok"]
    time.sleep(1.0)
    health = any(k in c for c in captured for k in
                 ("SKhynix", "Supported Functions", "supported functions",
                  "Binary build date", "Authenticated"))
    P(f"=== loader 健康检测: {'✓ 健康' if health else '✗ 不健康'} ===")
    if not health:
        for c in captured[-12:]: print("   |", c.strip()[:120], flush=True)
        P("!!! loader 未完成存储初始化, 退出"); os._exit(2)

    fh = fhc.firehose
    P("=== 会话就绪 ✓ ===")

    def session_call(desc, fn, retries=1):
        for attempt in range(retries + 1):
            try:
                r = fn()
                P(f"{desc} ✓")
                return r
            except Exception as e:
                P(f"{desc} 失败({attempt+1}/{retries+1}): {type(e).__name__}: {str(e)[:120]}")
                time.sleep(2)
        return None

    def read_segs(lun, start, sectors):
        if os.path.exists(TMP): os.remove(TMP)
        return session_call(f"读 LUN{lun} sector {start} x{sectors}",
                            lambda: fh.cmd_read(lun, start, sectors, TMP, True))

    t_all = time.time()
    failed = []
    for name, lun, start_sec, img in PARTS:
        img_size = os.path.getsize(img)
        nsec = (img_size + 4095) // 4096
        expect = md5_file(img)
        P(f"--- 写入 {name}: LUN{lun} @sector {start_sec}, {img_size/1048576:.2f}MB ---")
        t0 = time.time()
        r = session_call(f"写 {name}", lambda: fh.cmd_program(lun, start_sec, img), retries=1)
        if r is None:
            failed.append(name); continue
        P(f"    写入用时 {time.time()-t0:.0f}s, 回读校验中...")
        if read_segs(lun, start_sec, nsec) is None:
            failed.append(name); continue
        if not os.path.exists(TMP) or os.path.getsize(TMP) < nsec * 4096:
            P(f"    ✗ 读回文件异常"); failed.append(name); continue
        remote = md5_file(TMP)
        if remote == expect:
            P(f"    ✓ {name} 校验通过")
        else:
            P(f"    ✗ {name} 校验失败 → 重试一轮")
            r2 = session_call(f"重写 {name}", lambda: fh.cmd_program(lun, start_sec, img))
            if r2 is None: failed.append(name); continue
            if read_segs(lun, start_sec, nsec) is None: failed.append(name); continue
            if md5_file(TMP) == expect:
                P(f"    ✓ {name} 重试后校验通过")
            else:
                P(f"    ✗ {name} 重试仍失败"); failed.append(name)

    P(f"===== 写入汇总: 失败 {len(failed)}/{len(PARTS)}: {failed if failed else '无'} =====")
    if failed:
        P("!!! 存在失败分区, 不重启, 请断电重来或诊断"); os._exit(2)
    P("全部启动分区写入并校验通过 ✓ 发送 reset 重启...")
    session_call("reset", lambda: fh.cmd_reset())
    P(f"===== 完成 ✓ 总用时 {time.time()-t_all:.0f}s, 手机重启中, 1-2 分钟后看屏幕 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=5400)   # 90 分钟兜底
if t.is_alive():
    P("!!! 全局超时, 强制退出"); os._exit(3)
