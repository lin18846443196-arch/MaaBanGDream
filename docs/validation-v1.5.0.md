# RhythmPilot 1.5.0 验证状态

完整自动化验证：`scripts/verify.ps1` 为 **1914 passed / 12 skipped**。
固定便携 Python 3.12、MaaFw 5.10.2 检查通过；Native 扩展可导入，版本 0.1.0。
发布/启动清理/中文路径重启/Native 定向复核为 122 passed / 5 skipped。
挑战点数流程还通过真实 MaaFramework、无设备控制器的离线 Pipeline 回放：
倍数选择、199 点耗尽、OCR 不可信时拒绝点击均按预期处理。

上游完整 v1.4.5 ZIP 的 SHA256 已校验：
`b07ef8164b2944a82dcbf52dcaddad5df7f8591ce0bf8bf5b99aa4a217665bc6`。
复用其 Python 归档与未改动 Native 源码对应的扩展；桌面宿主和更新器从定制 MFA
源码及本仓库品牌补丁重新构建。二进制来源及摘要写入候选包 `BUILD-INFO.json`。

- 自动化：完整测试通过；正在完成候选包结构与独立启动检查。
- 构建：RhythmPilot 程序与独立更新器已构建；完整/更新 ZIP 正在生成验证。
- 开发部署：原用户安装未被覆盖。
- 真实设备/游戏：迁移后的版本尚未验收。
- 发布：用户已指定仓库与名称，GitHub 授权延后，尚未推送或创建 Release。

原始录像、设备信息、个人配置和历史诊断保留在本地且被 Git 忽略。
公开源码测试需要这些原始证据时明确跳过，裁剪后的功能模板随源码保留。
