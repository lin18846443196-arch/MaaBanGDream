# RhythmPilot 发布流程

发布仓库：<https://github.com/lin18846443196-arch/MaaBanGDream>。
版本从 1.5.0 开始；上游版本保留其原含义，本地快照标签用于比较定制基线。

## 源码与桌面程序

Maa 项目和定制 MFA 使用独立 Git 仓库。Maa 仓库留存自己的源码、品牌构建配置
及 `patches/rhythmpilot-mfa-branding.patch`，不把 MFA 整套源码混入本仓库。
准备桌面源码时，在隔离的 MFA checkout 中切到
`a39dcd87ba2e5098ee23072e9a015c5c36f8c8d1`，运行
`scripts/prepare-rhythmpilot-mfa.ps1 -SourceRoot <MFA checkout>`，审查并独立提交补丁。

使用 .NET SDK 10、Python 3.12、MaaFw 5.10.2 和定制 MFA 2.12.0。
`packaging/RhythmPilot.targets` 只设置桌面宿主及更新器品牌，核心程序集名称保留兼容。
补丁同时设置桌面程序集身份、窗口图标、界面名称和更新重启路径。

## 构建与验证

1. 完成此分支的代码审查，将变更通过 squash PR 合并到自己的 `main`。
2. 在干净的源码和 MFA checkout 中运行 `scripts/verify.ps1`；如使用准备好的便携
   Python，可显式传 `-Python <python.exe> -Portable -MfaRoot <开发运行目录>`。
3. 运行 `scripts/build-windows-release.ps1 -Version 1.5.0 -MfaSourceRoot <MFA checkout>
   -BuildPython <固定 Python>`。默认从源码构建 Native 并打包中性 Conda 运行库。
4. 首次迁移可复用经上游完整包 SHA256 校验后提取的 Python 归档和 Native 二进制，
   分别传入 `-RuntimeArchive/-RuntimeArchiveSha256` 和
   `-NativeExtension/-NativeExtensionSha256`。Native 源码与 v1.4.5 有差异时禁止复用。
   `BUILD-INFO.json` 明确记录复用来源与摘要，桌面宿主及更新器仍重新构建。
5. 验证完整包、平铺更新包、中文/空格路径首次准备与后台重启；实测挑战点数阈值、
   难度、次数、断网跳车和诊断保存，以及受迁移影响的协力/失败退出。

产物是版本目录外壳的完整 ZIP、平铺的 runtime-free 更新 ZIP，分别附 `.sha256`。
更新包不含 Python 运行库归档和谱面；不携带个人配置、账号、截图或诊断日志。

## GitHub 留存与发布

先登录自己的 GitHub 账号，再向 origin 推送功能分支及 `personal-v1.4.3-snapshot`，
审查差异并完成 PR。不要向 upstream 推送，也不重写已有远端历史。
干净 main 构建完成后创建并推送 `v1.5.0` 标签，确保标签指向包中记录的源码提交。

运行 `scripts/publish-github-release.ps1 -PackageRoot <版本目录> -Python <固定 Python>`
会校验包与 SHA256，并创建 **草稿预发布**。脚本要求该源码提交和标签已经在自己的仓库。
正式发布使用 `-Publish -DeviceAcceptance <验收 JSON>`；验收 JSON 至少包含
`version`、`commit`、`passed: true`，并附实际模式、设备和结论的本地证据引用。
私密证据不能上传到公共仓库。

从功能分支构建的本地候选不等于正式 Release，不能直接沿用为 main 的稳定版产物。
更新 `docs/validation-v1.5.0.md` 时分别记录自动化、构建、部署、真机与 GitHub 状态。
