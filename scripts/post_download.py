# -*- coding: utf-8 -*-
"""下载完成后立即执行: MD5校验 -> 解压 -> 核对防回滚参数 -> 列分区清单"""
import hashlib, tarfile, os, struct, sys, re, json, time

TGZ = r"F:/Temp/munch_os1.tgz"
EXPECT_SIZE = 6236143171
EXPECT_MD5 = "2f6342532f8800153457149a77bf82fe"
OUT = r"F:/Temp/munch_os1"
IMG = os.path.join(OUT, "images")

def P(*a):
    print(*a, flush=True)

# ---------- 1. 等待文件大小到位 ----------
while True:
    if not os.path.exists(TGZ):
        P("文件不存在"); sys.exit(1)
    sz = os.path.getsize(TGZ)
    P(f"当前大小: {sz} / {EXPECT_SIZE}")
    if sz >= EXPECT_SIZE:
        break
    time.sleep(20)

# ---------- 2. MD5 校验 ----------
P("计算 MD5 ...")
h = hashlib.md5()
with open(TGZ, "rb") as f:
    while True:
        c = f.read(1 << 24)
        if not c: break
        h.update(c)
got = h.hexdigest()
P(f"MD5: {got}")
P(f"期望: {EXPECT_MD5}")
if got != EXPECT_MD5:
    P("!!! MD5 不匹配，文件损坏 !!!"); sys.exit(2)
P("MD5 校验通过")

# ---------- 3. 解压 images/ ----------
P("解压 ...")
os.makedirs(OUT, exist_ok=True)
with tarfile.open(TGZ, "r:gz") as t:
    members = [m for m in t.getmembers() if "/images/" in m.name or m.name.endswith(".txt")]
    t.extractall(OUT, members=members)
P(f"解压完成 -> {IMG}")

# ---------- 4. 防回滚核对 ----------
av = os.path.join(IMG, "anti_version.txt")
if os.path.exists(av):
    P(f"=== anti_version.txt: {open(av).read().strip()} ===")
else:
    P("无 anti_version.txt")

# vbmeta rollback_index
for name in ["vbmeta.img", "vbmeta_system.img"]:
    p = os.path.join(IMG, name)
    if not os.path.exists(p): continue
    d = open(p, "rb").read()
    if d[:4] != b"AVB0":
        P(f"{name}: 非AVB0"); continue
    rb = struct.unpack(">Q", d[64:72])[0]
    flags = struct.unpack(">I", d[72:76])[0]
    P(f"{name}: rollback_index={rb} flags=0x{flags:x}")

# ---------- 5. 分区清单 ----------
rows = []
for fn in sorted(os.listdir(IMG)):
    if not fn.startswith("rawprogram"): continue
    xml = open(os.path.join(IMG, fn), encoding="utf-8").read()
    for m in re.finditer(r'<program [^>]*?/>', xml):
        tag = m.group(0)
        def g(k):
            r = re.search(k + r'="([^"]*)"', tag)
            return r.group(1) if r else ""
        rows.append((int(g("physical_partition_number") or 0), g("label"),
                     g("filename"), int(g("num_partition_sectors") or 0)))
P(f"=== 分区条目 {len(rows)} 个 ===")
tot = 0
for lun, label, fname, secs in rows:
    p = os.path.join(IMG, fname) if fname else None
    ok = p and os.path.exists(p)
    tot += secs * 4096 if ok else 0
    P(f"LUN{lun} {label:20s} {secs*4/1024:9.1f}MB {fname} {'OK' if ok else 'MISSING'}")
P(f"总镜像量: {tot/1048576:.0f} MB")
json.dump([{"lun": l, "label": la, "file": f, "secs": s} for l, la, f, s in rows],
          open(r"F:/Temp/munch_fix/os1_parts.json", "w"), indent=1)
P("=== 全部完成 ===")
