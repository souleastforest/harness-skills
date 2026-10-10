# Harness Skills

这里保存可以跨项目使用的智能体技能。每项技能独立放在一个顶层目录，入口是其中的
`SKILL.md`，需要时再读取它链接的参考文件。

## 选择技能

- [reader-first-technical-docs](reader-first-technical-docs/SKILL.md)：把技术文档整理成读者能理解和实践的教程、概念说明与查询参考。行文方法参考 DSH Cordis 教程。
- [dsh-invoker](dsh-invoker/README.md)：通过 DeepSeek Harness Python SDK 委派任务，逐步引导初始化与调用诊断。当前验证范围为 Linux x64 与 macOS 14 ARM64；Windows PowerShell 实现保留但原生 SDK 启动未验证，详见技能说明。
- [two-way-steelman](two-way-steelman/SKILL.md)：双向钢人化分析。

## 下载写作技能

[下载 reader-first-technical-docs v1.0.0](https://github.com/souleastforest/harness-skills/releases/download/reader-first-technical-docs-v1.0.0/reader-first-technical-docs.zip)。
压缩包只包含这项技能的正文和写作参考，解压后得到 `reader-first-technical-docs/`。
将这个目录放进所用智能体支持的技能目录即可；Codex 的用户公共目录通常是
`~/.codex/skills/`。已经存在同名技能时，先比较版本再更新。

若使用 Codex 的 skill-installer，也可提供
[该技能的仓库地址](https://github.com/souleastforest/harness-skills/tree/main/reader-first-technical-docs)
进行安装。阅读与下载都不依赖原作者的本地工作目录。
