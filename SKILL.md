---
name: literature-classifier
description: Classify and reorganize a folder of literature PDFs by research theme and document type. Use this skill whenever the user wants to批量整理文献、给 PDF 文献分类、区分期刊/学位/综述/会议/工程文档、把新放进根目录的论文自动归档，或希望对扫描版 PDF 启用 OCR 后再分类。 This skill also applies when the classifier needs to泛化到现有主题之外，但仍尽量回填到已有研究方向而不是随意创建新主题。
---

# Literature Classifier

对文献库根目录中的 PDF 进行批量分类、生成清单、复核候选主题，并在确认后重排目录。

## 何时使用

- 用户说“把这些论文分类”“整理文献目录”“按研究主题归档 PDF”
- 用户明确要求区分 `期刊论文 / 学位论文 / 综述论文 / 会议论文 / 工程文档`
- 用户要求先读取 PDF 内容，再分类，而不是按文件名粗分
- 用户希望扫描版 PDF 先做 OCR 再分类
- 用户提到“不要随意增加新主题”“新主题要收敛”“分类要通用化”
- 用户希望参考现有研究方向表或外部学科分类先验

## 核心原则

- 先读 PDF 内容，再分类，不只看文件名。
- 主题分类优先综合 `标题 + 摘要 + 关键词 + 研究主题上下文` 的相关性分数。
- 默认优先回填到已有主题；只有与所有已有主题都明显不相关时，才允许进入候选新主题。
- `pore network / pore-network / pore networks / PNM` 视为同一类线索。
- `LBM / lattice Boltzmann`、`PINN / physics-informed neural networks`、`ML / machine learning` 等方法词既可作主题，也可作辅助标签。
- 前 4 页中如果第一页是 cover，会自动跳过，优先从真正开始出现摘要/关键词/正文结构的页面抽取信号。

## 输入与配置

- 文献库根目录：通常 PDF 在根目录或现有主题目录下。
- 可选根目录配置：`literature_classifier_config.json`

支持的配置键：

```json
{
  "manual_overrides": {
    "example.pdf": {
      "doc_type": "期刊论文",
      "primary_theme": "PNM-孔隙网络模型",
      "doc_subtype": "",
      "reason": "manual fix after review"
    }
  },
  "theme_aliases": {
    "CFD与数值方法参考": ["backward-facing step flow"]
  },
  "theme_promotions": {
    "新主题-SPH": "SPH-光滑粒子流体动力学"
  }
}
```

## 分类方法

### 类型判断

- 类型层只使用：
  - `期刊论文`
  - `学位论文`
  - `综述论文`
  - `会议论文`
  - `工程文档`
- `其他资料` 已取消。
- 专著/教材/书章/讲义/技术手册等统一并入 `工程文档`，并通过 `doc_subtype` 细分。

### 主题判断

- 一级主题先按本地研究方向表打分：
  - `references/taxonomy.md`
- 再参考外部学科先验：
  - `references/reference_taxonomy.json`
  - 其中包含 Scopus ASJC / Web of Science Research Areas 到本地主题的映射
- 每篇论文会输出：
  - `title_score`
  - `abstract_score`
  - `keyword_score`
  - `topic_score`
  - `theme_relevance_score`
  - `reference_prior`
  - `new_theme_gate`

### 新主题策略

- 候选主题使用 `新主题-*` 目录，但默认强门控。
- 只有在这些条件下才会进入候选主题：
  - 与已有主题的相关性分数明显偏低
  - 外部学科先验无法回填到已有主题
  - 主题上下文重叠率也不足以支撑回填
- 候选主题会写入：
  - `.literature-classifier/theme_registry.json`
- 候选主题可以通过 `theme_promotions` 提升为正式中文主题名。

## 工作流程

### Step 1: Dry-run

```bash
python "<skill_dir>/scripts/classify_literature.py" --root "<literature_root>"
```

输出：

```text
<literature_root>/.literature-classifier/classification_manifest.csv
<literature_root>/.literature-classifier/classification_summary.md
<literature_root>/.literature-classifier/theme_registry.json
```

### Step 2: 重点检查

- `classification_summary.md`
  - `工程文档` 中是否混入明显期刊论文
  - `自动生成新主题` 是否还有不该存在的候选主题
  - `新主题抑制统计` 是否足够高
  - `主题来源 / 主题状态 / 主题相关性分数` 是否合理
- `classification_manifest.csv`
  - 低分论文是否被回填到合适的已有主题
  - `reference_prior` 与 `new_theme_gate` 是否解释得通
- `theme_registry.json`
  - 候选主题是否需要提升成正式主题

### Step 3: 修正误分

- 若个别文件误分：改 `manual_overrides`
- 若已有主题别名不够：改 `theme_aliases`
- 若候选主题应转正：改 `theme_promotions`

### Step 4: 执行重排

```bash
python "<skill_dir>/scripts/classify_literature.py" --root "<literature_root>" --execute
```

执行后会：

- 按 `<主题>/<类型>/[可选第三级方向]/文件.pdf` 重建目录
- 清理空目录
- 保留 `.literature-classifier/` 作为清单、汇总和候选主题注册表输出目录

## 常见检查点

- 如果 `工程文档` 中有 DOI、期刊名、摘要和关键词，优先检查是否误判成专著/教材
- 如果 `新主题-*` 中有 `multiphase / porous / soil / microstructure / battery` 等词，优先尝试回填到已有主题
- 如果前几页像 cover，重点检查摘要和关键词是否其实从第 2-4 页开始
- 如果 OCR 未命中，查看 manifest 中的 `ocr_error`

## 参考文件

- 主题与子方向说明：`references/taxonomy.md`
- 配置示例：`references/config-example.json`
- 外部学科先验映射：`references/reference_taxonomy.json`
- 测试提示：`evals/evals.json`
