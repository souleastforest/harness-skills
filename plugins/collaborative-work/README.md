# collaborative-work 插件 0.5.0

这是 Codex 插件包，提供九个独立技能：主入口 start，以及 help、brainstorm、complexity、develop、story、team、maintain、matt。

从 `collaborative-work:start` 开始：没有具体任务时介绍用法，附有任务时直接协作。也可选择独立功能入口。团队编排由用户显式启用。

0.5.0 补齐可复跑的发布校验、严格原生发现、分层评估和许可说明。变更见 [CHANGELOG](CHANGELOG.md)，已测环境与验证边界见 [compatibility.json](compatibility.json)。

按插件方式安装并启用，再从技能选择器选择 `collaborative-work:help` 查看用法。主入口的完整标识是 `collaborative-work:start`；插件没有声明裸 `collaborative-work` 的自动别名。仅把插件目录复制进单技能目录，不能代替插件安装。

源码仓库提供 `tools/collaborative_work` 发布工具。维护者从同一套声明环境执行 validate、测试、build、verify，再做原生发现与行为验收；工具不随运行 ZIP 分发。ZIP、manifest 和 SHA256 文件应一起保存；校验值只能核对字节，不等于发布者签名。

安装前，使用发布工具核对 ZIP、manifest 和 checksum，并解压到新的固定目录。已有同名同版本缓存时，先比较完整文件清单；字节不同应停止，不能覆盖成同一个版本。

本包附带本地目录清单。使用支持插件命令的 Codex CLI 注册并安装，将示例路径换成实际核验并解压的位置：

```sh
codex plugin marketplace add /absolute/path/to/collaborative-work
codex plugin add collaborative-work@collaborative-work-local
```

安装后开启新的桌面会话查看入口。保留解压目录，以便后续从本地源更新。若系统中的 CLI 版本较旧，使用桌面版随附的同版本 CLI，避免不同版本对插件支持不一致。

Codex 原生发现和桌面界面的验证状态见随发布提供的验证说明。单独键入冒号不会打开技能选择器；不同界面的技能提及控件以实际客户端为准。

插件无 MCP 服务、hook、安装脚本或配置改写。运行说明及随附 Superpowers 许可证保存在 `skills/start/`，各功能入口按需读取共用规则和专项参考。

新增内容采用 [MIT](LICENSE)；第三方版权、许可及借鉴范围见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。0.3.0 的文字子命令仍可兼容理解，但只有插件版提供独立技能注册。
