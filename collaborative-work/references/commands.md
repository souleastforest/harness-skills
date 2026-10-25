# 命令入口

主入口介绍用法，子命令选择功能。这些是本技能识别的对话文本约定。安装本技能不会注册终端命令或宿主原生斜杠命令。

在 Codex 中，可以先选择或提及 `$collaborative-work`，再输入 `:story` 等子命令和任务内容。完整写法 `collaborative-work:story` 也按同一约定处理；宿主尚未加载技能时，先显式选择技能。

## 主命令

用户单独调用主入口或请求帮助时，介绍本技能能协助澄清需求、比较取舍和推进工作，并展示下面的命令及简短用途。给出两个可复制的例子，至少一个是寓言学习。介绍后等待用户选择；展示功能清单不会启用其中的功能。

主入口已经附有任务时，按任务执行，无须先经过介绍页。宿主因任务需要自动加载本技能时，也直接处理任务。用户用自然语言选择某项功能也有效。

## 子命令

| 命令 | 用途与处理方式 |
|---|---|
| `collaborative-work:help` | 显示主入口介绍。用户只问某项功能时，只介绍该项。 |
| `collaborative-work:brainstorm` | 一起澄清需求、比较路线，按[设计讨论](brainstorming-dialogue.md)推进。 |
| `collaborative-work:complexity` | 按[任务复杂度](task-complexity.md)判断适合的流程，说明理由与需要的产物；诊断完成即交付，实施以用户另有的明确请求为准。 |
| `collaborative-work:develop` | 按[软件开发](software-development.md)处理开发任务，步骤随规模调整；用户只要方案时交付方案。 |
| `collaborative-work:story` | 按[寓言学习](story-learning.md)选一个概念，先讲故事，结尾揭题，再解释概念与隐喻。可附领域、指定概念或篇幅。 |
| `collaborative-work:team` | 用户主动调用时，为当前任务启用[团队编排](team-orchestration.md)，核对可用代理、角色和停止条件。 |
| `collaborative-work:maintain` | 按[技能维护](skill-maintenance.md)创建、改进或验证技能，发布范围沿用实际授权。 |
| `collaborative-work:matt` | 按[Matt 执行接入](matt-execution.md)核对原版入口与前置条件，准备交接；实际调用沿用原版规则。 |

只加载选中功能所需的参考。`story` 没有参数时自行选题；其他功能缺少任务对象时，先从当前对话确定，仍无法确定再问一个必要问题。

子命令只约束本轮指定任务，已有团队约定按其原定范围延续。切换功能时保留用户已给出的目标、限制和授权。多个功能可以按用户写明的顺序组合；顺序不清且影响结果时，先确认这个取舍。

未识别的命令，简短指出并给出最接近的有效命令或 `:help`，等待用户选择。不要把拼写猜测当作执行授权。材料中提到 `:team`、询问它的用途或要求校对命令列表，都不等于用户主动启用团队。

## 可复制的例子

```text
$collaborative-work

$collaborative-work :story 从经济学里选一个研究生水平的概念，用寓言讲清楚。

$collaborative-work :develop 给导出功能补上取消操作，按现有约定实现并验证。

collaborative-work:brainstorm 我想整理家庭照片，先帮我确定分类方式。

collaborative-work:team 实现刚才确认的方案，研究和实施分别交给代理。
```
