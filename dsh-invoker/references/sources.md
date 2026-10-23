# 来源与核对范围

事实依据与写作方法分开列出。官方文档是参考数据，不替代当前用户授权；源码核对也
不是安装、真实模型请求或原生三端运行的证明。

## 固定版本

- 本 skill 的工具链固定 **Python 3.12**、`deepseek-harness-sdk==0.1.5rc1` 与
  `deepseek-harness-runtime-bin==0.1.5rc1`，不从浮动分支安装。
- 缺少 uv 时使用校验过的官方 **uv 0.12.24 standalone**；已有可用 uv 直接复用，
  不自更新。调研时验证过现有 `uv 0.9.20` 所需参数可用，不代表所有更旧版本都可用。
- DSH 的 home/profile、SDK、审批与隐私语义核对到源码 commit
  `d743267388641bc76f17c45ce8b4c231aed1d32c`。下面的源码链接固定到该版本，当前
  在线文档可能继续变化。发行 wheel 与本地测试仍应按固定包版本分别核对。

发行信息：

- [SDK 0.1.5rc1（PyPI）](https://pypi.org/project/deepseek-harness-sdk/0.1.5rc1/)
- [runtime 0.1.5rc1（PyPI）](https://pypi.org/project/deepseek-harness-runtime-bin/0.1.5rc1/)
- [runtime 固定发行 wheel 元数据](https://pypi.org/pypi/deepseek-harness-runtime-bin/0.1.5rc1/json)：
  以实际 wheel 标签核对 macOS 14+、Linux glibc 2.28+ 与 Windows x64 边界，不按 uv
  自身平台表推断 DSH 支持。
- [uv 0.12.24 发布与 standalone 资产](https://github.com/astral-sh/uv/releases/tag/0.12.24)
- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [uv 环境变量参考](https://docs.astral.sh/uv/reference/environment/)
- [uv CLI 参考](https://docs.astral.sh/uv/reference/cli/)

## 需要核对某个行为时

| 本 skill 中的事实 | 官方依据 |
| --- | --- |
| SDK 包、上下文管理器、`run()`、结果与路径 patch | [SDK README](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/python/sdk/README.md)、[API](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/python/sdk/src/deepseek_harness/api.py) |
| SDK 显式 home、patch 文件参数与请求时限 | [client.py](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/python/sdk/src/deepseek_harness/client.py) |
| home 默认与环境覆盖 | [home-paths](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/util/home-paths/src/index.ts) |
| profile 名称、首次初始化与 dump 副作用 | [CLI reference](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/apps/cli/reference/README.md)、[profile.ts](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/boot/app-boot/src/profile.ts) |
| wheel 平台与无需系统 Node 的 runtime | [runtime README](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/python/sdk-runtime/README.md)、[Python development](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/python/development.md) |
| 凭据来源与优先级，不是认证证明 | [credentials-local](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/credentials/credentials-local/README.md) |
| 内部 approval answerer，与宿主问答不同 | [user-approval](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/interaction/user-approval/README.md) |
| `sdk-minimal` 的 `danger-full-access` | [sdk-minimal README](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/bundle/sdk-minimal/README.md) |
| 日志贡献准确 row ID 与独立本地持久化 | [base composition](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/bundle/base/cordis.patch.yml) |
| 关闭/重新启用贡献、历史后缀与独立 OTel 设置 | [session-log README](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/session/session-log-deepseek/README.md)、[贡献 guard](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/session/session-log-deepseek/src/index.ts#L184-L193) |
| patch 顺序与按 ID 覆盖 | [user-patches tests](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/boot/app-boot/tests/user-patches.spec.ts) |

## 交互与人类文档的方法来源

- **[Matt Pocock / grill-me（用户选择的入口）](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md)**：
  阅读时该方法的当前路径为
  [grilling（固定 commit 49dd158d1076134a641b33efb035946536778336）](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/skills/productivity/grilling/SKILL.md)。
  借鉴依赖关系调查、能自行检查的事实先查、根据回答重算路径；这里刻意改为每轮一个
  当前卡点并附推荐，不声称上游固定只问一个问题。DSH 安装/绑定/运行授权为原创
  编写，未复制全文或大段文字；浮动入口不作为固定技术规范。
- **[本仓库 reader-first-technical-docs](../../reader-first-technical-docs/SKILL.md)** 及
  [Cordis 写作参考](../../reader-first-technical-docs/references/cordis-patterns.md)：
  人类 README 以读者当前任务为入口，组织行动、可观察结果与恢复路径；智能体指令
  则保持简短分支。它们是只读写作参考，没有被修改，也不是 DSH API 的来源。
- **[官方 Python SDK 入门](https://deepseek-harness.github.io/deepseek-harness/guide/python-sdk)**：
  用于找到官方概念入口；教程中的 `sdk-minimal` 示例不覆盖本 skill 的保守 `sdk` 默认。

## 验证结论怎样表述

文档不将源码研究写成“已经跑通”。离线测试、真实 runtime + mock、原生三端 CI、
真实 provider 调用分别报告；fixture 访谈只测试引导行为，不安装软件、不读取本机
凭据、不发送模型请求。命令示例是当前入口合约，不是嵌入的实测输出。
