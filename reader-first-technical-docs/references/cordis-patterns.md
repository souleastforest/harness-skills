# Cordis 教程的写作参考

需要结构示例、改写对照或来源依据时阅读。本参考提炼 DSH
[Cordis 教程](https://deepseek-harness.github.io/deepseek-harness/develop/cordis-tutorial/)
的阅读组织方式；具体语言、实现、命令与权限取自当前项目。

2026-10-08 完整克隆作者仓库，未安装或运行其软件。阅读版本：
`deepseek-ai/deepseek-harness@5badb15009ae1756c3afe0ae0cef1faafc290ccc`。
阅读范围包含中文索引、01–07全部章节、概念入门、Context API，以及插件/工具开发入口。

## 从结构中学到什么

索引先解释 Cordis 是什么、教程帮助谁以及最终结果，再把概念速读、逐步实践和 API
查询分流。第一章给出最小插件和预期输出，随后解释启动器、加载器与插件怎样协作。
第三章用同一个能力的提供与消费解释依赖，受控地改变配置帮助读者观察行为差异。
第六章从“没有输出”的症状进入诊断，再把状态名称连接到可验证的原因与修复。

按读者目的选择这些写法：多页入口需要分流，动手教程需要动作和观察，概念说明需要
对象关系，排障需要从症状找到检查入口。API 页的密集签名适合查询；教程只引入当前
动作所需部分，复杂配置的其他分支留到需要时展开。

## 一个实验指导的改写例子

假设原稿把 baseline、candidate、worker/eval、轨迹与晋升写进同一条箭头，读者又不熟悉
这些名称。整体整理时，可按下面的示意先解释对象，再接到过程：

“我们先保留一份旧的执行环境，作为比较起点。这份环境包含当时交给出图智能体的
指令、技能、工具和配置。在实验记录中，它叫 baseline。”

“接着回看已经发生的成功和失败，挑出有证据的改动，组成准备验证的新环境。
这份新环境叫 candidate。它是否更好，要通过后续试跑和比较判断。”

“新环境准备好后，先检查出图智能体实际读到了这些内容，再让它完成一次任务。
独立审查员检查本次图纸；实验者保存结果，决定保留、修订还是撤销这组改动。”

这三段适合向新读者介绍实验关系。专家字段参考可保留英文名称并直接给定义与约束；
只修改一句话时，补足那一句缺失的关系即可。它们不是可执行教程：实践页还需用当前
项目的实际材料和入口补齐动作、观察与失败检查。

长文中，方法来源可放在背景页，完整字段和停止规则可放在参考页；读者执行当前步骤
必须知道的限制仍写在该步骤旁。

## 固定来源

- [教程索引](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/index.zh.md)
- [第一个插件](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/01-first-plugin.zh.md)
- [服务](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/03-services.zh.md)
- [组合与热重载](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-tutorial/06-composition-and-hmr.zh.md)
- [概念入门](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-primer.zh.md)
