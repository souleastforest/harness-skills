# Cordis 教程的写作参考

本 skill 提炼阅读组织方式，保留具体项目自己的实现与事实。来源是 DSH 的
[Cordis 教程](https://deepseek-harness.github.io/deepseek-harness/develop/cordis-tutorial/)。

2026-10-08 完整克隆作者仓库，未安装或运行其软件。阅读版本：
`deepseek-ai/deepseek-harness@5badb15009ae1756c3afe0ae0cef1faafc290ccc`。
阅读范围包含中文索引、01–07全部章节、概念入门、Context API，以及插件/工具开发入口。

## 从结构中学到什么

索引先解释 Cordis 是什么、教程帮助谁以及最终结果，再把概念速读、逐步实践和 API
查询分流。第一章给出最小插件和预期输出，随后解释启动器、加载器与插件怎样协作。
第三章用同一个能力的提供与消费解释依赖，受控地改变配置帮助读者观察行为差异。
第六章从“没有输出”的症状进入诊断，再把状态名称连接到可验证的原因与修复。

这四种写法分别适合入口、教程、概念建立和排障。API 页的密集签名适合查询，
不必复制到教程正文；复杂配置的全部分支也应在读者真正需要时再展开。

## 一个实验指导的改写例子

原句把 baseline、candidate、worker/eval、轨迹与晋升写进同一条箭头。
可改为下面三段，由对象定义自然接到过程：

“我们先保留一份旧的执行环境，作为比较起点。这份环境包含当时交给出图智能体的
指令、技能、工具和配置。在实验记录中，它叫 baseline。”

“接着回看已经发生的成功和失败，挑出有证据的改动，组成准备验证的新环境。
这份新环境叫 candidate。它是否更好，要通过后续试跑和比较判断。”

“新环境准备好后，先检查出图智能体实际读到了这些内容，再让它完成一次任务。
独立审查员检查本次图纸；实验者保存结果，决定保留、修订还是撤销这组改动。”

概念页可进一步解释一场试跑及其多次调用。教程则从具体材料和当前检查点开始，
每步给出动作与可观察结果。论文来源放在背景页，字段和停止规则放在参考页。

## 固定来源

- [教程索引](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/index.zh.md)
- [第一个插件](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/01-first-plugin.zh.md)
- [服务](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/03-services.zh.md)
- [组合与热重载](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/06-composition-and-hmr.zh.md)
- [概念入门](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-primer.zh.md)
