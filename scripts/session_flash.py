# -*- coding: utf-8 -*-
"""单会话一次性完成版: 等待9008 → 握手+loader → 健康检测(硬超时) →
读1扇区验证 → 全量分块校验 → 只续写坏段 → 复验 → 清userdata → 恢复devinfo(回读校验) → 重启
任何阶段失败: 快速诊断并退出, 绝不无限重试。"""
import sys, os, time, hashlib, threading, logging
import usb.core
sys.path.insert(0, r"F:/Temp/munch_tools/edl")

FIX = r"F:/Temp/munch_fix"
BAK = r"F:/Temp/munch_backup"
TMP = f"{FIX}/v_seg.bin"
LOADER_DEFAULT = "F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images/prog_ufs_firehose_sm8250_ddr_5.elf"
LOADER_ALT = "F:/Temp/munch_fix/loader_alt.bin"
LOADER = os.environ.get("EDL_LOADER", LOADER_DEFAULT)
USE_MEM = os.environ.get("EDL_MEMORY", "UFS")

SEG_SECTORS = 131072            # 512MB per segment (4096B sectors)
SEG_BYTES = SEG_SECTORS * 4096
PARTS = [
    ("metadata", 16640,   0x1000,   f"{FIX}/metadata_raw.img"),
    ("cust",     155648,  0x80000,  f"{FIX}/cust_raw.img"),
    ("super",    679936,  0x220000, f"{FIX}/super_raw.img"),
    ("rescue",   2916352, 0x8000,   f"{FIX}/rescue_raw.img"),
]
DEVINFO_SECTOR = 12288
TOTAL_MAP = {p[0]: p[2] for p in PARTS}   # 分区名 -> 总扇区数

P = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def md5_head(path, nbytes):
    h = hashlib.md5()
    with open(path, "rb") as f:
        left = nbytes
        while left > 0:
            c = f.read(min(1 << 24, left))
            if not c: break
            h.update(c); left -= len(c)
    return h.hexdigest() if left == 0 else None

def md5_offset(path, offset, nbytes):
    h = hashlib.md5()
    with open(path, "rb") as f:
        f.seek(offset)
        left = nbytes
        while left > 0:
            c = f.read(min(1 << 24, left))
            if not c: break
            h.update(c); left -= len(c)
    return h.hexdigest() if left == 0 else None

# ============ 主流程（在子线程运行，主线程管超时） ============
def pipeline():
    sys.argv = ["edl.py", f"--loader={LOADER}", f"--memory={USE_MEM}",
                "rs", "0", "1", "dummy.bin", "--sectorsize=4096"]
    import edl
    from edlclient.Library.firehose_client import firehose_client
    from edlclient.Library.Connection.usblib import usb_class
    from edlclient.Library.sahara import sahara
    from edlclient.Config.usb_ids import default_ids

    captured = []          # 捕获 loader 健康信息
    def cap_print(s):
        try:
            print(s, flush=True)
        except Exception:
            pass
        captured.append(str(s))

    # 同时捕获 logger 输出（firehose 的 INFO 日志走 logging 而非 printer）
    class _CapHandler(logging.Handler):
        def emit(self, record):
            try:
                captured.append(record.getMessage())
            except Exception:
                pass
    _h = _CapHandler()
    _h.setLevel(logging.INFO)
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    _root.addHandler(_h)

    P(f"使用 loader: {os.path.basename(LOADER)}")
    # 先等 9008 出现在总线上再打开（避免立即退出）
    import usb.backend.libusb1 as _lb
    _b = _lb.get_backend(find_library=None)
    P("等待 9008 接入（15 分钟内）...")
    _t0 = time.time()
    while time.time() - _t0 < 900:
        if _lb and usb.core.find(idVendor=0x05C6, idProduct=0x9008, backend=_b):
            break
        time.sleep(0.5)
    else:
        P("!!! 15 分钟未见 9008, 退出"); os._exit(2)
    time.sleep(1.0)  # 等驱动稳定
    cdc = usb_class(portconfig=default_ids, loglevel=logging.WARNING, serial_number=None)
    for _i in range(5):
        if cdc.connect():
            break
        time.sleep(1.0)
    else:
        P("!!! USB 打不开设备（重试5次失败）"); os._exit(2)
    P("USB 已打开，探测设备状态...")

    mode = None
    sah_obj = None
    # 注意: usblib 的 usbread 在 timeout>1 时会把超时值递增无限重试，
    # 只有从 1 起步数到 10 才返回空 —— 探测必须用 timeout=1（edl 同款）
    probe = cdc.read(0xC * 4, timeout=1)
    if probe and len(probe) > 1 and probe[0] == 0x01:
        P("状态: 全新 PBL (sahara) → 上传 loader")
        sah_obj = sahara(cdc, loglevel=logging.WARNING)
        sah_obj.programmer = LOADER
        v = 2
        # 解析 hello 包取版本
        try:
            pkt = sah_obj.ch.pkt_hello_req(probe)
            v = pkt.version
        except Exception:
            pass
        if sah_obj.cmd_info(version=v) is None:
            P("!!! cmd_info 失败"); os._exit(2)
        r = sah_obj.connect()
        if r.get("mode") != "sahara":
            P(f"!!! 二次握手异常: {r.get('mode')}"); os._exit(2)
        mode = sah_obj.upload_loader(version=v)
        P(f"upload_loader → {mode}")
    else:
        # 可能 loader 已加载: 发 nop 探测 firehose
        P("无 sahara hello，发 nop 探测 firehose...")
        try:
            cdc.write(b'<?xml version="1.0" ?><data><nop /></data>')
        except Exception as e:
            P(f"!!! nop 写失败: {e}"); os._exit(2)
        res = cdc.read(2048, timeout=1)
        if b"<?xml" in res:
            P("状态: loader 已加载且能响应 (firehose) → 直接复用会话")
            mode = "firehose"
        else:
            P("!!! 设备既无 sahara hello 也不响应 nop —— 会话已死。")
            P("!!! 请彻底断电: 抠电池排线等10秒再扣回，然后短接+插线重来。")
            os._exit(2)

    if not mode or "firehose" not in str(mode):
        P(f"!!! 未能进入 firehose (mode={mode}), 退出"); os._exit(2)

    # 建立 firehose（INFO 日志经 cap_print 捕获）；90 秒内建不起来 = 会话不健康
    class SahStub:
        programmer = LOADER
        serial = None
        bit64 = True
    _sah = sah_obj if sah_obj else SahStub
    _conn_result = {}

    def _do_fh_connect():
        try:
            _f = firehose_client(edl.args, cdc, _sah, logging.INFO, cap_print)
            _r = _f.connect(_sah)
            _conn_result["fhc"] = _f
            _conn_result["ok"] = _r
        except Exception as e:
            _conn_result["err"] = f"{type(e).__name__}: {str(e)[:150]}"

    _t = threading.Thread(target=_do_fh_connect, daemon=True)
    _t.start()
    _t.join(90)
    if _t.is_alive():
        P("!!! firehose 建立 90 秒未完成（卡在读第一扇区）= UFS 仍处于楔死状态")
        P("!!! 唯一解法: 抠电池排线断电 10 秒再扣回，然后短接+插线重来")
        os._exit(2)
    if "err" in _conn_result:
        P(f"!!! firehose 建立异常: {_conn_result['err']}"); os._exit(2)
    fhc = _conn_result["fhc"]
    ok = _conn_result["ok"]
    time.sleep(1.0)  # 让残余日志包进入 captured

    health = any(k in c for c in captured for k in
                 ("SKhynix", "Supported Functions", "supported functions",
                  "Binary build date", "Authenticated"))
    P(f"=== loader 健康检测: {'✓ 健康（存储初始化完成）' if health else '✗ 不健康'} ===")
    if not ok and not health:
        P("!!! firehose connect 失败")
    if not health:
        P("--- captured 尾部 ---")
        for c in captured[-12:]:
            print("   |", c.strip()[:120], flush=True)
        P("!!! loader 未完成存储初始化。建议: 断电10秒+换loader重试。退出码2")
        os._exit(2)

    fh = fhc.firehose
    P("=== 会话就绪 ✓ 以下操作全部复用此会话 ===")

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

    def read_seg(start, sectors):
        if os.path.exists(TMP): os.remove(TMP)
        return session_call(f"读 sector {start} x{sectors}",
                            lambda: fh.cmd_read(0, start, sectors, TMP, True))

    def check_segment(pname, base_sec, idx, local):
        start = base_sec + idx * SEG_SECTORS
        nsec = min(SEG_SECTORS, TOTAL_MAP[pname] - idx * SEG_SECTORS)
        if read_seg(start, nsec) is None: return "fail"
        if not os.path.exists(TMP) or os.path.getsize(TMP) < nsec * 4096:
            P(f"[校验] {pname}#{idx}: 读回文件异常"); return "fail"
        remote = md5_head(TMP, nsec * 4096)
        expect = md5_offset(local, idx * SEG_BYTES, nsec * 4096)
        r = "ok" if remote == expect else "bad"
        P(f"[校验] {pname}#{idx}: {'✓ 已正确写入' if r=='ok' else '✗ 需续写'}")
        return r

    def write_segment(pname, base_sec, idx, local):
        start = base_sec + idx * SEG_SECTORS
        nsec = min(SEG_SECTORS, TOTAL_MAP[pname] - idx * SEG_SECTORS)
        chunk = f"{FIX}/w_seg.img"
        with open(local, "rb") as src, open(chunk, "wb") as dst:
            src.seek(idx * SEG_BYTES)
            left = nsec * 4096
            while left > 0:
                c = src.read(min(1 << 24, left))
                if not c: break
                dst.write(c); left -= len(c)
        return session_call(f"续写 {pname}#{idx} @ sector {start} ({nsec*4096/1e6:.0f}MB)",
                            lambda: fh.cmd_program(0, start, chunk))

    # ---- 1. 会话健康测试 ----
    P("步骤0: 会话健康测试（读 1 扇区）")
    if read_seg(0, 1) is None:
        P("!!! 会话不可用, 退出"); os._exit(2)

    # ---- 2. 全量分块校验 ----
    P("步骤1: 全量分块校验开始（只读）")
    t0 = time.time()
    bad = []
    total_segs = sum((p[2] + SEG_SECTORS - 1) // SEG_SECTORS for p in PARTS)
    done = 0
    for pname, bsec, total_sectors, local in PARTS:
        nseg = (total_sectors + SEG_SECTORS - 1) // SEG_SECTORS
        for i in range(nseg):
            r = check_segment(pname, bsec, i, local)
            done += 1
            if r == "fail":
                P(f"!!! 通信失败于 {pname}#{i}, 退出(需重新短接)"); os._exit(2)
            if r == "bad":
                bad.append((pname, bsec, i, local))
            P(f"进度 {done}/{total_segs} 段, 坏段 {len(bad)}, 用时 {time.time()-t0:.0f}s")
    P(f"校验完成: 坏段 {len(bad)}/{total_segs}")

    # ---- 3. 续写 + 复验 ----
    if bad:
        P(f"步骤2: 续写 {len(bad)} 个坏段")
        for pname, bsec, i, local in bad:
            if write_segment(pname, bsec, i, local) is None:
                P("!!! 续写失败, 退出"); os._exit(2)
        P("步骤3: 复验坏段")
        still = []
        for pname, bsec, i, local in bad:
            if check_segment(pname, bsec, i, local) != "ok":
                still.append((pname, bsec, i, local))
        if still:
            P(f"复验 {len(still)} 段未过, 第二轮重写...")
            for pname, bsec, i, local in still:
                write_segment(pname, bsec, i, local)
            bad2 = [f"{p}#{i}" for p, b, i, l in still if check_segment(p, b, i, l) != "ok"]
            if bad2:
                P(f"!!! 第二轮仍未过: {bad2}, 退出"); os._exit(2)
        P("全部坏段修复 ✓")
    else:
        P("数据全部完好, 无需续写 ✓")

    # ---- 4. 收尾 ----
    P("步骤4: 清 userdata 前 64MB（开机自动完成格式化）")
    if session_call("erase userdata", lambda: fh.cmd_erase(0, 2949120, 16384)) is None:
        P("!!! 失败, 退出"); os._exit(2)

    P("步骤5: 恢复原版 devinfo")
    dv = f"{BAK}/devinfo_backup.bin"
    if session_call("写 devinfo", lambda: fh.cmd_program(0, DEVINFO_SECTOR, dv)) is None:
        P("!!! 失败, 退出"); os._exit(2)
    nsec_dv = min(256, os.path.getsize(dv) // 4096)
    if read_seg(DEVINFO_SECTOR, nsec_dv) is None: os._exit(2)
    h1 = md5_head(TMP, nsec_dv * 4096)
    h2 = md5_head(dv, nsec_dv * 4096)
    P(f"devinfo 回读: {'✓ 匹配' if h1 == h2 else '✗ 不匹配'}")
    if h1 != h2: os._exit(2)

    P("步骤6: 重启手机")
    session_call("reset", lambda: fh.cmd_reset())
    P("===== 全部完成 ✓ 手机正在重启, 等待约 1-2 分钟看屏幕 =====")
    os._exit(0)

t = threading.Thread(target=pipeline, daemon=True)
t.start()
t.join(timeout=3600)   # 全局 60 分钟兜底
if t.is_alive():
    P("!!! 全局超时 60 分钟, 强制退出（避免无限重试）")
    os._exit(3)
