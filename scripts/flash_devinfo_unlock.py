# -*- coding: utf-8 -*-
"""devinfo 解锁实验: 等9008 → 握手+loader → 健康检测 →
写入解锁版 devinfo (LUN0 @12288, is_unlocked=1, is_unlock_critical=1) →
回读校验 → reset。可逆: 原版备份在 munch_backup/devinfo_backup.bin"""
import sys, os, time, hashlib, threading, logging
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
ROM = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images"
LOADER = f"{ROM}/prog_ufs_firehose_sm8250_ddr_5.elf"
IMG = f"{FIX}/devinfo_unlocked.bin"
TMP = f"{FIX}/v_devinfo.bin"
DEVINFO_LUN, DEVINFO_SEC = 0, 12288   # devinfo @ 0x3000000 字节 / 4096

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

    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    P("等待 9008 接入（15 分钟内）... 长按电源12秒断电 → 黑屏后按住工程线按钮插线 → 叮咚松开")
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
    fhc = _conn_result["fhc"]
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

    # 先回读当前 devinfo 头部留证
    if os.path.exists(TMP): os.remove(TMP)
    try:
        fh.cmd_read(DEVINFO_LUN, DEVINFO_SEC, 16, TMP, True)
        cur = open(TMP, "rb").read(64)
        P(f"当前 devinfo 头: {cur[:16]}  0x10={cur[0x10]} 0x18={cur[0x18]} 0x90={int.from_bytes(cur[0x90:0x94],'little')}")
    except Exception as e:
        P(f"读当前 devinfo 失败(继续): {e}")

    # 写入解锁版 (64KB = 16 sectors)
    expect = md5_file(IMG)
    P(f"写入解锁 devinfo: LUN{DEVINFO_LUN} @sec {DEVINFO_SEC}, 64KB, 0x10=1 0x18=1")
    fh.cmd_program(DEVINFO_LUN, DEVINFO_SEC, IMG)
    P("写 ✓ 回读校验...")
    if os.path.exists(TMP): os.remove(TMP)
    fh.cmd_read(DEVINFO_LUN, DEVINFO_SEC, 16, TMP, True)
    remote = md5_file(TMP)
    if remote == expect:
        P("✓✓ 解锁 devinfo 写入并校验通过")
    else:
        P(f"✗ 校验失败 remote={remote} expect={expect} → 重试一轮")
        fh.cmd_program(DEVINFO_LUN, DEVINFO_SEC, IMG)
        if os.path.exists(TMP): os.remove(TMP)
        fh.cmd_read(DEVINFO_LUN, DEVINFO_SEC, 16, TMP, True)
        if md5_file(TMP) == expect:
            P("✓ 重试后校验通过")
        else:
            P("✗✗ 重试仍失败, 不重启"); os._exit(2)

    # 确认关键位
    rd = open(TMP, "rb").read(0x100)
    P(f"写入后: magic={rd[:16]}  0x10(is_unlocked)={rd[0x10]}  0x18(unlock_critical)={rd[0x18]}  0x90={int.from_bytes(rd[0x90:0x94],'little')}")

    P("发送 reset 重启...")
    try: fh.cmd_reset()
    except Exception: pass
    P("===== 完成 ✓ 手机重启中, 看屏幕: 橙字警告=EDL解锁成立 / 红屏=有RPMB交叉校验 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=3600)
if t.is_alive():
    P("!!! 全局超时, 强制退出"); os._exit(3)
