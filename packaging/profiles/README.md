# Expert 默认校准

两份已验收文件：

| 文件 | 引擎 | 环境 | 时序偏移 |
| --- | --- | --- | --- |
| expert-20260905233716.json | Legacy | 1280×720 / DPI 240 / 60 FPS / standard / 流速 5.0 | 60 ms |
| expert-20261003194036.json | Native | 同上 | 60 ms |

完整包内置，更新时只导入缺失文件。独立 ZIP 中只需把需要的 Profile JSON 放入安装目录 `profiles/`，已有安装请保留自己的 `selection.json`。通过“设置 → 演出设置”选择引擎和对应 Profile；明确钉选的文件不会自动跨引擎替换。新安装默认 Legacy、自动按环境选择。

Native 校准文件已去除本机报告、录像及 jlog 路径和逐条执行时序样本，保留校准设置、实际验收成绩与时序汇总指标。不同设备性能和环境可能需要重新校准；MuMu Native 的长期时钟偏斜仍未解决，通用使用推荐 Legacy。
