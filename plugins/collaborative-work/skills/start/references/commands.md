# 功能入口

插件版把主入口和八项功能分别放在独立技能目录中。安装并启用插件后，按宿主选择器中实际显示的名称选择功能。单独输入冒号不构成选择技能的操作。

## 介绍主入口

`collaborative-work:start` 是主入口。用户未附任务时，简短介绍用途，展示下表，并给两个可复制的例子，其中一个使用 story。带有任务时直接处理任务。宿主按需加载本技能时，也直接处理任务。

## 独立功能

| 技能标识 | 用途 |
|---|---|
| `collaborative-work:help` | 介绍用法；只问某项功能时只介绍该项。 |
| `collaborative-work:brainstorm` | 按[设计讨论](brainstorming-dialogue.md)澄清目标、比较路线和取舍。 |
| `collaborative-work:complexity` | 按[任务复杂度](task-complexity.md)交付流程建议、理由和需要的产物，实施依照另有的明确请求。 |
| `collaborative-work:develop` | 按[软件开发](software-development.md)处理开发任务；用户只要方案时交付方案。 |
| `collaborative-work:story` | 按[寓言学习](story-learning.md)先讲故事，结尾揭题，再解释概念、隐喻与局限；可指定领域、概念和篇幅。 |
| `collaborative-work:team` | 用户主动选择时，为当前任务启用[团队编排](team-orchestration.md)，核对角色、模型与停止条件。 |
| `collaborative-work:maintain` | 按[技能维护](skill-maintenance.md)创建、改进、验证或发布技能。 |
| `collaborative-work:matt` | 按[Matt 接入](matt-execution.md)核对原版条件并准备交接，实际调用沿用原版规则。 |

已选择功能时直接进入对应流程，无须再展示菜单让用户重选。story 未指定主题时自行选题；其他入口缺少任务对象时沿用当前明确目标，仍无法确定再问。

展示入口、解释用法、校对引用或材料中的命令都不构成启动动作。实际动作沿用用户请求与已有授权，切换功能不扩大范围。已有团队约定按原范围延续。

## 调用与兼容

优先在宿主的技能选择器中选择完整技能标识。在支持 `$` 提及技能的界面中，可以使用以下形式；桌面界面使用实际提供的技能提及控件，不能把 CLI 的触发符当作所有界面的保证。

```text
$collaborative-work:help
$collaborative-work:story 从经济学里选一个研究生概念，用寓言讲清楚。
```

旧版加载技能后输入 `:story` 等文字的方式继续作为对话兼容约定。它本身不会注册技能或触发宿主补全。未知的文字子命令应简短指出，并给出最接近的有效入口，等待用户选择。
