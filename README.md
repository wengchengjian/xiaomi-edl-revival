# munch-9008-revival

Redmi K40S (codename `munch`, Snapdragon 870 / SM8250-AC) 9008 深刷救砖工具集与完整技术复盘。

从"The System has been destroyed"红屏死机，到 EDL 全量重建、系统完全恢复的全过程沉淀。

> **适用对象**：小米/红米高通机型（尤其 SM8250 平台）变砖救援、EDL 深刷研究者。
> 所有流程在一台量产 munch 上完整实测（2026-11）。

---

## 一、红屏根因与修复（完整复盘）

### 现象

开机红屏 `The System has been destroyed`，无法进系统/fastboot，只能进 9008 (EDL)。

### 根因链（两个独立问题叠加）

1. **sparse 镜像错写**：官方线刷包里 `super/cust/metadata/rescue` 是 Android sparse 格式
   （magic `3aff26ed`）。官方 MiFlash 会自动解稀疏，而 bkerler/edl 的 qfil 模式**直接把
   sparse 字节流烧进分区** → 分区内容整体错位 → AVB 校验 system 失败 → 红屏。
   - 解法：`scripts/unsparse.py` 手动解稀疏为 raw 后再刷。

2. **镜像版本与设备 RPMB 记录不自洽**：刷入版本的 AVB rollback_index 必须 ≥ 设备 RPMB
   中记录的历史最高值（OTA 会抬高它，且**只升不降、与 BL 锁状态无关**）。
   - 解法：**刷比设备历史版本新的官方线刷包**（本例为 OS1.0.15.0.ULMCNXM）。

### 修复流程总览

```
9008 进线 → 上传授权 firehose loader → 单会话脚本：
  ① 解稀疏 super/cust/metadata/rescue → 按设备真实 GPT 偏移写入 → 回读 MD5 校验
  ② 全量刷入最新官方包（32 分区，严格官方语义：只刷 a 槽、不碰 GPT/devinfo/EFS/校准）
  ③ 每分区写后回读校验，失败自动重试
  ④ reset 重启 → 绿字 AVB 通过 → 系统复活
```

### 关键经验

| 坑 | 说明 |
|---|---|
| **不接电池 = UFS 抽签** | 纯 USB 供电时 UFS 初始化随机失败，loader 时好时坏；接回电池立竿见影 |
| **会话中断 = UFS 楔死** | 写入中途杀进程/掉 USB，重启进 9008 也救不回，必须等（勿反复重试） |
| **edl 工具 sahara 危险路径** | 握手失败时会发送 21 字节 ARM 载荷打掉会话，需自写最小握手绕开（见 `session_flash.py`） |
| **firehose.py 类型混淆** | Python 3.13 下第 736 行 bytes/dict 二义，需补丁 `hasattr(rsp.data,'get')` |
| **假速度** | EDL 日志出现数百 MB/s = 幻影成功（UFS 未真正写入），以回读校验为准 |
| **非 4K 整除镜像** | 回读校验必须只比对前 N 字节，否则尾部 padding 造成假阴性 |
| **slot unbootable 标记** | 反复启动失败后 bootloader 置 GPT 属性 bit55，锁定态又拒绝 `set_active` → 需 EDL 改 GPT 属性位（priority/tries/清除 bit55） |

---

## 二、小米安全机制地图（全实测）

这是本项目最有价值的部分——**每一堵墙都实测过边界**。

### 信任链

```
fuse (物理烧死，含 OEM 公钥哈希)          ← one-time programmable，不可改
  └→ PBL (硅片 ROM)                       ← 改上层即硬砖（同平台 Black Shark 4 实测）
      └→ XBL (验 tz/hyp/abl 的小米签名)
          └→ abl (验 vbmeta 签名 + boot hash)
              └→ vbmeta (hash/hashtree descriptor 覆盖 boot/dtbo/vendor_boot/system...)
```

### BL 解锁的真相（实测）

- **解锁授权是服务器 token 制**：`fastboot oem unlock` 返回 `Token Verify Failed, Reboot
  the device`。`devinfo` 分区的 `is_unlocked`(0x10)/`is_unlock_critical`(0x18) 只是**结果
  存储**，直接写 1 不被 abl 采信。
- **提权漏洞 ≠ root ≠ 解锁**：root 打破的是系统内边界（SELinux/UID），BL 锁保护的是启动
  链完整性——验证在 root 产生之前就已完成。任何提权洞拿到的"临时 root"写什么分区都会被
  签名链拒绝（boot→AVB hash、vbmeta→RSA、abl→XBL 签名）。
- **合法后门只有一类**：小米自己签名的工程文件（factory ROM）。工厂 abl 不含 token 校验
  （产线需要），XBL 视其与量产 abl 同权（同一把私钥签名）。

### RPMB 反降级（工厂路线的终结者，实测）

- 工厂包（FACTORY-MUNCH-0309，2022-03）单刷 abl / 六件套 / **全工厂化 23 分区**，均红屏。
- 红屏由 eng abl 输出 → 证明 XBL 接受工程签名、eng abl 正常运行。
- 死因：**eng vbmeta 的 rollback_index (3048) < 设备 RPMB 记录 (~3696)**——OTA 抬高过的
  设备触发反降级拒绝。
- RPMB 硬件边界：rollback 单调递增；写需 HMAC（密钥芯片内锁定，CPU 不可读）；读响应带
  MAC 验证；SoC→UFS 通路无注入点。**无软件绕法**。唯一物理解 = 换 UFS 芯片（BGA 级维修）。
- **结论：工厂包只救"未 OTA 抬高 RPMB"的设备。**
- 合法剩余路径：绕社区绑定资格 + 官方 Mi Unlock 工具（168h）→ 解锁 → 刷 Magisk。
  （HyperOS 1 时代社区 bypass 工具伪造绑定成功，需自行评估合规与账号风险。）

### 提权漏洞现状（全实测，munch @ HyperOS 1.0.15 / 2026-03 补丁）

| 探测 | 结果 |
|---|---|
| `miui.mqsas.IMQSNative` root-dd 服务 | shell 域被 sepolicy 隔离，不可达 |
| ABL cmdline 注入 (`oem set-gpu-preemption ... selinux=permissive`) | unknown command |
| 公开内核漏洞 | 4.19 老内核补丁齐全，无现成 |

---

## 三、脚本清单

所有脚本为单会话设计：等待 9008 → 最小握手 → loader 健康检测 → 操作 → 回读校验 → reset。

| 脚本 | 用途 |
|---|---|
| `unsparse.py` | Android sparse → raw 转换（MiFlash 自动做、edl 工具不做的关键一步） |
| `fb_probe.py` | pyusb 实现 fastboot 协议（bulk 端点直写，绕过 fastboot.exe 驱动兼容问题） |
| `session_flash.py` | 单会话长驻刷写框架：23 段分块校验 + 坏段续写 + 清 userdata + 恢复 devinfo |
| `flash_os1.py` | 全量刷写模板（32 分区，官方语义：a 槽 + 不碰 GPT/devinfo/EFS，512MB 分块写入） |
| `restore_full_official.py` | 23 分区官方恢复（固件层 + 启动链，秒级可逆的"后悔药"模板） |
| `flash_gpt_slot.py` | EDL 改 GPT 分区属性位（slot active/unbootable，绕过锁定态 set_active 限制） |
| `flash_devinfo_unlock.py` | devinfo 解锁实验脚本（写 0x10/0x18，用于验证官方 abl 不采信） |
| `flash_full_eng.py` / `flash_eng_chain.py` / `flash_eng_abl.py` | 工厂包实验三连（记录 RPMB 边界的验证过程） |
| `post_download.py` | ROM 下载后自动流水线：MD5 校验 → 解压 → anti_version/rollback 核对 → 分区清单生成 |

### 使用前必读

1. **改脚本里的硬编码路径**（`F:/Temp/...`）为你的实际路径
2. **loader 必须与机型匹配**：`prog_ufs_firehose_sm8250_ddr.elf`（SM8250 通用，Xiaomi EDL
   Auth 自动通过）；其他机型从对应线刷包取
3. **刷写偏移必须与设备真实 GPT 核对**：先 EDL 读出各 LUN 的 GPT dump 逐项比对（脚本中的
   sector 偏移是 munch 实测值，其他机型不可直接使用）
4. **永远保留"恢复脚本 + 官方原件"再动手**：任何实验分区都可被官方原件秒级覆盖回去
5. 电量充足、电池接好、USB 线稳固——三个变量排除后 UFS 才稳定

---

## 四、EDL 环境与驱动

- 工具：[bkerler/edl](https://github.com/bkerler/edl)（本仓库脚本 `sys.path.insert` 引用它，
  需自行 clone 到 `F:/Temp/munch_tools/edl` 或修改路径）
- 进 9008：关机 → 主板测试点短接（munch 在后盖内）→ 插线；已解锁机型可用 `adb reboot edl`
- Windows 驱动注意：
  - fastboot 设备（VID 18D1/D00D）需 **Xiaomi WHQL 签名驱动**（MiFlash Pro 自带）才能被
    Mi Unlock 识别；Google 官方 INF 不含 D00D，社区改版 INF 无签名会被拒
  - Google USB Driver（`android_winusb.inf`）适合 ADB 接口（PID 4EE7）
  - EDL 9008 用 Qualcomm QDLoader 驱动

---

## 五、致谢与参考

- [bkerler/edl](https://github.com/bkerler/edl) — EDL 工具本体
- [Renate] 的 EDL Utility 与 XDA 社区的 sig.bin/loader 生态
- XDA Black Shark 4 (同平台) 解锁研究帖 — 信任链实测的交叉验证
- 小米工厂包流通生态（fiime / mifirm / romdevelopers 等）— 仅作研究引用，本仓库不含任何工厂 ROM

## 免责声明

- 本仓库仅含自研脚本与文档，**不含任何受版权保护的固件镜像、泄露的工程 ROM 或设备私密数据**
- 刷机有风险：操作不当可导致设备变砖。请完整阅读文档、备份官方原件后再动手
- BL 解锁相关操作请遵守设备所有权与当地法律法规；绕过账号锁（防盗锁）不属于本项目范围
