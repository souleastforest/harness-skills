# 名单工具 2.0 发布说明

本版导出默认使用 UTF-8 编码。旧版的 export_file 函数已移除，调用者需改用 export_rows。

export_rows 接收筛选后的行列表，返回 CSV 文本；调用者负责选择保存位置。空结果仍返回字段表头。字段顺序为 name、team、hours、note。

升级前请保存现有名单。名单 JSON 格式保持不变。
