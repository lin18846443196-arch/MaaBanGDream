# YesBanGDream 2.0.0 验证状态

完整自动化验证：固定运行时检查与完整测试为 **1922 passed / 12 skipped**，
`git diff --check` 通过。
固定便携 Python 3.12、MaaFw 5.10.2 检查通过；Native 扩展可导入，版本 0.1.0。
发布/启动清理/中文路径重启/Native 定向复核为 122 passed / 5 skipped；
新增长路径修复与发布启动定向复核 40 passed。
完整 MFA 源码导出新增 6 项回归通过，覆盖源码提交一致性、排除本机文件、脏工作树拒绝、
归档损坏与二进制构建信息不匹配。
挑战点数流程还通过真实 MaaFramework、无设备控制器的离线 Pipeline 回放：
倍数选择、199 点耗尽、OCR 不可信时拒绝点击均按预期处理。

上游完整 v1.4.5 ZIP 的 SHA256 已校验：
`b07ef8164b2944a82dcbf52dcaddad5df7f8591ce0bf8bf5b99aa4a217665bc6`。
复用其 Python 归档与未改动 Native 源码对应的扩展；桌面宿主和更新器从定制 MFA
源码及本仓库品牌补丁重新构建。二进制来源及摘要写入候选包 `BUILD-INFO.json`。

- 自动化：完整测试通过；在独立公开源码目录中执行完整测试为 **1705 passed /
  229 skipped**。该目录不含私密录像与 Native 构建产物，因此相关回放/扩展测试
  明确跳过。含本地证据和经验证扩展的源码验证为 1922 passed / 12 skipped。
  跳过项不是成功的设备验收。
- 构建：YesBanGDream 程序与独立更新器已构建；完整/更新 ZIP 结构和校验检查通过，
  最终候选包含长路径修复。构建提交与摘要见各包的 BUILD-INFO.json 和 SHA256 文件。
- 便携环境：Windows PowerShell 5.1 下的中文、空格、深目录首次准备通过，
  Python/MaaFw/Core/MFA/Binding 版本检查通过；全程使用独立候选目录。
- 开发部署：原用户安装未被覆盖。
- 真实设备/游戏：迁移后的版本尚未验收。
- 发布：2026-10-03 已向个人仓库推送源码，经 [PR #1](https://github.com/lin18846443196-arch/MaaBanGDream/pull/1) 合并到 main，并发布 [YesBanGDream v2.0.0](https://github.com/lin18846443196-arch/MaaBanGDream/releases/tag/v2.0.0)。附件为完整包、更新包及 MFA 源码包；GitHub Release 为公开发布，未标记预发布。
  包内记录的项目构建提交为 `f76c5db`，main 的合并提交为 `5247801`；发布状态不代表真实游戏验收通过。

原始录像、设备信息、个人配置和历史诊断保留在本地且被 Git 忽略。
公开源码测试需要这些原始证据时明确跳过，裁剪后的功能模板随源码保留。

首次准备发现并修复了上游 conda-unpack 在超过 MAX_PATH 的目录中访问失败的问题。
解压使用扩展 Windows 路径，并通过有目录边界的文件访问适配执行原修复脚本；
写入环境的 prefix 仍为普通路径，不改系统长路径策略、不改原用户安装。

GitHub 授权、源码留存与发布已完成；本次迁移后的真实游戏验收仍未完成，不能用自动化测试或公开发布状态代替。
