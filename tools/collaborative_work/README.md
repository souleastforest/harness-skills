# collaborative-work 发布工具

本工具用于源码维护和发布检查，不随插件运行包分发。支持 Python 3.10 及以上；
`PyYAML==6.0.3` 和 `markdown-it-py==4.2.0` 声明在 `pyproject.toml`，下载文件散列固定在 `uv.lock`。
构建后端也固定版本。使用独立环境，不依赖系统全局安装的 YAML 包。

从仓库根目录准备环境并运行测试：

```sh
uv sync --project tools/collaborative_work --locked
uv run --project tools/collaborative_work --locked python -B -m unittest discover -s tools/collaborative_work/tests -v
```

以下命令也从仓库根目录运行：

```sh
uv run --project tools/collaborative_work --locked python -B -m cwrelease validate plugins/collaborative-work
uv run --project tools/collaborative_work --locked python -B -m cwrelease build plugins/collaborative-work --output dist
```

`validate` 只读源文件，返回插件名称、清单版本、本地目录名称、九项技能短名及全部文件的 SHA-256。
它检查固定的运行目录、技能身份和一致版本、界面提示中的完整命名空间、team 的显式调用策略、
包内链接和普通文件类型。重复 JSON/YAML 键、缺失或多余技能、未知文件或目录、符号链接、特殊文件、
逃逸或不存在的本地链接都会失败。外部 HTTP/HTTPS/mailto 链接仅作为来源引用，不进行网络访问。
链接由 markdown-it-py 的 CommonMark 解析器读取实际 link_open 的 href 和 image 的 src；
内联链接、嵌套标签和已使用的引用式链接遵循同一检查。行内代码、围栏代码、缩进代码中的示例
不会当作链接，也不扫描图片 alt 文本中的伪链接。解析期间不渲染或获取任何目标。
解析器依据见[官方使用文档](https://markdown-it-py.readthedocs.io/en/latest/using.html)和
[Token API](https://markdown-it-py.readthedocs.io/en/latest/api/markdown_it.token.html)；
所固定版本的发行信息见 [PyPI 4.2.0](https://pypi.org/project/markdown-it-py/4.2.0/)。

运行文件集合包括两个插件清单、README、九项 SKILL.md 和 agents/openai.yaml、start 的现有九篇
参考及原样保留的 LICENSE.superpowers。根目录还允许 CHANGELOG.md、compatibility.json、
LICENSE / LICENSE.md / LICENSE.txt、LICENSE.matt 和第三方声明
THIRD_PARTY_NOTICES.md / THIRD-PARTY-NOTICES.md / THIRD_PARTY.md。
本工具不替权利人选择第一方许可证，也不以文件存在证明其许可结论。
新增运行文件前应明确修改布局规则及相关验证。

`build` 从插件 manifest 读取版本，不把发布版本写死在工具里。它生成三个同名前缀文件：

- `<name>-plugin-v<version>.zip`
- `<name>-plugin-v<version>.manifest.json`
- `<name>-plugin-v<version>.sha256`

ZIP 只有一个插件根目录，文件按名称排序；时间戳固定为 1980-01-01，权限固定为普通文件 0644，
使用 ZIP_STORED，避免压缩库版本影响字节。原始文件字节不改写。外部 manifest 记录全部文件散列，
checksum 同时绑定 ZIP 与 manifest。构建前后重新核对源文件；输出必须位于源树外。
已有同名同版本产物只有逐字节一致时可复用，任一产物字节不同都会拒绝覆盖。

使用 build 返回的实际路径验证；解压目标必须是尚不存在的新目录：

```sh
uv run --project tools/collaborative_work --locked python -B -m cwrelease verify /path/to/release.zip --manifest /path/to/release.manifest.json --checksum /path/to/release.sha256 --extract-to /path/to/new-directory
```

`verify` 核对两个外部散列和每个 ZIP 成员，拒绝非法相对路径、重复条目、符号链接、额外文件和
错误布局；在临时目录重新执行源码校验，通过后才复制到可选解压目标。清单中的路径同样不被信任。
校验和提供字节完整性检查，不证明发布者身份，也不代替签名。
三个 CLI 子命令成功退出 0，检查失败退出非零，错误写入 stderr。

需要在自动化中避免手写版本文件名，可以直接使用 API：

```python
from cwrelease.package import build, verify

artifacts = build("plugins/collaborative-work", "dist")
verified = verify(artifacts["archive"], artifacts["manifest"], artifacts["checksum"])
```

公共读取接口为 `ReleaseError`、`snapshot_tree(root)` 和 `validate_source(root)`。
`snapshot_tree` 返回 POSIX 相对路径到 SHA-256 的字典；`validate_source` 返回
`{name, version, marketplace, skills, files}`，其中 `skills` 为排序后的九项短名列表。

安装前先检查精确版本缓存；不存在时可以继续，已有相同字节可复用，已有不同字节会拒绝，必须发布新版本：

```sh
uv run --project tools/collaborative_work --locked python -B -m cwrelease native preflight --source plugins/collaborative-work --cache-root /absolute/path/to/marketplace/collaborative-work/version
```

报告输出必须在受检源和缓存之外，并使用尚不存在的新文件名；危险路径或已有记录都会在探测前被拒绝，失败证据不会被下一次运行覆盖。

原生发现验证另由 `cwrelease.native` 提供，不启动模型 turn：

```sh
uv run --project tools/collaborative_work --locked python -B -m cwrelease native read --source plugins/collaborative-work --codex-bin /absolute/path/to/codex --output /path/to/native-read.json
uv run --project tools/collaborative_work --locked python -B -m cwrelease native installed --source plugins/collaborative-work --codex-bin /absolute/path/to/codex --cache-root /absolute/path/to/marketplace/collaborative-work/version --output /path/to/native-installed.json
```

结构检查、原生发现、模型行为、桌面 UI 与公开发布分别保留结果。测试中的受控 app-server
进程验证协议和失败行为；它们不证明某个实际 Codex 版本、桌面界面或其他宿主已通过验收。
