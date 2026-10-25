# 接入 Matt 规格驱动执行

准备使用 Matt 规格驱动执行链，或用户指定 Matt 时读本页。它是可选接入，依赖已存在的原版链。普通软件开发沿用项目流程，无需检查 Matt 依赖。

## 先找到真实入口

先查本会话可用技能与宿主登记，不足时再定向查项目、用户级技能目录和插件登记。核对真实路径与来源，并确认宿主及子代理能调用。名称相同或缓存中存在文件，都不足以确认可用。

记录入口全名或路径、来源、版本、调用规则和依赖。以下依据固定研究版 `49dd158d1076134a641b33efb035946536778336`；安装版本不同时，先读该版本的实际要求。区分查到文件、实际装载与运行的证据。

固定版 `implement-spec` 标记 `disable-model-invocation: true`，调用规则禁止其他技能通过 Skill tool 调用人工触发技能。助手检查条件、准备交接，由用户亲自在当前宿主显式触发原版。用户已在当前请求触发真实入口时，沿用该请求。使用核实后的完整入口名或路径；`/implement-spec <spec>` 仅是上游示例，当前宿主的命令形式需另行核对。

## 核对这一轮真正需要的条件

- **技能可用**。固定版需要 `implement-spec`、`tdd`、`code-review`，其中 `tdd` 的安装文档还要求 `codebase-design`。确认安装完整、子代理能调用所需技能，并核对实际安装版新增的依赖。接口或测试边界尚未确定时才需要调用 `codebase-design`，已有决定直接复用。技术配置由助手检查。
- **已有输入**。复用权威 spec、含阻塞关系的全部 ticket graph、tracker 配置，以及本轮授权和验收记录。固定版按 `docs/agents/issue-tracker.md` 解析 tracker，实际以安装版为准。按需要使用 `to-spec`、`to-tickets`、`setup`；接入本身不授权生成规格、票据、配置或安装依赖。
- **执行与验证**。检查本轮所需的后台子代理、每票 git worktree、集成分支与 merge、tracker 读写能力。Git 忽略的本地 fixtures、数据库和凭据不会随 worktree 带走，需已有合适且获授权的验证安排。保留凭据原位，并按实际运行记录说明验证结果。
- **自动动作与授权**。tracker 可能关闭票据；PR-closing flow 会在首个 merge 后开 draft PR，末尾标为 ready。逐项核对 merge、cleanup 和外部写入是否已获授权，普通实施许可或入口调用不能替代这项核对。缺少依赖时说明情况，安装和配置另需相应授权。

## 完整规格和控制边界都通过，才交接

原版执行 entire spec 和全部 ticket graph，最终审查也面向整份规格。传入的 spec 必须现成、完整、自足，并与本轮授权匹配。仅选部分 tickets 不能缩小原版执行范围；也不能为接入自行扩大授权或另造规格。

范围不匹配时，说明差异并停下 Matt 分支，继续独立且已授权的工作。用户要求必须用 Matt 时，将受影响部分的阻塞带回用户，不自行更换执行链。

固定研究版缺少完整的改意暂停、分轮决策与修后定向复核机制。交接前确认宿主能实际打断或取消相关 worker，并阻止未授权的后续 merge、tracker 和 PR 动作。无法维持这些控制时，停止接入；预计不会中途改意，也仍需具备终止能力。

链中的自动动作或全局修复若超出本轮范围，需有实际约束手段才可进入。交接说明无法代替执行控制。新的用户取舍由主助手统一带回；Matt 返回后，主助手审阅实际证据，完成范围内的必要修复与定向复核，报告剩余阻塞。

## 给用户一段可随入口提交的交接

引用已有材料即可。将以下占位内容替换为真实输入和已核实的控制安排。

```text
规格/全部票据：【完整 spec 与 ticket graph 指针】；tracker：【既有配置】。
本轮授权及验收：【指针】，覆盖完整规格；接口和验证安排：【已有材料】。
沿用【决策节奏、暂停与改意边界】；宿主可通过【实际方式】取消相关 worker，并阻止后续 merge/tracker/PR。
外部动作仅限【已有授权】；未授权动作已由【实际控制】阻止。阻塞时回主助手，不扩范围。
```

缺少依赖时说明缺少哪项、哪些任务正在等待，以及哪些已授权工作可以继续。按实际请求处理安装或替代方案。

## 固定来源

以下用于核对本页依据，实际运行要求以核实过的安装版为准。

- [implement-spec 技能](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/skills/engineering/implement-spec/SKILL.md)
- [implement-spec 执行说明](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/docs/engineering/implement-spec.md)
- [技能调用规则](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/.agents/invocation.md)
- [tdd 技能](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/skills/engineering/tdd/SKILL.md)
- [tdd 安装前提](https://github.com/mattpocock/skills/blob/49dd158d1076134a641b33efb035946536778336/docs/engineering/tdd.md#L210-L212)
