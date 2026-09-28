# 医学数据交换核心（mde）

本轮只接入一种**明确限定的本地交换格式** `mde-json` v1，导入统一记录后可再导出，
重点是患者、就诊（encounter）与检查（study）之间引用的可靠保留。

## 交换格式

一个 JSON 信封即一个来源批次：

```json
{
  "format": "mde-json",
  "format_version": 1,
  "source_id": "hospital-a",
  "batch_id": "20260929-01",
  "records": {
    "patient":   [{"id": "p1", "version": 1, "name": "张伟"}],
    "encounter": [{"id": "e1", "version": 1, "patient_ref": "p1"}],
    "study":     [{"id": "s1", "version": 1,
                    "encounter_ref": "e1", "patient_ref": "p1"}]
  }
}
```

引用是来源内部的本地 id；不同 `source_id` 的命名空间互相隔离。

## 关键规则

- 身份 = `source_id + 类型 + 本地 id`。同一身份的更新覆盖版本，不会产生第二个患者；
  绝不按姓名等属性做模糊匹配。
- 引用目标尚未到达时，关系进入 `pending_relations`；目标到达后自动兑现。乱序到达
  不丢关系，同名人也不会被错误配给。
- 版本：更大版本号为更新；更小为重放（`stale`）；同版本同内容为重复
  （`duplicate`）；同版本不同内容为冲突（批次结果 `ok=False`，记录冲突明细）。
- 批量边界：整批先做格式校验，不合法则整批不写入；批次由 `(source_id, batch_id)`
  幂等识别，重复导入返回 `duplicate_batch`，不重复记账。
- 导出：
  - `export_unified()` 输出统一图，链接为稳定 uid，未解析链接为 `null` 并列入
    `pending_relations`。
  - `export_source_envelope(source_id)` 回导为 `mde-json`，只包含引用已全部解析的记录，
    保证再导入仍能还原患者-就诊-检查关系。

## 使用

```python
from mde import RecordStore

store = RecordStore()
store.import_envelope(envelope_dict)      # 或 store.import_file("b.json")
store.export_unified()
store.export_source_envelope("hospital-a")
```

运行测试：

```bash
python -m pytest -q
```
