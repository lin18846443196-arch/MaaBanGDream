<p align="center">
  <img src="docs/assets/yesbangdream-logo.png" alt="YesBanGDream Logo" width="260">
</p>

<h1 align="center">YesBanGDream</h1>

<p align="center">
  <strong>BanG Dream! 自动化 · 实时演奏 · 挑战 / 协力 / 团队 / 组曲 · 本地谱面辅助</strong>
</p>

<p align="center">
  <a href="https://github.com/lin18846443196-arch/MaaBanGDream/releases/tag/v2.0.1"><img src="https://img.shields.io/badge/Version-v2.0.1-ff6f9f" alt="Version"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-0078D4" alt="Windows">
  <img src="https://img.shields.io/badge/MaaFramework-5.10.2-4c8bf5" alt="MaaFramework">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-6f42c1" alt="License"></a>
</p>

YesBanGDream 是基于 [MaaBanGDream](https://github.com/coatcn1/MaaBanGDream) 的非官方版本，由 **lin18846443196-arch** 独立维护，使用 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 和定制 MFAAvalonia 控制 Android 模拟器。

当前发布版本为 **2.0.1**，以本地定制的上游 **1.4.3** 为基线，已同步上游 **1.4.4 / 1.4.5** 的相关改进。项目名称、图标、更新源和版本号独立维护；仓库地址继续使用 `lin18846443196-arch/MaaBanGDream`。

2.0.1 更新双角色相框图标，修复协力准备页误判、直接启动时的 Profile 通信错误及 MuMu 前台额外切换；同时提供 Legacy / Native Expert 默认校准。最近一次 MuMu Native Expert 协力完成 5/5，具体结果与保留的问题见 [验证状态](docs/validation-v2.0.1.md)。

[下载 Releases](https://github.com/lin18846443196-arch/MaaBanGDream/releases) · [问题反馈](https://github.com/lin18846443196-arch/MaaBanGDream/issues) · [2.0.1 版本说明](docs/release-notes-v2.0.1.md)

## 功能总览

当前客户端提供以下 **10 项任务**：

| 任务 | 可选项与主要行为 |
| --- | --- |
| 🎶 自动演出 | 使用游戏内自动演出；当前曲目或每轮随机选曲，五档难度，连续执行指定次数；自动演出配额耗尽时结束。 |
| 🎹 单人实时演奏 | 机器人排练 / 正式演奏，当前曲目 / 每轮随机，五档难度、演奏次数、诊断记录；正式演奏需要匹配的已验收 Profile。 |
| 🤝 协力演出 | 普通匹配、好友邀请或六位私人房间号；普通房支持自由 / 初级 / 首席 / 传奇档位；五档难度、次数、不指定 / 随机 / 当前曲目、成员退出处理、断网跳车及诊断记录。 |
| 👥 团队演出 | 普通公开匹配，等待满员后自动开演，每局结算后回主页重新进入；Easy / Normal / Hard / Expert 四档难度、次数、单阶段等待 30–600 秒、每局异常重试 0–5 次及诊断记录。 |
| ⚡ 一键实时演奏 | 用户手动进入演出，程序被动识别开场封面与标题，按所选难度演奏一首，处理结算并恢复主页后结束；支持五档难度和诊断记录。 |
| 🎯 实时演奏校准 | 一次排练确定时序，再用正式演出验证并生成、启用 Profile；五档难度、当前 / 随机曲目、自动续跑 / 重新开始及诊断记录。 |
| 🎁 每日免费抽卡 | 查找“每日 3 次免费 演出招募”，抽取已解锁的免费单抽，最多三次；每日演出奖励尚未解锁或免费次数耗尽时正常结束。 |
| 🏆 挑战演出 | 自动进入当前活动挑战曲，识别所持挑战点数并选择 8 / 4 / 2 / 1 倍，接入实时演奏及结算；支持五档难度、次数、断网跳车和诊断记录。 |
| 🎼 组曲演奏 | 自由巡演 / 课题巡演，每组三首；自由巡演支持当前曲目 / 每首随机和统一难度，课题巡演沿用预设歌曲与难度；支持次数、符合条件的断点续跑及每首独立诊断记录。 |
| 📹 手动流程录像 | 录制模拟器内的手动操作；5 / 10 / 15 FPS，最长 5 / 15 / 30 分钟，可显示触摸位置；点击 MFA 停止按钮保存 MKV、帧时间映射和首末帧。 |

带“演出次数”的普通任务支持 **1–999 次**，填 **0** 表示无限运行，直到手动停止、资源耗尽或遇到无法安全继续的失败。组曲次数按歌曲计算，只接受 **3–999 且为 3 的倍数**，填 0 则循环完整三首。一键实时演奏固定执行一首；校准、抽卡和录像按各自流程结束。

自动演出使用游戏内功能；其余实时演奏任务由程序识别画面、规划触控，并结合可用的本地谱面完成演奏。

### 挑战点数与倍数

每轮都重新读取余额，优先选择能够支付的最大倍数。当前流程按 **1 倍消耗 200 点** 计算：

| 所持挑战点数 | 选择倍数 | 本局消耗 |
| --- | --- | --- |
| ≥ 1600 | 8 倍 | 1600 点 |
| 800–1599 | 4 倍 | 800 点 |
| 400–799 | 2 倍 | 400 点 |
| 200–399 | 1 倍 | 200 点 |
| < 200 | 正常结束任务 | 不再开演 |

点数识别不可信、选择后的余额变化或倍率状态未确认时停止开演。挑战演出不自动重试失败局，失败局不计入完成次数。

### 协力与团队

协力支持同房续演；成员退出时可选择“确认后报错结束”或“不报错并重新入房”。随机 / 当前曲目模式在第一轮留 **10 秒筛歌窗口**，期间可手动确认或停止；普通房综合能力不足时会明确结束。

协力和挑战的“生命归零断网跳车”默认关闭。实际断网需要模拟器具备 **root 及按游戏 UID 隔离网络** 的能力，只临时阻断游戏流量并恢复网络，保留 ADB 连接。协力开启后在入房前检查能力，检查失败则停止任务；挑战失败恢复和团队逃生在无法隔离网络时改为直接重启游戏。挑战恢复后仍结束失败局；团队按各自预算重试，入房与演奏重试分别受限，默认上限为 2 次。

### 组曲与一键实时

组曲使用正式实时演奏和现有 Profile，不提供排练模式。三首的 Profile 流速必须一致；每首在自己的准备页识别歌曲，必要时由开场最终封面补全。只有完整三首演完才增加三个完成数，第三首后按顺序尽力读取三张判定页；一组发生可重试失败后，按预算重演完整三首。

断点续跑需同时满足当前任务身份、组曲会话、歌曲和选项一致；旧任务或不匹配会话不能直接续接第 2 / 3 首。组曲中途不会进入设置页或自动选择“休息”。

一键实时演奏监听期间不主动选曲或导航，请先启动任务，再手动进入开演流程；程序不会从歌曲中途开始输入。开启流速检查时需有最近 **15 分钟** 内的流速读回；关闭时信任声明流速。演完一首后仍会执行结算和主页恢复。

## 下载与首次使用

前往 [YesBanGDream v2.0.1 Release](https://github.com/lin18846443196-arch/MaaBanGDream/releases/tag/v2.0.1)，按用途选择附件：

| 文件 | 用途 |
| --- | --- |
| `YesBanGDream-v2.0.1-win-x64.zip` | 首次安装的完整便携包，包含桌面客户端、更新器、Python / .NET 运行时、Agent、模型和本地谱面。 |
| `YesBanGDream-v2.0.1-win-x64-update.zip` | 已有兼容便携环境的更新包；不含 Python 运行库归档及谱面库。 |
| `YesBanGDream-v2.0.1-MFA-source.zip` | 对应定制桌面客户端和更新器的完整源码及品牌构建输入。 |
| `YesBanGDream-v2.0.1-Expert-profiles.zip` | Legacy / Native Expert 两份校准及导入说明，便于已有安装手动补齐。 |

1. 将完整包**完全解压到新目录**，不要在压缩软件内直接运行，也不要直接覆盖旧安装。
2. 双击 **`启动 YesBanGDream.cmd`**。首次启动会在当前目录的 `runtime` 下展开固定 Python 环境，并检查运行时版本。
3. 在客户端添加或选择 Android 模拟器，设置 **1280 × 720、DPI 240**；当前任务资源面向**哔哩哔哩服**。
4. 按下方建议配置游戏。环境匹配时可直接使用随包 Expert 默认 Profile；环境不匹配则先运行“实时演奏校准”，再在“设置 → 演出设置 → 实时演奏 Profile”确认。
5. 选择任务、难度和次数后开始运行；需要提前结束时使用客户端停止按钮。

普通用户无需安装 Python、Miniconda、.NET 或开发工具。程序入口为 `YesBanGDream.exe`，建议通过上述启动器完成便携环境准备、校验及旧诊断清理。便携环境支持中文、空格和深目录路径。

### 环境与游戏设置

| 项目 | 要求或建议 |
| --- | --- |
| 操作系统 | Windows 10 / 11 x64 |
| 游戏与控制器 | Android 模拟器、ADB 连接；当前界面资源为哔哩哔哩服 |
| 分辨率 / DPI | 1280 × 720 / 240 |
| 实时演奏环境 | 分辨率、DPI、游戏帧率、画质、流速和引擎须与 Profile 一致；变化后重新校准 |
| 模拟器 | 雷电 9 有较多既有验收记录；MuMu 继续使用 Legacy，Native 时钟偏斜尚未稳定解决 |
| 随包运行时 | Python 3.12、MaaFramework 5.10.2、定制 MFAAvalonia 2.12.0、桌面 .NET 10；精确组合见 [runtime-compatibility.json](runtime-compatibility.json) |

流速以外的游戏内设置请手动配置：

| 游戏内设置 | 推荐值 |
| --- | --- |
| 镜像、判定辅助 | 关闭 |
| 连击数量显示 | 开启；横向位置“右”、纵向位置“上” |
| FAST / SLOW 表示 | 开启 |
| NOTE TYPE | 1 |
| TAP EFFECT | 4 |

Expert / Special 建议使用 **5.0 或更高流速**，并按实际流速校准。正式演出准备页会先关闭 `3D演出 / 动画MV`，再检查并关闭 3D Cut-in。

## 演出设置

进入 **“设置 → 演出设置”** 可集中管理：

| 页面 | 功能与使用方式 |
| --- | --- |
| 实时演奏 Profile | 查看验收、引擎、环境、流速及校准记录；单击查看 / 编辑，双击设为当前任务难度的 Profile。可调整目标 FPS、时序偏移和超时；修改已验收 Profile 会撤销验收状态。 |
| 谱面辅助 | 本地谱面辅助、谱面主时钟、Native C++ 测试开关、Native 协力漏键抖动，以及 Bestdori 手动同步。 |
| 流速 | 为五档难度设置目标流速；“开演前自动设置并验证流速”开启时从主页读取、按需修正并复核，关闭时完全跳过设置页并信任目标值。 |
| 调试目录 | 查看记录目录、是否存在和最近更新时间，复制路径或直接打开。 |
| 任务安全 | “不检查结果”、单局技术失败重试次数、其他程序占用检测与清理选项。 |

高难度 Profile 可兼容较低难度，Expert 与 Special 属于同一兼容等级，环境和流速仍需匹配。单人、协力、挑战、自动演出及自由巡演请求 Special 时，只在该曲 Special 不可选的情况下显式回退 Expert；实时校准必须实际选中所选难度。实际 Special 的实时触控需要可信本地谱面及方向信息。

### 随包 Expert 校准

| 文件 | 引擎 | 匹配环境 | 时序偏移 |
| --- | --- | --- | --- |
| `expert-20260905233716.json` | Legacy | 1280×720、DPI 240、游戏 60 FPS、standard、流速 5.0 | 60 ms |
| `expert-20261003194036.json` | Native | 同上 | 60 ms |

新安装默认 Legacy，使用自动环境匹配；启用 Native 后可匹配 Native 文件。已有手动钉选会保留，切换引擎时请双击对应 Profile，或取消钉选后使用自动匹配。设置变化或实际表现不稳定时重新校准。

完整包内置两份文件，更新器保留用户 `profiles/`，通过独立 `default-profiles/` 在启动时只补缺失 JSON，不覆盖同名校准、选择或运行选项。直接打开程序后，Profile 管理和 Agent 也会补齐。手动导入独立 ZIP 时只复制需要的校准 JSON，保留自己的 `selection.json`。

### 谱面与引擎

本地曲库快照包含 **809 首歌曲、1777 张 Hard / Expert / Special 谱面和 867 个封面文件**；数量随手动同步更新。程序结合封面、实读标题、实际难度 / 等级和谱面摘要确认歌曲，处理共享封面及同名别名，避免使用错误谱面。

- **Legacy**：视觉实时识别与本地谱面辅助，支持 TAP、FLICK、HOLD / Slide、双押和 Special Left / Right 方向输入。Easy / Normal 等无本地谱面的难度可整局使用视觉 Legacy。
- **Native C++ 核心（测试）**：默认关闭，使用可信本地谱面和开场证据驱动触控；开演后发生故障会安全终止，不在歌曲中途切换 Legacy。仅建议在固定环境、已验证曲目中试用。
- **Native 协力漏键抖动**：可选功能，仅 Native 协力生效，每局漏掉 1–2 个普通单点并保留首音；关闭时按原谱面派发。
- **手动同步**：先停止演奏任务，再点击“谱面辅助 → 同步/更新全部谱面（Hard / Expert / Special）”。已校验文件增量复用，封面优先 CN，缺失时回退 JP / EN。

标题 OCR 和谱面读取在本地执行，演奏过程不会请求 Bestdori；游戏连接、客户端更新、公告及手动谱面同步仍需要各自的网络连接。数据格式见 [Bestdori 本地谱面仓库说明](docs/bestdori-chart-repository.md)。

### 结果、安全与恢复

普通实时演奏尽力采集一张判定页，组曲采集三张，保存 PERFECT / GREAT / GOOD / BAD / MISS（PGGBM）和结果记录。“不检查结果”只跳过判定数字读取；实时校准仍需完整成绩验收 Profile。

结算通过安全角落点击加快动画，再用 Android BACK 推进，每次输入后重新识别。已确认演出完成后的数字识别、截图或保存异常记录为警告，不撤销完成状态；生命归零、歌曲身份冲突和环境不匹配仍保留明确失败原因。手动停止会停止输入并释放触点。

单局技术失败重试次数支持 **0–99**，默认 **1**，用于普通单人、校准、协力和组曲；挑战不自动重试，团队使用自己的重试选项。重试前释放触点并恢复页面，组曲重演整组三首；身份、Profile 或谱面硬冲突不能靠重复尝试绕过。

启动演出任务时会检测其他程序占用；“跳过其他程序进程占用清理”开启后仍检测并警告，但不会终止其他进程。请确保同一模拟器只由一个自动化程序控制。

“设置 → 性能设置 → 任务运行时阻止息屏”开启后，在任务执行期间阻止显示器息屏和系统休眠；完成、停止、失败、关闭开关或退出程序时解除，软件空闲时遵循系统设置。

## 诊断记录与自动清理

实时演奏任务可选择：

| 记录级别 | 保存内容 |
| --- | --- |
| 轻量 Trace（推荐） | 关键事件、状态、诊断与结果证据，便于定位且占用较小。 |
| 完全关闭 | 关闭常规 Trace / 完整录像；演出结果及必要错误信息仍按任务流程保存。 |
| 完整录像 | MKV + Trace，记录更多开演、演奏与结算证据；组曲每首独立记录，文件较大。 |

可从“演出设置 → 调试目录”打开记录位置。故障反馈请说明版本、任务、请求及实际难度、模拟器、引擎、流速和发生阶段，并提供相关日志 / Trace / 结果；分享前去除个人设备和账号信息。

通过 **`启动 YesBanGDream.cmd`** 启动时，会清理 `logs`、`debug`、`screencap` 中**最后修改时间超过 24 小时**的自动日志、诊断录像、截图和结果信息：

- 记录目录内仍有近期更新时，保留整份记录，避免拆散证据。
- 保留手动流程录像、`debug/config`、用户配置、校准 Profile 和未完成校准引用的证据。
- 程序已运行时跳过清理；被占用、无法删除的文件及链接跳过，不影响启动。
- 仅准备环境的 `start-release.ps1 -NoLaunch` 不执行清理。重要自动诊断请在过期前另行备份。

## 更新与本机数据

客户端使用 GitHub 更新入口检查本仓库 Release。已有便携 Python 时优先使用更新包，运行库缺失时回退完整包；下载支持断点续传和 SHA-256 校验，独立更新器在主程序退出后替换程序文件并后台重启。

| 数据 | 位置与保留方式 |
| --- | --- |
| 客户端、模拟器及任务配置 | `config/`；常规更新保留。 |
| Profile、校准及组曲会话 | `profiles/`；常规更新保留。 |
| 自动日志、诊断、截图 | `logs/`、`debug/`、`screencap/`；更新保留，启动时按上述 24 小时策略清理。 |
| 本地谱面库 | `resource/charts/`；更新包不覆盖，通过谱面同步独立维护。 |
| 便携 Python | `runtime/`；随完整包提供并在首次启动准备。 |

“关于我们 → 更新日志”读取随包的 `resource/Release.md`；独立公告由本仓库主分支的 [docs/announcement.md](docs/announcement.md) 维护，内容变化时提醒，离线使用缓存或随包文件。更新器显示进度，更新后展示一次版本说明。

## 验证状态与已知限制

2.0.1 已正式发布为 Latest，提供完整包、更新包、定制 MFA 源码包及 Expert 默认校准 ZIP，各附 SHA256；八个附件的远端摘要与本地一致。本地完整自动化为 **1927 passed / 12 skipped**，纯公开源码为 **1708 passed / 231 skipped**。Windows PowerShell 5.1 下中文、空格路径首次及重复准备通过检查，补齐 Native 校准时已有 Profile 和选择保持不变。详细记录见 [验证状态](docs/validation-v2.0.1.md)。

**MuMu Native Expert 协力已完成五局验收。** 输入全部执行、触点释放正常，准备页误判没有复发；一次成员退出正常重入。前四首判定全部为 PERFECT，第五首数字未读出；第 1、5 首设备时序诊断超阈值，第 4 首记录 SLOW 182，录像有帧缺口。不承诺全连或零失误。挑战、团队、组曲和 Special 边界未新增完整实测。

Native 核心及迁入的 ADB 端口归属校验、Native 等待成本抖动过滤、协力成员加载页保护均为测试 / 试验功能，默认关闭。MuMu 请继续使用 Legacy。多客户端同时运行或频繁切换配置存在已知 Avalonia 崩溃风险；避免与 ALAS 等其他自动化程序同时控制同一模拟器。

详见 [验证状态](docs/validation-v2.0.1.md) 和 [上游迁移风险评估](docs/upstream-migration-v2.0.0.md)。

## 源码开发与构建

项目 Agent / Pipeline 与桌面宿主分属两个源码目录；桌面部分需要本版本对应的定制 MFAAvalonia。优先使用 Release 附带的 `YesBanGDream-v2.0.1-MFA-source.zip` 获取对应桌面源码和重建说明。

```text
workspace/
├─ MaaBanGDream/   # 本仓库：Agent、Pipeline、模板、发布脚本
└─ MFAAvalonia/    # 独立定制桌面源码
```

上游桌面基线为 [coatcn1/MFAAvalonia · fix/speed-only-settings](https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings)，品牌准备脚本要求隔离 checkout 位于提交 `a39dcd87ba2e5098ee23072e9a015c5c36f8c8d1`。使用 [prepare-yesbangdream-mfa.ps1](scripts/prepare-yesbangdream-mfa.ps1) 应用本仓库补丁，审查并独立提交；不要用同版本官方 Core DLL 覆盖，否则会丢失演出设置、Profile 管理和更新源保护。

源码开发使用固定 Python 3.12 Conda 环境 `maabangdream`，按环境准备脚本安装依赖后验证：

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
```

已有隔离的开发 MFA 运行目录后，通过部署脚本同步源码并启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1
```

Windows 发布由 [build-windows-release.ps1](scripts/build-windows-release.ps1) 构建，版本参数使用 `2.0.1`；需要匹配的定制 MFA 源码、.NET SDK、Python 及 Native 构建工具。完整步骤和验证要求见 [发布流程](docs/release-yesbangdream.md) 与 [便携包说明](docs/release-package.md)。两个源码提交、运行库来源和品牌摘要记录在包内 `BUILD-INFO.json`。

欢迎提交 [Issues](https://github.com/lin18846443196-arch/MaaBanGDream/issues) 和 Pull Request。功能使用 `feature/*`、修复使用 `fix/*`，提交前运行验证并审查差异，通过 PR squash 合并；不要提交个人配置、设备信息、日志、录像或构建产物。详细约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档与许可证

| 文档 | 内容 |
| --- | --- |
| [CHANGELOG.md](CHANGELOG.md) | 版本改动与项目进度 |
| [2.0.1 版本说明](docs/release-notes-v2.0.1.md) | 当前版本的主要变化 |
| [验证状态](docs/validation-v2.0.1.md) / [迁移评估](docs/upstream-migration-v2.0.0.md) | 自动化、设备验收及迁移风险 |
| [Windows 便携包说明](docs/release-package.md) / [发布流程](docs/release-yesbangdream.md) | 安装、更新、构建及源码交付 |
| [Bestdori 谱面说明](docs/bestdori-chart-repository.md) | 数据格式、同步与身份匹配 |
| [LICENSING.md](LICENSING.md) / [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) | 非商业许可范围和第三方权利 |
| [TRADEMARKS.md](TRADEMARKS.md) | 上游名称和 Logo 的使用规则 |

YesBanGDream 保留上游来源说明及许可证。从上游 `v1.4.0` 起，自有部分使用 [PolyForm Noncommercial License 1.0.0](LICENSE)，允许非商业目的的查看、运行、研究、修改和分发，不授权收费软件、收费分发、**收费部署或维护**、商业服务及商业产品集成；属于源码可用（source-available）项目。

上游 `v1.3.9` 及更早版本继续适用各自随附的 GPL-3.0-only 许可证。定制 MFAAvalonia 使用 GPL-3.0，MaaFramework 使用 LGPL-3.0；其他第三方组件、游戏素材、谱面、模型和用户提供的图标素材适用各自权利条款。使用时请遵守游戏服务条款，品牌与上游来源说明不代表官方认可。
