# -*- coding: utf-8 -*-
"""Android sparse image -> raw 转换器（MiFlash 会做这步，bkerler edl 的 qfil 不会）"""
import struct, sys, os, time

def unsparse(src, dst):
    t0 = time.time()
    f = open(src, 'rb')
    magic, major, minor, fhs, chs, blk, tblks, tchunks, csum = struct.unpack("<IHHHHIIII", f.read(28))
    assert magic == 0xED26FF3A, f"{src} 不是 sparse 镜像"
    w = open(dst, 'wb')
    done = 0
    zeros = b'\x00' * (1 << 24)
    for i in range(tchunks):
        t, res, chunk_sz, total_sz = struct.unpack("<2H2I", f.read(12))
        data_sz = total_sz - 12
        if t == 0xCAC1:      # RAW
            assert data_sz == chunk_sz * blk, f"RAW chunk 尺寸异常"
            rem = data_sz
            while rem:
                buf = f.read(min(1 << 24, rem))
                w.write(buf); rem -= len(buf)
            done += chunk_sz
        elif t == 0xCAC2:    # FILL
            fill = f.read(4)
            n = chunk_sz * blk
            unit = fill * (blk // 4)
            while n:
                b2 = unit[:min(n, len(unit))]
                w.write(b2); n -= len(b2)
            done += chunk_sz
        elif t == 0xCAC3:    # DONTCARE -> 全 0
            n = chunk_sz * blk
            while n:
                b2 = zeros[:min(n, len(zeros))]
                w.write(b2); n -= len(b2)
            done += chunk_sz
        elif t == 0xCAC4:    # CRC32，无输出
            f.read(4)
        else:
            raise Exception(f"未知 chunk 类型 0x{t:04x}")
        if (i + 1) % 50 == 0 or i == tchunks - 1:
            print(f"  {os.path.basename(src)}: chunk {i+1}/{tchunks}, 块 {done}/{tblks}", flush=True)
    f.close(); w.close()
    out = done * blk
    print(f"[OK] {src} -> {dst}: {out} 字节, 耗时 {time.time()-t0:.1f}s", flush=True)
    return out

if __name__ == "__main__":
    jobs = [
        ("metadata.img", "metadata_raw.img", 4096),
        ("rescue.img",   "rescue_raw.img",   32768),
        ("cust.img",     "cust_raw.img",     524288),
        ("super.img",    "super_raw.img",    2228224),
    ]
    base = r"F:/Temp/munch_rom/munch_images_V14.0.7.0.TLMCNXM_13.0/images"
    outdir = r"F:/Temp/munch_fix"
    os.makedirs(outdir, exist_ok=True)
    for src, dst, nps in jobs:
        out = unsparse(os.path.join(base, src), os.path.join(outdir, dst))
        exp = nps * 4096
        mark = "✓" if out == exp else f"✗ 期望 {exp}"
        print(f"  校验 {dst}: {out} vs 扇区数×4096={exp} {mark}", flush=True)
