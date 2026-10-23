# 睡眠记录合同 V1

这是 V1 睡眠数据的唯一字段与更新规范。格式使用 Markdown，每月一个文件，
每个醒来日一个小节，小节内一个 JSON code block。无需先编写自动化脚本。

## 文件与身份

所有路径相对已确定的 `self_root`：

| 路径 | 用途 |
| --- | --- |
| `health/sleep/YYYY-MM.md` | 唯一睡眠事实源；按醒来日选择月份 |
| `health/_raw/sleep/YYYY-MM-DD/` | 该醒来日的原图副本 |
| `02-life.md` | 到睡眠月档的链接或索引 |

`sleep_date` 是 V1 唯一主键，格式为 `YYYY-MM-DD`，表示主睡眠结束所在的本地日期。
月档使用精确标题 `## YYYY-MM-DD`，同一日期只能有一个标题和一个记录块。跨午夜
睡眠归入醒来日；开始时间更正不改变身份。V1 一天记一晚主睡眠，不建立 session 表、
版本链或数据库。图中含多段睡眠而主睡眠不明确时，先确定主睡眠再入档。

多张图可以共同证明同一条记录。重复提交按日期更新原有小节，不能以开始时间或
图片数量创建新记录。更正归属日期时，将原小节和证据引用迁到正确月份/日期，移除
旧主键下的小节，并核对目标日期是否已存在；保持一晚一条，保留原图。

## 字段

JSON 顶层包含以下全部核心字段；未知用 `null`，真实显示为零才写 `0`。
`sleep_date` 必须先确定，否则暂不写入正式月档。分钟值、次数为非负整数。

| 字段 | 类型 | 含义与来源 |
| --- | --- | --- |
| `sleep_date` | string | 本地醒来日 |
| `sleep_start` | string / null | 入睡时间；带日期与 UTC 偏移的 ISO 8601 时间 |
| `sleep_end` | string / null | 醒来时间；带日期与 UTC 偏移的 ISO 8601 时间 |
| `total_sleep_min` | integer / null | App 显示的总睡眠时长，统一为分钟 |
| `deep_sleep_min` | integer / null | 深睡分钟 |
| `light_sleep_min` | integer / null | 浅睡分钟 |
| `rem_sleep_min` | integer / null | 快速眼动睡眠分钟 |
| `sleep_score` | integer / null | App 显示的睡眠分数 |
| `wake_count` | integer / null | App 显示的清醒次数 |
| `deep_sleep_continuity_score` | integer / null | App 显示的深睡连续性分数 |
| `breathing_quality_score` | integer / null | App 显示的呼吸质量分数 |
| `awake_min` | integer / null | 仅限 App 直接显示的清醒分钟；未显示必须为 null |
| `metadata` | object | 下表定义的来源与入档信息 |
| `notes` | string[] | 日期补全依据、待核问题或用户明确提供的补充；无则空数组 |

分数保留 App 的原值和标签，不自行套用分值范围、健康阈值或解释病因。设备不是
必填项；若用户明确提供设备或感受，可在 `notes` 注明来自用户，不冒充图中事实，
也不从分数推断感受。

`metadata`：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `contract_version` | integer | 固定为 `1` |
| `source` | string / null | 图中或用户明确说明的 App 来源，如 `huawei_health`；不明则 null |
| `evidence_refs` | string[] | 非空且去重；从月档文件出发到原图的相对路径 |
| `recorded_at` | string | 首次入档的实际时间，ISO 8601，带日期与 UTC 偏移 |
| `updated_at` | string，选填 | 已有内容实际被更正或补充的时间；单纯重复提交不改它 |
| `timezone` | string / null | 已知的 IANA 时区；不明则 null |

`recorded_at` 在更正时保持首次值；用 `updated_at` 表达后续实际变更。不要求也不
根据手机状态栏的时分构造完整拍摄时间。

## 日期、未知值与校验

先使用截图可见日期、时间和明确上下文。只有已有配置或会话上下文确定时区时，
才可使用 `Asia/Shanghai`。年份、时区、跨午夜的起始日期若由上下文补全，须在
`notes` 说明依据；不能把系统当前日期当作所有历史截图的年份。时区不明时保留
可见钟点在 `notes`，完整时间字段保持 `null`，直到可以补全偏移。

小时和分钟统一换算为整数分钟；不可辨认的数据保持 `null`，不靠比例反推缺失
分期。不把近期趋势、其他日期的指标、营养建议或宣传文字转为这晚的事实。

同时有深睡、浅睡、REM 和总时长时，检查三项之和是否等于总睡眠。完整起止时间
存在时计算跨度；跨度减去总睡眠仅是派生差值，可写进 `notes`，**不能填充
`awake_min`**。若图中直接给出清醒时长，才录入该源值。

分期合计不符、总睡眠超过跨度或源值互相冲突时，回看相关原图。仍不一致则保留
可辨认的来源值，在 `notes` 标明“待核”与具体差异，不凑数、不推断病因。
新图未显示某字段时保留已有有据值；只有确有更正依据才替换。冲突无法决定取值时，
保留原有值并注明其他来源值待核；新记录的该字段可为 `null`，在备注保留冲突读数。

## 原图与幂等更新

原图按字节计算 SHA-256，建议命名为 `<sha256>.<原扩展名>`，保存在对应醒来日
目录。相同内容复用一个文件与一条引用；不同截图保留各自副本。不要压缩、裁剪或
转码后替代原件；不覆盖同名但内容不同的文件。

可选的月度无损 ZIP 只是额外副本，散图与 `evidence_refs` 保持原样。保留、复核和
归档策略见 [初始化参考](initialization-v1.md#截图复核与归档)，不改变本合同版本。

写入时先读目标月档和对应日期小节：

1. 日期不存在则新增小节；存在则在原处合并有据字段与去重后的证据列表。
2. 无新信息时保持记录不变；有更正时保留首次 `recorded_at`，更新 `updated_at`。
3. 保持其他日期小节及手写文字。已有重复日期小节先合并并去重，不继续叠加。
4. 回读确认日期唯一、JSON 可解析、字段类型正确、所有相对证据路径存在且哈希与
   原图一致，再同步 `02-life.md` 的睡眠小节。

`02-life.md` 可以链接 `[睡眠月档](health/sleep/YYYY-MM.md)`；详细记录仍以月档
为准。新建最小索引可仅含文档标题、`## 睡眠` 和月档链接。保护已有生活内容，
只维护睡眠链接或索引，不在此新增数值或感受摘要；更正归属月份时同步链接。

## 合成示例

以下日期、数值、文件名均为虚构，只展示格式。起止跨度 450 分钟、总睡眠 420
分钟，差 30 分钟没有被当作清醒源值。

````markdown
# 睡眠 · 2030-04

## 2030-04-16

```json
{
  "sleep_date": "2030-04-16",
  "sleep_start": "2030-04-15T23:40:00+08:00",
  "sleep_end": "2030-04-16T07:10:00+08:00",
  "total_sleep_min": 420,
  "deep_sleep_min": 90,
  "light_sleep_min": 240,
  "rem_sleep_min": 90,
  "sleep_score": 82,
  "wake_count": 1,
  "deep_sleep_continuity_score": 78,
  "breathing_quality_score": 96,
  "awake_min": null,
  "metadata": {
    "contract_version": 1,
    "source": "example_sleep_app",
    "evidence_refs": [
      "../_raw/sleep/2030-04-16/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
    ],
    "recorded_at": "2030-04-16T08:00:00+08:00",
    "timezone": "Asia/Shanghai"
  },
  "notes": [
    "时区按用户已知配置补全；入睡日期按跨午夜顺序补全。",
    "图中未显示清醒分钟；起止跨度与总睡眠的派生差值为 30 分钟。"
  ]
}
```
````
