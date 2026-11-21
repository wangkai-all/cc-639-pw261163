# DICOM 收件工作站（合成样本演示）

把 DICOM 收件程序做成网页工作站：通过项目自带的演示影像服务查询检查、
选择序列、接收实例，再查看结构化字段、引用关系和接收报告，最后导出
匿名化下载包。

**全部样本由程序生成（`server/demo_data.py`），不涉及真实病人，也不做
诊断；像素预览仅用于核对实例。**

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh          # 同时启动演示影像服务(11112)与 Web 工作站(8000)
```

打开 http://127.0.0.1:8000 ：

1. **查询与接收** — C-FIND 查询检查 → 展开序列 → 查看实例预告（含传输
   语法）→ 「接收此序列」C-MOVE 到本机库，立即显示每实例的接收结果。
2. **本地接收库** — 检查/序列/实例三级层级树；实例详情含分组结构化
   字段（文件元信息/标识与引用/身份与描述/采集参数/私有字段）、引用
   关系（被引实例在库/缺失、被谁引用）与像素预览；标识冲突与隔离区
   单独列出。
3. **报告与导出** — 接收报告、引用完整性报告、匿名化规则全文，一键
   生成并下载 ZIP。

运行测试：` .venv/bin/python -m pytest tests/ -q `（17 项，含进程内
C-FIND/C-MOVE 集成测试）。

## 架构

```
浏览器 (Vue 3, static/)
   │ HTTP/JSON
FastAPI (server/main.py)
   ├─ receiver.py   C-FIND/C-MOVE SCU + 内嵌 C-STORE SCP(11113)
   ├─ store.py      本地库：三级索引、去重、冲突、隔离（data/）
   ├─ anonymize.py  匿名化规则引擎
   ├─ references.py 引用提取与完整性报告
   └─ pixel.py      像素预览 PNG
演示影像服务 (server/demo_scp.py, 11112)
   └─ demo_data.py  程序化合成样本（确定性 UID，2.25 弧）
```

接收链路：页面选序列 → 工作站发 C-MOVE → 演示服务把实例 C-STORE 推到
工作站内嵌 SCP → 用命令集 + 协商的表示上下文重建文件元信息 → 入库。

## 接收时的三个层级与异常处理

- **检查 / 序列 / 实例**三级在查询、本地索引、层级展示与导出表中始终
  分开。
- **隔离**：C-STORE 命令集的 Affected SOP Instance/Class UID 与数据集
  内的 SOP Instance/Class UID 不一致（即重建的文件元信息与数据集标识
  不一致）时，实例进入隔离区，不进层级树、不参与引用完整性统计，页面
  与报告中单独展示。
- **冲突**：同一 SOP Instance UID 带来不同内容（按内容哈希判断）时，
  两个版本都保留，标记冲突并给出字段级差异，**绝不静默覆盖**；内容
  完全一致的重复接收记为 duplicate。

## 匿名化规则（`server/anonymize.py`，随导出包附带）

1. **移除**：身份/人口学字段（PatientName、PatientID、生日等）、医护
  与机构标识、预约标识、设备标识、全部日期时间、自由文本。
2. **保留**：采集与几何参数（Modality、矩阵、层厚、KVP、TR/TE、窗宽
  窗位、方位等），规则文档中有明确清单。
3. **UID 重映射**：只重映身份 UID——Study/Series/SOP Instance UID、
  Frame of Reference UID 及对它们的全部引用。递归遍历嵌套序列（如
  ReferencedStudySequence > ReferencedSeriesSequence >
  ReferencedSOPSequence、SourceImageSequence），同一次导出内同一原始
  UID 映射到同一新 UID（`2.25.<uuid5(salt, original)>`，salt 每次导出
  随机），因此跨实例引用保持一致；文件元信息的 Media Storage SOP
  Instance UID 同步为映射后的数据集 SOP Instance UID。未知且非周知的
  UID 按防呆策略也重映射。
4. **类型标识保留**：SOP Class UID（含 Referenced SOP Class UID）、
  Transfer Syntax UID、周知 UID 绝不重映射。**Transfer Syntax UID 必须
  对应实际编码——本系统不做任何转码，原值保留**，不作为身份 UID 替换。
5. **私有字段**：默认全部移除（可配置白名单，默认为空）——作用于所有
  私有标签，而非仅页面可见字段。

## 导出下载包（`POST /api/export`）

```
tables/studies.json|csv      检查层结构化表（匿名标识）
tables/series.json|csv       序列层
tables/instances.json|csv    实例层（含传输语法）
anonymized/<study>/<series>/<instance>.dcm   匿名化文件（原传输语法）
reports/reference_integrity.json   引用完整性报告（匿名标识空间）
reports/anonymization_rules.json   匿名化规则全文
reports/processing_summary.json    处理摘要：各类动作计数，不含任何原始
                                   身份；原始→匿名 UID 映射表不随包导出
```

## 演示样本（`server/demo_data.py`，10 个实例 / 2 检查 / 6 序列）

| 序列 | 内容 | 演示点 |
|---|---|---|
| 研究一/序列1 | CT ×3，Explicit VR LE | 私有标签；实例3 为派生图像，SourceImageSequence 引用实例1/2 |
| 研究一/序列2 | CT ×2，Implicit VR LE | 不同传输语法 |
| 研究一/序列3 | Key Object Selection ×1 | 三层嵌套引用；其中一个被引实例不存在（缺失引用） |
| 研究一/序列6 | CT ×1 | 与序列1实例1 **同一 SOP Instance UID 但内容不同**（标识冲突） |
| 研究二/序列4 | MR ×2，Deflated Explicit VR LE | 第三种传输语法；与序列1共享 Frame of Reference UID |
| 研究二/序列5 | CT ×1 | C-STORE 命令集 UID 与数据集 UID 不一致（隔离演示） |

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/remote/studies` | C-FIND 检查列表 |
| GET | `/api/remote/studies/{uid}/series` | C-FIND 序列 |
| GET | `/api/remote/series/instances?study_uid=&series_uid=` | C-FIND 实例 |
| POST | `/api/retrieve` | C-MOVE 接收序列 `{study_uid, series_uid}` |
| GET | `/api/studies` | 本地三级层级 |
| GET | `/api/instances/{uid}` | 结构化字段（分组） |
| GET | `/api/instances/{uid}/references` | 引用关系（出/入） |
| GET | `/api/instances/{uid}/pixel.png` | 像素预览 |
| GET | `/api/conflicts` `/api/quarantine` | 冲突 / 隔离 |
| GET | `/api/reports/receiving` `/api/reports/integrity` | 接收报告 / 引用完整性 |
| GET | `/api/anonymization/rules` | 匿名化规则 |
| POST | `/api/export` | 生成并下载 ZIP |
| POST | `/api/reset` | 清空本地库 |
