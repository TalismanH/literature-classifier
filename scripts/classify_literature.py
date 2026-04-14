#!/usr/bin/env python3
"""Reclassify and reorganize the PDF library by research content."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import fitz  # PyMuPDF

try:
    from PIL import Image
except ImportError:  # pragma: no cover - depends on local env
    Image = None

try:
    import pytesseract
except ImportError:  # pragma: no cover - depends on local env
    pytesseract = None


FIRST_PAGES = 4
OCR_PAGES = 3
LOW_TEXT_THRESHOLD = 500
LOW_FRONT_THRESHOLD = 120
OCR_LANGUAGE = "eng+chi_sim"
VALID_DOC_TYPES = {"期刊论文", "学位论文", "综述论文", "会议论文", "工程文档"}
DOC_TYPE_ORDER = ["期刊论文", "学位论文", "综述论文", "会议论文", "工程文档"]
TESSERACT_ENV_VARS = ("TESSERACT_CMD", "PYTESSERACT_TESSERACT_CMD")
COMMON_TESSERACT_PATHS = [
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Users\Lenovo\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Users\Lenovo\miniconda3\envs\pnw\Library\bin\tesseract.exe"),
    Path(r"C:\Users\Lenovo\miniconda3\envs\pnw\Scripts\tesseract.exe"),
    Path(r"E:\Softwares\Tesseract-OCR\tesseract.exe"),
]
DEFAULT_OUTPUT_DIRNAME = ".literature-classifier"
CONFIG_FILENAME = "literature_classifier_config.json"
THEME_REGISTRY_FILENAME = "theme_registry.json"
REFERENCE_TAXONOMY_FILENAME = "reference_taxonomy.json"
CANDIDATE_THEME_PREFIX = "新主题-"
PROMOTION_SUGGESTION_THRESHOLD = 3
NEW_THEME_SCORE_THRESHOLD = 15
DIRECT_ASSIGN_THRESHOLD = 45
AMBIGUOUS_ASSIGN_THRESHOLD = 35
LOW_CONFIDENCE_ASSIGN_THRESHOLD = 15
SIGNAL_OVERLAP_THRESHOLD = 0.2


def resolve_tesseract_cmd() -> str:
    for env_name in TESSERACT_ENV_VARS:
        value = os.environ.get(env_name)
        if value and Path(value).exists():
            return value

    which_hit = shutil.which("tesseract")
    if which_hit:
        return which_hit

    for path in COMMON_TESSERACT_PATHS:
        if path.exists():
            return str(path)

    return ""


TESSERACT_CMD = resolve_tesseract_cmd()
OCR_AVAILABLE = bool(TESSERACT_CMD and pytesseract is not None and Image is not None)
if OCR_AVAILABLE:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    tessdata_dir = Path(TESSERACT_CMD).parent / "tessdata"
    if tessdata_dir.exists() and not os.environ.get("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)


def normalize_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = text.replace("\u2010", "-").replace("\u2011", "-")
    text = text.replace("\u2012", "-").replace("\u2013", "-")
    text = text.replace("\u2014", "-").replace("\u2212", "-")
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = normalize_text(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def normalize_match_text(text: str) -> str:
    normalized = normalize_text(text).lower().replace("-", " ")
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def is_cover_like_text(text: str) -> bool:
    normalized = normalize_match_text(text)
    if not normalized:
        return True
    cover_markers = [
        "downloaded from",
        "all rights reserved",
        "pre proof",
        "accepted manuscript",
        "journal pre proof",
        "copyright",
    ]
    return len(normalized) < 120 and any(marker in normalized for marker in cover_markers)


def select_content_start_index(page_texts: list[str]) -> int:
    best_index = 0
    best_score = -1
    for index, page_text in enumerate(page_texts):
        normalized = normalize_text(page_text)
        lowered = normalize_match_text(page_text)
        score = 0
        if not is_cover_like_text(page_text):
            score += 1
        if len(normalized) > 200:
            score += 1
        if re.search(r"\babstract\b|摘要", lowered, re.IGNORECASE):
            score += 4
        if re.search(r"\bkeywords?\b|关键词|主题词", lowered, re.IGNORECASE):
            score += 3
        if re.search(r"\bintroduction\b|引言|1\.", lowered, re.IGNORECASE):
            score += 2
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def extract_abstract_text(front_text: str) -> str:
    text = normalize_text(front_text)
    patterns = [
        r"(?:\babstract\b|摘要)\s*[:：]?\s*(.{80,2000}?)(?=\bkeywords?\b|关键词|index terms|1\.\s|introduction|引言|materials and methods|methods\b|©|references\b)",
        r"(?:\babstract\b|摘要)\s*[:：]?\s*(.{80,2000})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_text(match.group(1))[:1800]
    return ""


def extract_keyword_terms(front_text: str, metadata_subject: str) -> list[str]:
    text = normalize_text(front_text)
    keyword_block = ""
    patterns = [
        r"(?:\bkeywords?\b|key words|index terms|关键词|主题词)\s*[:：]?\s*(.{5,400}?)(?=\bintroduction\b|引言|1\.\s|materials and methods|methods\b|©|references\b)",
        r"(?:\bkeywords?\b|key words|index terms|关键词|主题词)\s*[:：]?\s*(.{5,400})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            keyword_block = match.group(1)
            break
    raw_terms = re.split(r"[;,，；·•]\s*|\s{2,}", keyword_block)
    if metadata_subject:
        raw_terms.extend(re.split(r"[;,，；|]\s*", normalize_text(metadata_subject)))
    filtered = []
    for term in raw_terms:
        cleaned = normalize_text(term).strip(" .,:;")
        if len(cleaned) < 2:
            continue
        filtered.append(cleaned)
    return unique_preserve_order(filtered)


def extract_topic_terms(title_text: str, abstract_text: str, keyword_terms: list[str], metadata_subject: str) -> list[str]:
    combined = normalize_text(f"{title_text} {abstract_text} {metadata_subject}")
    zh_terms = re.findall(r"[\u4e00-\u9fff]{2,12}", combined)
    en_terms = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", combined)
    stop_en = {
        "study", "analysis", "model", "models", "modelling", "modeling", "method", "methods", "based", "using",
        "paper", "review", "article", "research", "results", "introduction", "abstract", "keywords", "conclusions",
    }
    stop_zh = {"研究", "分析", "方法", "模型", "结果", "引言", "摘要", "关键词", "结论", "应用"}
    topic_terms = list(keyword_terms)
    topic_terms.extend(term for term in zh_terms if term not in stop_zh)
    topic_terms.extend(term for term in en_terms if term.lower() not in stop_en)
    return unique_preserve_order(topic_terms[:40])


@dataclass(frozen=True)
class ThemeRule:
    folder: str
    title_patterns: list[re.Pattern[str]]
    front_patterns: list[re.Pattern[str]]
    body_patterns: list[re.Pattern[str]]


@dataclass
class ExtractedPaper:
    metadata_title: str
    metadata_subject: str
    page_count: int
    front_text: str
    full_text: str
    abstract_text: str
    keyword_terms: list[str]
    topic_terms: list[str]
    native_char_count: int
    ocr_used: bool
    ocr_error: str
    source_text_mode: str


@dataclass
class Classification:
    source_path: Path
    source_name: str
    doc_type: str
    doc_subtype: str
    primary_theme: str
    confidence: str
    type_evidence: str
    theme_evidence: str
    evidence_snippet: str
    method_tags: list[str] = field(default_factory=list)
    ocr_used: bool = False
    ocr_error: str = ""
    source_text_mode: str = "native"
    subtopic: str = ""
    target_relpath: str = ""
    theme_key: str = ""
    theme_origin: str = "anchor"
    theme_status: str = "anchor"
    title_score: int = 0
    abstract_score: int = 0
    keyword_score: int = 0
    topic_score: int = 0
    theme_relevance_score: int = 0
    reference_prior: str = ""
    new_theme_gate: str = ""
    abstract_excerpt: str = ""
    keyword_terms: list[str] = field(default_factory=list)
    topic_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ThemeSelection:
    primary_theme: str
    confidence: str
    theme_evidence: str
    theme_key: str
    theme_origin: str
    theme_status: str
    title_score: int
    abstract_score: int
    keyword_score: int
    topic_score: int
    theme_relevance_score: int
    reference_prior: str
    new_theme_gate: str


@dataclass
class UserConfig:
    manual_overrides: dict[str, tuple[str, str, str, str]]
    theme_aliases: dict[str, list[str]]
    theme_promotions: dict[str, str]


@dataclass
class ThemeRuntime:
    theme_aliases: dict[str, list[str]]
    theme_promotions: dict[str, str]
    registry_entries: dict[str, dict]
    reference_taxonomy: dict[str, dict]
    candidate_cache: dict[str, str] = field(default_factory=dict)


TYPE_PATTERNS = {
    "thesis": compile_patterns(
        [
            r"\bthesis\b",
            r"\bdissertation\b",
            r"\bdoctoral dissertation\b",
            r"\bdoctor of philosophy\b",
            r"\bmaster thesis\b",
            r"\bmaster of science\b",
            r"\bmaster of engineering\b",
            r"\betd\b",
            r"\btheses and dissertations\b",
            r"硕士论文",
            r"硕士学位论文",
            r"博士论文",
            r"博士学位论文",
            r"学位论文",
        ]
    ),
    "review": compile_patterns(
        [
            r"\ba review\b",
            r"\breview article\b",
            r"\bcomprehensive review\b",
            r"\bcomprehensive investigation\b",
            r"\bcomprehensivee investigation\b",
            r"\bcomprehensive analysis\b",
            r"\bsystematic literature analysis\b",
            r"\bsystematic review\b",
            r"\bliterature review\b",
            r"\bsurvey\b",
            r"\bstate[- ]of[- ]the[- ]art review\b",
            r"综述",
            r"研究进展",
        ]
    ),
    "book": compile_patterns(
        [
            r"\bcambridge university press\b",
            r"\bcrc press\b",
            r"\bisbn\b",
            r"\btextbook\b",
            r"\bmonograph\b",
            r"第\s*\d+\s*章",
        ]
    ),
    "book_explicit": compile_patterns(
        [
            r"\(auth\.\)",
            r"\(\d{4},\s*springer\)",
            r"anna.?s archive",
        ]
    ),
    "book_chapter": compile_patterns([r"^\s*chapter\s+\d+\b", r"^\s*\d+\.\d+\s+[A-Z]"]),
    "engineering_doc": compile_patterns(
        [
            r"\buser manual\b",
            r"\buser guide\b",
            r"\bsoftware documentation\b",
            r"\bsoftware design\b",
            r"\bdesign document\b",
            r"\btechnical specification\b",
            r"\bweb app user guide\b",
            r"\bmanual\b",
            r"软件设计",
            r"设计文档",
            r"用户指南",
            r"说明书",
            r"技术手册",
        ]
    ),
    "journal_en": compile_patterns(
        [
            r"\bopen access\b",
            r"\barticle\b",
            r"\bdoi[:/ ]",
            r"\baccepted[: ]",
            r"\breceived[: ]",
            r"\bpublished online[: ]",
            r"\bavailable online\b",
            r"\bjournal homepage\b",
            r"\bcontents lists available\b",
            r"\barticle info\b",
            r"\barticle history\b",
            r"\barticle open\b",
            r"\boriginal article\b",
            r"\bresearch article\b",
            r"\bresearch paper\b",
            r"\bprogram summary\b",
            r"\bedited by\b",
            r"\brights reserved\b",
            r"\bscientific reports\b",
            r"\belsevier\b",
            r"\bspringer\b",
            r"\bwiley\b",
            r"\btaylor\s*&\s*francis\b",
            r"\bphys\.\s*fluids\b",
            r"\bjournal of\b",
            r"\bvol\.?\s*\d+",
            r"\bvolume\s*\d+",
            r"\bnumber\s*\d+",
            r"\bpacs number",
        ]
    ),
    "journal_cn": compile_patterns(
        [
            r"第\s*\d+\s*卷",
            r"第\s*\d+\s*期",
            r"收稿日期",
            r"修回日期",
            r"基金项目",
            r"文章编号",
            r"中图分类号",
            r"关键词",
            r"摘要",
            r"CN\s*\d{4}-\d+",
            r"ISSN\s*\d{4}-\d+",
        ]
    ),
    "conference": compile_patterns(
        [
            r"\bprepared for presentation at\b",
            r"\bconference\b",
            r"\bmeeting\b",
            r"\bproceedings\b",
            r"\bsymposium\b",
            r"\bworkshop\b",
            r"\bheld in\b",
            r"\bSPE\/EAGE\b",
            r"\bSPE\b",
        ]
    ),
    "preprint": compile_patterns([r"\barxiv[: ]", r"\bpreprint\b"]),
    "lecture_note": compile_patterns([r"\blecture\b", r"\bnotes by\b", r"\bhandout\b"]),
    "book_support": compile_patterns([r"\bpublisher\b", r"出版社", r"\bpress\b", r"\bedition\b", r"\bchapter\b"]),
}


METHOD_TAG_PATTERNS = {
    "PNM": compile_patterns(
        [
            r"pore[- ]?networks?\b",
            r"\bpore[- ]network model(?:s|ing)?\b",
            r"\bPNM\b",
            r"\bPNW\b",
        ]
    ),
    "LBM": compile_patterns([r"\blattice[- ]boltzmann\b", r"\bLBM\b"]),
    "PINN": compile_patterns([r"\bphysics[- ]informed neural network(?:s)?\b", r"\bPINN(?:s)?\b"]),
    "ML": compile_patterns(
        [
            r"\bmachine learning\b",
            r"\bdeep learning\b",
            r"\bdata-driven\b",
            r"\breinforcement learning\b",
            r"\bmeta-learning\b",
            r"\bneural network(?:s)?\b",
            r"\bartificial intelligence\b",
        ]
    ),
}


THEME_RULES = [
    ThemeRule(
        folder="电化学多孔电极与浸润",
        title_patterns=compile_patterns(
            [
                r"\blithium[- ]ion batter(?:y|ies)\b",
                r"\belectrolyte fill(?:ing)?\b",
                r"\belectrolyte wett(?:ing|ability)\b",
                r"\bporous electrode\b",
                r"\belectrode\b",
                r"\bfuel cell\b",
                r"\bgas diffusion layer\b",
                r"\bporous transport layer\b",
                r"锂离子电池",
                r"电解液",
                r"浸润",
                r"浸润性",
                r"电极",
                r"燃料电池",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\blithium[- ]ion batter(?:y|ies)\b",
                r"\belectrode\b",
                r"\belectrolyte\b",
                r"\bfuel cell\b",
                r"\bgas diffusion layer\b",
                r"\bporous transport layer\b",
                r"锂离子电池",
                r"电解液",
                r"燃料电池",
            ]
        ),
        body_patterns=compile_patterns([r"\belectrolyte\b", r"\belectrode\b", r"\bgas diffusion layer\b"]),
    ),
    ThemeRule(
        folder="孔隙结构与数字岩心",
        title_patterns=compile_patterns(
            [
                r"\bQSGS\b",
                r"\bpore[- ]structure\b",
                r"\bpore[- ]space reconstruction\b",
                r"\bpore[- ]network extraction\b",
                r"\bdigital rock\b",
                r"\bdigital core\b",
                r"\bdigital rock physics\b",
                r"\bporous media reconstruction\b",
                r"\bporous structure reconstruction\b",
                r"\bmultiple-point statistics\b",
                r"\brandom sphere pack(?:s|ing)?\b",
                r"\brandom pack(?:s|ings)?\b",
                r"\bgaussian random field\b",
                r"\bdiffusion model[- ]based generation\b",
                r"\bstochastic reconstruction\b",
                r"\bgenerate random packed\b",
                r"\bSierpinski\b",
                r"\bnetwork extraction\b",
                r"\bpore[- ]throat segmentation\b",
                r"\bsegmentation of porous\b",
                r"\btomograph(?:y|ic)\b",
                r"\bmicro[- ]CT\b",
                r"\bFIB[- ]SEM\b",
                r"\bimage[- ]based porous\b",
                r"\bvoxelization\b",
                r"\bporous microstructure\b",
                r"\bmicrostructure characterization\b",
                r"\bvisuali[sz]ation\b",
                r"\bx-ray ct\b",
                r"孔隙结构",
                r"孔隙表征",
                r"孔隙重构",
                r"孔隙结构重构",
                r"孔隙结构生成",
                r"网络提取",
                r"数字岩心",
                r"表征",
                r"可视化",
                r"层析",
                r"断层扫描",
                r"重构",
                r"随机场",
                r"分形",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\bQSGS\b",
                r"\bpore[- ]structure\b",
                r"\bdigital rock\b",
                r"\bnetwork extraction\b",
                r"\btomograph(?:y|ic)\b",
                r"\bmicro[- ]CT\b",
                r"\bimage[- ]based porous\b",
                r"\bsegmentation\b",
                r"\bvoxelization\b",
                r"\bgaussian random field\b",
                r"\bmultiple-point statistics\b",
                r"\brandom sphere pack(?:s|ing)?\b",
                r"\bporous media reconstruction\b",
                r"\bporous microstructure\b",
                r"孔隙结构",
                r"孔隙表征",
                r"孔隙重构",
                r"网络提取",
                r"数字岩心",
                r"表征",
                r"可视化",
                r"重构",
                r"随机场",
            ]
        ),
        body_patterns=compile_patterns(
            [
                r"\bQSGS\b",
                r"\bpore[- ]structure\b",
                r"\bdigital rock\b",
                r"\bnetwork extraction\b",
                r"\btomograph\b",
                r"\bsegmentation\b",
                r"\bvoxel\b",
                r"\bporous media reconstruction\b",
                r"\bporous microstructure\b",
                r"孔隙结构",
                r"数字岩心",
                r"网络提取",
                r"重构",
            ]
        ),
    ),
    ThemeRule(
        folder="自发渗吸与毛细现象",
        title_patterns=compile_patterns(
            [
                r"\bspontaneous imbibition\b",
                r"\bimbibition\b",
                r"\bcapillary rise\b",
                r"\bcapillary pressure\b",
                r"\bhysteresis\b",
                r"\bwetting\b",
                r"\bdrying\b",
                r"\bdrainage\b",
                r"自发渗吸",
                r"芯吸",
                r"毛细上升",
                r"毛细压力",
                r"润湿",
                r"排驱",
                r"干燥",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\bspontaneous imbibition\b",
                r"\bcapillary rise\b",
                r"\bcapillary pressure\b",
                r"\bwetting\b",
                r"\bdrying\b",
                r"自发渗吸",
                r"芯吸",
                r"毛细",
            ]
        ),
        body_patterns=compile_patterns([r"\bimbibition\b", r"\bcapillary\b", r"自发渗吸", r"毛细"]),
    ),
    ThemeRule(
        folder="迂曲度与有效输运",
        title_patterns=compile_patterns([r"\btortuosity\b", r"\bformation factor\b", r"迂曲度"]),
        front_patterns=compile_patterns([r"\btortuosity\b", r"\bformation factor\b", r"迂曲度"]),
        body_patterns=compile_patterns([r"\btortuosity\b", r"迂曲度"]),
    ),
    ThemeRule(
        folder="页岩与纳米孔流动",
        title_patterns=compile_patterns([r"\bshale\b", r"\bnanopore\b", r"\btight formation\b", r"页岩", r"纳米孔", r"致密"]),
        front_patterns=compile_patterns([r"\bshale\b", r"\bnanopore\b", r"页岩", r"纳米孔", r"致密"]),
        body_patterns=compile_patterns([r"\bshale\b", r"\bnanopore\b", r"页岩", r"纳米孔"]),
    ),
    ThemeRule(
        folder="反应传输与吸附",
        title_patterns=compile_patterns([r"\breactive transport\b", r"\badsorption\b", r"\bdissolution\b", r"反应传输", r"吸附", r"溶解"]),
        front_patterns=compile_patterns([r"\breactive transport\b", r"\badsorption\b", r"\bdissolution\b", r"反应传输", r"吸附"]),
        body_patterns=compile_patterns([r"\breactive transport\b", r"\badsorption\b", r"\bdissolution\b"]),
    ),
    ThemeRule(
        folder="PNM-孔隙网络模型",
        title_patterns=compile_patterns(
            [
                r"\bOpenPNM\b",
                r"pore[- ]?networks?\b",
                r"\bpore[- ]network modeling package\b",
                r"\bnew pore[- ]network model\b",
                r"\bfully implicit .*pore[- ]network model\b",
                r"\bdynamic pore[- ]network model\b",
                r"\bvalidating the generalized pore network model\b",
                r"\breview of pore network modelling\b",
                r"\bpore[- ]network model(?:ling)?\b",
                r"\bpore[- ]network simulator\b",
                r"\bpore[- ]network approach\b",
                r"孔隙网络模型",
            ]
        ),
        front_patterns=compile_patterns([r"\bpore[- ]network model(?:ling)?\b", r"\bOpenPNM\b", r"孔隙网络模型"]),
        body_patterns=compile_patterns([r"\bpore[- ]network\b", r"\bOpenPNM\b", r"孔隙网络模型"]),
    ),
    ThemeRule(
        folder="LBM-格子玻尔兹曼",
        title_patterns=compile_patterns(
            [
                r"\blattice[- ]boltzmann methods?\b",
                r"\blattice[- ]boltzmann model\b",
                r"\blattice[- ]boltzmann solver\b",
                r"\blattice[- ]boltzmann library\b",
                r"\bimplementation of .* lattice boltzmann\b",
                r"\bmultiphase LBM library\b",
                r"\bSailfish\b",
                r"格子玻尔兹曼",
            ]
        ),
        front_patterns=compile_patterns([r"\blattice[- ]boltzmann methods?\b", r"\blattice[- ]boltzmann\b", r"格子玻尔兹曼"]),
        body_patterns=compile_patterns([r"\blattice[- ]boltzmann\b", r"\bLBM\b", r"格子玻尔兹曼"]),
    ),
    ThemeRule(
        folder="PINN-物理信息神经网络",
        title_patterns=compile_patterns(
            [
                r"\bphysics[- ]informed neural network(?:s)?\b",
                r"\bPINN(?:s)?\b",
                r"\bphysics[- ]informed learning\b",
                r"物理信息神经网络",
            ]
        ),
        front_patterns=compile_patterns([r"\bphysics[- ]informed neural network(?:s)?\b", r"\bPINN(?:s)?\b", r"物理信息神经网络"]),
        body_patterns=compile_patterns([r"\bphysics[- ]informed\b", r"\bPINN(?:s)?\b", r"物理信息神经网络"]),
    ),
    ThemeRule(
        folder="计算机算法",
        title_patterns=compile_patterns(
            [
                r"\bsimulated annealing\b",
                r"\bannealing algorithm\b",
                r"\bscheduling\b",
                r"\bsorting\b",
                r"\bautomatic differentiation\b",
                r"\bauto[- ]differentiation\b",
                r"\bLU decomposition\b",
                r"\bQR decomposition\b",
                r"\bincomplete LU\b",
                r"\bsparse direct solver(?:s)?\b",
                r"\blinear system(?:s)? of equations\b",
                r"\bsparse inverse covariance\b",
                r"\bmatrix decomposition\b",
                r"\blinear algebra library\b",
                r"\bfactorization\b",
                r"\boptimization algorithm\b",
                r"\bbayesian network(?:s)?\b",
                r"退火",
                r"排序",
                r"调度",
                r"自动微分",
                r"矩阵分解",
                r"矩阵求解",
                r"线性方程组",
                r"稀疏求解",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\bsimulated annealing\b",
                r"\bscheduling\b",
                r"\bautomatic differentiation\b",
                r"\bLU decomposition\b",
                r"\bQR decomposition\b",
                r"\bincomplete LU\b",
                r"\bsparse direct solver(?:s)?\b",
                r"\blinear algebra library\b",
                r"\bmatrix decomposition\b",
                r"\blinear system(?:s)?\b",
                r"\boptimization\b",
                r"退火",
                r"调度",
                r"自动微分",
                r"矩阵分解",
                r"线性方程组",
            ]
        ),
        body_patterns=compile_patterns(
            [
                r"\bsimulated annealing\b",
                r"\bscheduling\b",
                r"\bautomatic differentiation\b",
                r"\bLU decomposition\b",
                r"\bQR decomposition\b",
                r"\bincomplete LU\b",
                r"\bsparse direct solver(?:s)?\b",
                r"\blinear algebra\b",
                r"\bmatrix decomposition\b",
                r"\blinear system(?:s)?\b",
                r"\bfactorization\b",
                r"矩阵分解",
                r"线性方程组",
            ]
        ),
    ),
    ThemeRule(
        folder="ML-机器学习与数据驱动",
        title_patterns=compile_patterns(
            [
                r"\bmachine learning\b",
                r"\bdeep learning\b",
                r"\bdata[- ]driven\b",
                r"\breinforcement learning\b",
                r"\bmeta-learning\b",
                r"\bneural network(?:s)?\b",
                r"\bPoreFlow-Net\b",
                r"机器学习",
                r"深度学习",
                r"神经网络",
                r"数据驱动",
            ]
        ),
        front_patterns=compile_patterns([r"\bmachine learning\b", r"\bdeep learning\b", r"\bdata[- ]driven\b", r"\bneural network(?:s)?\b", r"机器学习", r"深度学习"]),
        body_patterns=compile_patterns([r"\bmachine learning\b", r"\bdeep learning\b", r"\bneural network(?:s)?\b", r"机器学习", r"深度学习"]),
    ),
    ThemeRule(
        folder="多孔介质基础与通用数值方法",
        title_patterns=compile_patterns(
            [
                r"\bporous media\b",
                r"\bdigital rock\b",
                r"\bmulti[- ]phase flow\b",
                r"\bpermeability solver\b",
                r"\bnon[- ]darcy\b",
                r"\bflow in porous media\b",
                r"\bpermeable media\b",
                r"多孔介质",
                r"渗流",
                r"多相流",
            ]
        ),
        front_patterns=compile_patterns([r"\bporous media\b", r"\bpermeable media\b", r"多孔介质", r"渗流", r"多相流"]),
        body_patterns=compile_patterns([r"\bporous media\b", r"\bmulti[- ]phase flow\b", r"多孔介质", r"渗流"]),
    ),
    ThemeRule(
        folder="流固耦合与空化",
        title_patterns=compile_patterns(
            [
                r"\bfluid[- ]structure interaction\b",
                r"\bghost fluid method\b",
                r"\bcavitation\b",
                r"\bone-fluid model\b",
                r"\bcompressible gas-liquid simulation\b",
                r"\bclose-in explosion\b",
                r"\bhydro-elasto-plastic\b",
                r"\bgas-liquid simulation\b",
                r"流固耦合",
                r"空化",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\bfluid[- ]structure interaction\b",
                r"\bghost fluid method\b",
                r"\bcavitation\b",
                r"\bone-fluid model\b",
                r"\bgas-liquid simulation\b",
                r"流固耦合",
                r"空化",
            ]
        ),
        body_patterns=compile_patterns([r"\bfluid[- ]structure\b", r"\bghost fluid\b", r"\bcavitation\b", r"\bone-fluid\b", r"流固耦合", r"空化"]),
    ),
    ThemeRule(
        folder="CFD与数值方法参考",
        title_patterns=compile_patterns(
            [
                r"\bcomputational fluid dynamics\b",
                r"\bNavier[- ]Stokes\b",
                r"\bStokes flow\b",
                r"\bHagen[- ]Poiseuille\b",
                r"\bbackward[- ]facing step flow\b",
                r"\bpartial differential equation\b",
                r"\bLU decomposition\b",
                r"\bQR decomposition\b",
                r"\bdiscontinuous galerkin\b",
                r"\bfinite element\b",
                r"\bdimensionless\b",
                r"\bnondimensionali[sz]ation\b",
                r"\bCFD\b",
                r"无量纲",
                r"方程分类",
                r"分解",
                r"软件设计",
            ]
        ),
        front_patterns=compile_patterns(
            [
                r"\bcomputational fluid dynamics\b",
                r"\bNavier[- ]Stokes\b",
                r"\bStokes flow\b",
                r"\bbackward[- ]facing step flow\b",
                r"\bpartial differential equation\b",
                r"\bfinite element\b",
                r"\bdiscontinuous galerkin\b",
                r"\bLU decomposition\b",
                r"\bQR decomposition\b",
                r"\bCFD\b",
                r"无量纲",
                r"软件设计",
            ]
        ),
        body_patterns=compile_patterns([r"\bNavier[- ]Stokes\b", r"\bCFD\b", r"无量纲", r"\bpartial differential equation\b", r"\bfinite element\b", r"\bdiscontinuous galerkin\b", r"\bbackward[- ]facing step flow\b"]),
    ),
    ThemeRule(
        folder="腐蚀与材料",
        title_patterns=compile_patterns(
            [
                r"\bcorrosion\b",
                r"\balloy\b",
                r"\bsurface film\b",
                r"\bmaterial\b",
                r"\bmicrostructure\b",
                r"腐蚀",
                r"材料",
            ]
        ),
        front_patterns=compile_patterns([r"\bcorrosion\b", r"\balloy\b", r"\bsurface film\b", r"腐蚀", r"材料"]),
        body_patterns=compile_patterns([r"\bcorrosion\b", r"\balloy\b", r"\bsurface film\b", r"腐蚀"]),
    ),
    ThemeRule(
        folder="机器人与仿生运动",
        title_patterns=compile_patterns(
            [
                r"\bbipedal robot\b",
                r"\bswimmer\b",
                r"\bgait\b",
                r"\bflapping swimmer\b",
                r"\brobot\b",
                r"机器人",
                r"仿生",
            ]
        ),
        front_patterns=compile_patterns([r"\bbipedal\b", r"\bswimmer\b", r"\bgait\b", r"\brobot\b", r"机器人", r"仿生"]),
        body_patterns=compile_patterns([r"\bbipedal\b", r"\bswimmer\b", r"\bgait\b", r"\brobot\b", r"机器人", r"仿生"]),
    ),
]


MANUAL_OVERRIDES: dict[str, tuple[str, str, str, str]] = {
    "2020 风雷(PHengLEI)通用CFD软件设计.pdf": ("期刊论文", "CFD与数值方法参考", "", "manual override: first-page journal article on CFD software design"),
    "Classification of Partial Differential Equations.pdf": ("工程文档", "CFD与数值方法参考", "书章资料", "manual override: book chapter screenshot review"),
    "Hagen-Poiseuille equation推导.pdf": ("工程文档", "CFD与数值方法参考", "教学笔记", "manual override: handwritten derivation note"),
    "[important] Gostick - 2016 - OpenPNM A pore network modeling package.pdf": ("期刊论文", "PNM-孔隙网络模型", "", "manual override: OpenPNM paper is a journal article"),
    "Localized corrosion behavior and surface corrosion film microstructure of a commercial dual-phase LZ91 Mg alloy.pdf": ("期刊论文", "腐蚀与材料", "", "manual override: corrosion paper outside porous-media core topics"),
    "LU Decomposition.pdf": ("工程文档", "计算机算法", "教学笔记", "manual override: standalone linear algebra note"),
    "Mason - 1994 - Effect of contact angle on capillary desplecement curvatures in pore throats formed by spheres.pdf": ("期刊论文", "自发渗吸与毛细现象", "", "manual override: scanned journal paper on capillary displacement"),
    "Multiphase Flow in Permeable Media - A Pore-Scale -- Blunt, Martin J_ -- 1, 2016 -- Cambridge University Press (Virtual Publishing) -- 9781107093461 -- 55e36ddb801d0c65e94d6ace54224401 -- Anna’s Archive.pdf": ("工程文档", "多孔介质基础与通用数值方法", "书籍教材", "manual override: porous-media book"),
    "Riemann Solvers and Numerical Methods for Fluid Dynamics.pdf": ("工程文档", "CFD与数值方法参考", "书籍教材", "manual override: numerical-methods textbook"),
    "Stokes flow介绍.pdf": ("工程文档", "CFD与数值方法参考", "书章资料", "manual override: book chapter screenshot review"),
    "ViennaCL 1.5.2 manual.pdf": ("工程文档", "计算机算法", "技术文档", "manual override: linear-algebra library manual"),
    "Yanovsky - QR decomposition with Gram-Schmidt.pdf": ("工程文档", "计算机算法", "教学笔记", "manual override: standalone linear algebra note"),
    "Gostick-OpenPNM.pdf": ("期刊论文", "PNM-孔隙网络模型", "", "manual override: OpenPNM journal article duplicate naming"),
    "基于Sierpinski carpet模型的多孔介质迂曲度计算.pdf": ("期刊论文", "迂曲度与有效输运", "", "manual override: journal first page clearly identifies tortuosity study"),
    "基于孔喉腔模型研究孔隙结构对于多孔介质孔隙度指数的影响.pdf": ("期刊论文", "孔隙结构与数字岩心", "", "manual override: journal first page clearly identifies pore-structure study"),
    "基于孔隙网络模型的电池热管理系统跨尺度分析.pdf": ("期刊论文", "电化学多孔电极与浸润", "", "manual override: journal first page clearly identifies battery thermal-management study"),
    "孔隙网络模型在污油泥热解中的应用研究.pdf": ("综述论文", "PNM-孔隙网络模型", "", "manual override: review-style article on pore-network model applications"),
    "孔隙网络模型的可视化方法及应用.pdf": ("期刊论文", "PNM-孔隙网络模型", "", "manual override: journal article on pore-network visualization methods"),
    "无量纲化.pdf": ("工程文档", "CFD与数值方法参考", "教学笔记", "manual override: dimensionless-analysis note"),
}


SUBTOPIC_RULES = {
    ("PNM-孔隙网络模型", "期刊论文"): [
        ("软件与算法", compile_patterns([r"openpnm", r"algorithm", r"package", r"simulator", r"fully implicit", r"fluid meniscus", r"validation", r"validating"])),
        ("两相流与毛细", compile_patterns([r"two-phase", r"imbibition", r"drainage", r"capillary", r"drying", r"wetting"])),
        ("电池与燃料电池", compile_patterns([r"battery", r"electrode", r"fuel cell", r"gas diffusion"])),
        ("页岩与非常规储层", compile_patterns([r"shale", r"nanopore", r"tight"])),
        ("反应传输与吸附", compile_patterns([r"reactive transport", r"adsorption", r"dissolution"])),
        ("提取与表征", compile_patterns([r"extraction", r"tomograph", r"image", r"\bct\b", r"segmentation"])),
    ],
    ("电化学多孔电极与浸润", "期刊论文"): [
        ("电解液浸润与填充", compile_patterns([r"electrolyte", r"filling", r"wetting", r"infiltration", r"imbibition"])),
        ("制造与压实", compile_patterns([r"calendering", r"manufacturing", r"drying", r"carbon-binder", r"process"])),
        ("热管理与性能", compile_patterns([r"thermal", r"performance", r"fast charging", r"capacity", r"impedance"])),
        ("表征与成像", compile_patterns([r"tomography", r"ultrasonic", r"neutron", r"fib-sem", r"imaging"])),
    ],
    ("自发渗吸与毛细现象", "期刊论文"): [
        ("自发渗吸", compile_patterns([r"imbibition", r"spontaneous"])),
        ("毛细上升与润湿", compile_patterns([r"capillary rise", r"wetting", r"contact angle"])),
        ("毛细压力与滞后", compile_patterns([r"capillary pressure", r"hysteresis"])),
        ("干燥与排驱", compile_patterns([r"drying", r"drainage"])),
    ],
    ("ML-机器学习与数据驱动", "期刊论文"): [
        ("PINN与物理约束", compile_patterns([r"physics-informed", r"\bpinn"])),
        ("多孔介质性质预测", compile_patterns([r"permeability", r"tortuosity", r"porosity", r"reaction rates"])),
        ("湍流与流动建模", compile_patterns([r"turbulence", r"\bflow\b"])),
        ("电池与电极数据驱动", compile_patterns([r"battery", r"electrode", r"cathode"])),
    ],
    ("计算机算法", "期刊论文"): [
        ("优化与搜索", compile_patterns([r"annealing", r"optimization", r"bayesian", r"acceptability"])),
        ("排序与调度", compile_patterns([r"scheduling", r"sort"])),
        ("自动微分与梯度", compile_patterns([r"automatic differentiation", r"auto-differentiation", r"gradient"])),
        ("矩阵分解与线性求解", compile_patterns([r"lu decomposition", r"qr decomposition", r"factorization", r"linear system", r"solver"])),
        ("稀疏线性代数与并行算法", compile_patterns([r"sparse", r"parallel", r"viennacl", r"linear algebra library"])),
    ],
    ("孔隙结构与数字岩心", "期刊论文"): [
        ("层析与三维表征", compile_patterns([r"tomograph", r"\bct\b", r"fib-sem", r"image"])),
        ("表征与网络提取", compile_patterns([r"extraction", r"network", r"segmentation", r"voxel", r"characterization", r"visual"])),
        ("生成与重建", compile_patterns([r"generation", r"reconstruction", r"qsgs", r"random", r"gaussian random field", r"sierpinski"])),
        ("分割与体素化", compile_patterns([r"segmentation", r"voxel", r"meshing"])),
        ("结构分析与可视化", compile_patterns([r"visual", r"characterization", r"microstructure"])),
    ],
    ("多孔介质基础与通用数值方法", "期刊论文"): [
        ("多相流与渗流基础", compile_patterns([r"porous media", r"multiphase", r"\bflow\b"])),
        ("数值方法与求解器", compile_patterns([r"finite element", r"discontinuous galerkin", r"solver", r"navier-stokes", r"stokes"])),
        ("流固耦合与界面", compile_patterns([r"fluid-structure", r"ghost fluid", r"cavitation", r"eulerian-lagrangian"])),
        ("高性能计算", compile_patterns([r"gpu", r"parallel", r"multi-gpu"])),
    ],
    ("LBM-格子玻尔兹曼", "期刊论文"): [
        ("方法与理论", compile_patterns([r"lattice boltzmann method", r"model for"])),
        ("多相流与相变", compile_patterns([r"multiphase", r"phase transition", r"phase-change"])),
        ("多孔介质应用", compile_patterns([r"porous", r"shale", r"electrolyte", r"permeable media"])),
    ],
    ("流固耦合与空化", "期刊论文"): [
        ("流固耦合", compile_patterns([r"fluid-structure", r"ghost fluid", r"hydro-elasto-plastic"])),
        ("空化与气液界面", compile_patterns([r"cavitation", r"gas-liquid", r"one-fluid"])),
    ],
    ("机器人与仿生运动", "期刊论文"): [
        ("步态与机构优化", compile_patterns([r"gait", r"bipedal", r"optimization"])),
        ("游动与柔性推进", compile_patterns([r"swimmer", r"flapping", r"flexible fin"])),
    ],
    ("腐蚀与材料", "期刊论文"): [
        ("腐蚀机理与膜层", compile_patterns([r"corrosion", r"surface film", r"alloy"])),
    ],
}

SUBTOPIC_FALLBACKS = {
    ("PNM-孔隙网络模型", "期刊论文"): "其他PNM研究",
    ("电化学多孔电极与浸润", "期刊论文"): "其他电化学研究",
    ("自发渗吸与毛细现象", "期刊论文"): "其他毛细研究",
    ("ML-机器学习与数据驱动", "期刊论文"): "其他数据驱动",
    ("计算机算法", "期刊论文"): "其他算法研究",
    ("孔隙结构与数字岩心", "期刊论文"): "其他孔隙结构研究",
    ("多孔介质基础与通用数值方法", "期刊论文"): "其他基础研究",
    ("LBM-格子玻尔兹曼", "期刊论文"): "其他LBM研究",
    ("流固耦合与空化", "期刊论文"): "其他流固耦合研究",
    ("机器人与仿生运动", "期刊论文"): "其他机器人研究",
    ("腐蚀与材料", "期刊论文"): "其他腐蚀材料研究",
}

ALWAYS_ASSIGN_SUBTOPIC_KEYS = {
    ("孔隙结构与数字岩心", "期刊论文"),
}


def extract_text_with_optional_ocr(pdf_path: Path) -> ExtractedPaper:
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # pragma: no cover - depends on local files
        return ExtractedPaper("", "", 0, "", "", "", [], [], 0, False, str(exc), "read-error")

    metadata = doc.metadata or {}
    native_front_parts = []
    native_full_parts = []
    first_page_texts: list[str] = []
    for index, page in enumerate(doc):
        text = page.get_text("text") or ""
        if index < FIRST_PAGES:
            native_front_parts.append(text)
            first_page_texts.append(text)
        native_full_parts.append(text)

    start_index = select_content_start_index(first_page_texts)
    content_front_parts = native_front_parts[start_index:]
    native_front_text = normalize_text(" ".join(content_front_parts or native_front_parts))
    native_full_text = normalize_text(" ".join(native_full_parts))
    front_text = native_front_text
    full_text = native_full_text
    ocr_used = False
    ocr_error = ""
    source_text_mode = "native"

    if OCR_AVAILABLE and (len(native_front_text) < LOW_FRONT_THRESHOLD or len(native_full_text) < LOW_TEXT_THRESHOLD):
        ocr_parts = []
        try:
            for index in range(min(len(doc), OCR_PAGES)):
                page = doc[index]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_parts.append(pytesseract.image_to_string(image, lang=OCR_LANGUAGE))
            ocr_text = normalize_text(" ".join(ocr_parts))
            if len(ocr_text) > len(front_text):
                front_text = normalize_text(f"{front_text} {ocr_text}")
                if len(full_text) < LOW_TEXT_THRESHOLD:
                    full_text = front_text
                ocr_used = True
                source_text_mode = "native+ocr"
        except Exception as exc:  # pragma: no cover - depends on external OCR runtime
            ocr_error = str(exc)

    abstract_text = extract_abstract_text(front_text)
    keyword_terms = extract_keyword_terms(front_text, (metadata.get("subject") or "").strip())
    topic_terms = extract_topic_terms((metadata.get("title") or "").strip(), abstract_text, keyword_terms, (metadata.get("subject") or "").strip())

    extracted = ExtractedPaper(
        metadata_title=(metadata.get("title") or "").strip(),
        metadata_subject=(metadata.get("subject") or "").strip(),
        page_count=len(doc),
        front_text=front_text,
        full_text=full_text,
        abstract_text=abstract_text,
        keyword_terms=keyword_terms,
        topic_terms=topic_terms,
        native_char_count=len(native_full_text),
        ocr_used=ocr_used,
        ocr_error=ocr_error,
        source_text_mode=source_text_mode,
    )
    doc.close()
    return extracted


def first_match(patterns: Iterable[re.Pattern[str]], text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def count_matches(patterns: Iterable[re.Pattern[str]], text: str) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


def normalize_theme_key(name: str) -> str:
    base = name.removeprefix(CANDIDATE_THEME_PREFIX)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "-", base.lower(), flags=re.UNICODE)
    key = re.sub(r"-{2,}", "-", key).strip("-")
    return key or "untitled"


def theme_alias_variants(name: str) -> list[str]:
    variants = {name, name.removeprefix(CANDIDATE_THEME_PREFIX), normalize_theme_key(name)}
    return [value for value in variants if value]


def score_pattern_hits(patterns: Iterable[re.Pattern[str]], text: str, weight: int) -> tuple[int, str]:
    hit = first_match(patterns, text)
    score = count_matches(patterns, text) * weight
    return score, hit


def extract_candidate_theme_seed(title_text: str, front_text: str, full_text: str) -> str:
    joined = normalize_text(f"{title_text} {front_text[:500]} {full_text[:500]}")

    quoted_phrase = re.search(r"[“\"]([^\"”]{4,40})[”\"]", title_text)
    if quoted_phrase:
        return sanitize_folder_name(quoted_phrase.group(1))

    phrase_patterns = [
        r"\b(?:study|studies|analysis|investigation|model(?:ling)?|method|methods|framework|theory|theories|hypothesis|dynamics|transport|interaction|simulation|simulations|design)\s+of\s+([A-Za-z][A-Za-z0-9\- ]{3,60})",
        r"\b([A-Za-z][A-Za-z0-9\- ]{3,60})\s+(?:theory|theories|hypothesis|dynamics|transport|interaction|simulation|simulations)\b",
        r"\b(?:for|of)\s+([A-Za-z][A-Za-z0-9\- ]{3,60})\b",
    ]
    stop_tokens = {
        "study", "analysis", "model", "modeling", "modelling", "based", "using", "approach", "method",
        "methods", "paper", "review", "effect", "effects", "investigation", "simulation", "simulations",
        "porous", "media", "general", "experimental", "theoretical", "article", "journal",
    }
    for pattern in phrase_patterns:
        match = re.search(pattern, title_text, re.IGNORECASE)
        if not match:
            continue
        phrase = normalize_text(match.group(1))
        words = [token for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", phrase) if token.lower() not in stop_tokens]
        if words:
            return sanitize_folder_name("-".join(words[:3]))

    zh_candidates = re.findall(r"[\u4e00-\u9fff]{2,12}", joined)
    stop_zh = {"研究", "分析", "方法", "模型", "基于", "文献", "论文", "数值模拟", "应用", "实验", "理论", "计算"}
    for token in zh_candidates:
        if token not in stop_zh:
            return sanitize_folder_name(token)

    acronym_match = re.search(r"\b([A-Z]{2,8}(?:-[A-Z]{2,8})?)\b", title_text)
    if acronym_match:
        return sanitize_folder_name(acronym_match.group(1))

    tokens = re.findall(r"[A-Za-z][A-Za-z-]{2,}", joined)
    fallback_tokens = [token for token in tokens if token.lower() not in stop_tokens]
    if fallback_tokens:
        return sanitize_folder_name("-".join(fallback_tokens[:3]))

    return "未命名"


def suggest_candidate_theme(title_text: str, front_text: str, full_text: str) -> tuple[str, str]:
    seed = extract_candidate_theme_seed(title_text, front_text, full_text)
    display_name = sanitize_folder_name(f"{CANDIDATE_THEME_PREFIX}{seed}")
    return display_name, normalize_theme_key(display_name)


def choose_confidence(score: int) -> str:
    if score >= 10:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def has_research_article_structure(front_text: str, full_text: str, page_count: int) -> bool:
    lower_front = front_text.lower()
    full_slice = full_text[:12000]
    score = 0
    if "abstract" in lower_front or "摘要" in front_text:
        score += 1
    if "introduction" in lower_front or "引言" in front_text:
        score += 1
    if re.search(r"\breferences\b|参考文献", full_slice, re.IGNORECASE):
        score += 1
    if re.search(r"\b(methods?|materials and methods|results?|conclusions?)\b", full_slice, re.IGNORECASE):
        score += 1
    return page_count >= 4 and score >= 2


def is_probable_book_material(type_zone: str, short_front: str, page_count: int, journal_score: int, article_front_hit: re.Match[str] | None) -> tuple[str, str] | None:
    chapter_hit = first_match(TYPE_PATTERNS["book_chapter"], short_front)
    if chapter_hit and journal_score == 0 and not article_front_hit:
        return "书章资料", chapter_hit

    book_hit = first_match(TYPE_PATTERNS["book"], type_zone)
    support_hit = first_match(TYPE_PATTERNS["book_support"], type_zone)
    if book_hit and (book_hit.lower() == "isbn" or support_hit or page_count >= 20):
        return "书籍教材", book_hit

    return None


def sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:60] or "新主题-未命名"


def suggest_new_theme(title_text: str) -> str:
    acronym_match = re.search(r"\b([A-Z]{2,8}(?:-[A-Z]{2,8})?)\b", title_text)
    if acronym_match:
        return sanitize_folder_name(f"新主题-{acronym_match.group(1)}")

    zh = re.findall(r"[\u4e00-\u9fff]{2,12}", title_text)
    stop_zh = {"研究", "分析", "方法", "模型", "基于", "文献", "论文", "数值模拟", "应用"}
    for token in zh:
        if token not in stop_zh:
            return sanitize_folder_name(f"新主题-{token}")

    tokens = re.findall(r"[A-Za-z][A-Za-z-]{2,}", title_text)
    stop_en = {
        "study", "analysis", "model", "modeling", "modelling", "based", "using",
        "approach", "method", "methods", "paper", "review", "effect", "effects",
        "investigation", "simulation", "simulations", "porous", "media",
        "more", "general", "experimental", "theoretical",
    }
    keywords = [token for token in tokens if token.lower() not in stop_en]
    if keywords:
        seed = "-".join(keywords[:2])
        return sanitize_folder_name(f"新主题-{seed}")

    return "新主题-未命名"


def detect_doc_type(title_text: str, front_text: str, full_text: str, page_count: int) -> tuple[str, str, str]:
    type_zone = f"{title_text} {front_text[:1800]}"
    short_front = front_text[:900]
    review_zone = f"{title_text} {front_text[:300]}"
    lower_front = front_text.lower()

    thesis_hit = first_match(TYPE_PATTERNS["thesis"], type_zone)
    if thesis_hit:
        return "学位论文", "", thesis_hit

    explicit_book_hit = first_match(TYPE_PATTERNS["book_explicit"], type_zone)
    if explicit_book_hit:
        return "工程文档", "书籍教材", explicit_book_hit

    review_hit = first_match(TYPE_PATTERNS["review"], review_zone)
    if review_hit:
        return "综述论文", "", review_hit

    conference_hit = first_match(TYPE_PATTERNS["conference"], type_zone)
    location_hit = re.search(
        r"\bheld in\s+[A-Z][A-Za-z.-]+(?:[ -][A-Z][A-Za-z.-]+)*(?:,\s*[A-Z][A-Za-z.-]+(?:[ -][A-Z][A-Za-z.-]+)*)?",
        type_zone,
        re.IGNORECASE,
    )
    if conference_hit or location_hit:
        return "会议论文", "", conference_hit or location_hit.group(0)

    article_front_hit = re.search(
        r"\b(open access|article open|research article|original article|article)\b",
        review_zone,
        re.IGNORECASE,
    )
    if article_front_hit and (page_count >= 4 or "abstract" in lower_front or "introduction" in lower_front or "摘要" in front_text):
        return "期刊论文", "", article_front_hit.group(0)

    journal_score = count_matches(TYPE_PATTERNS["journal_en"], type_zone) + count_matches(TYPE_PATTERNS["journal_cn"], type_zone)
    if journal_score >= 2:
        evidence = first_match(TYPE_PATTERNS["journal_en"], type_zone) or first_match(TYPE_PATTERNS["journal_cn"], type_zone)
        return "期刊论文", "", evidence or "journal markers"

    if count_matches(TYPE_PATTERNS["journal_cn"], type_zone) >= 1 and ("摘要" in type_zone or "Abstract" in type_zone):
        return "期刊论文", "", "journal markers"

    if has_research_article_structure(front_text, full_text, page_count):
        return "期刊论文", "", "paper structure markers"

    if re.search(r"\b(package|open-source application|software package|library)\b", title_text, re.IGNORECASE) and (
        re.search(r"a\s*b\s*s\s*t\s*r\s*a\s*c\s*t", full_text[:8000], re.IGNORECASE)
        or "introduction" in lower_front
    ):
        return "期刊论文", "", "software-paper structure"

    if page_count >= 5 and len(full_text) >= 3000 and "introduction" in lower_front:
        return "期刊论文", "", "research-article structure"

    preprint_hit = first_match(TYPE_PATTERNS["preprint"], type_zone)
    if preprint_hit:
        return "工程文档", "预印本", preprint_hit

    engineering_hit = first_match(TYPE_PATTERNS["engineering_doc"], type_zone)
    if engineering_hit:
        return "工程文档", "技术文档", engineering_hit

    lecture_hit = first_match(TYPE_PATTERNS["lecture_note"], type_zone)
    if lecture_hit:
        return "工程文档", "教学笔记", lecture_hit

    book_material = is_probable_book_material(type_zone, short_front, page_count, journal_score, article_front_hit)
    if book_material:
        subtype, evidence = book_material
        return "工程文档", subtype, evidence

    if page_count >= 4 and len(full_text) >= 1200 and len(title_text.split()) >= 5:
        return "期刊论文", "", "fallback: research-paper title/length heuristic"

    if len(front_text) < LOW_FRONT_THRESHOLD:
        return "工程文档", "低文本待确认", "front text too short and OCR unavailable or insufficient"

    return "工程文档", "未识别资料", "fallback: no strong type marker"


def detect_method_tags(title_text: str, front_text: str, full_text: str) -> list[str]:
    tags = []
    for name, patterns in METHOD_TAG_PATTERNS.items():
        score = 0
        if first_match(patterns, title_text):
            score += 3
        if first_match(patterns, front_text[:1800]):
            score += 2
        if first_match(patterns, full_text[:20000]):
            score += 1
        if score >= 3:
            tags.append(name)
    return tags


METHOD_TAG_TO_THEME = {
    "PNM": "PNM-孔隙网络模型",
    "LBM": "LBM-格子玻尔兹曼",
    "PINN": "PINN-物理信息神经网络",
    "ML": "ML-机器学习与数据驱动",
}

DEFAULT_THEME_ALIASES = {
    "电化学多孔电极与浸润": ["li-ion battery", "li ion battery", "pouch cell", "electrolyte filling", "wettability", "cathode", "anode", "graphite electrode", "li plating"],
    "孔隙结构与数字岩心": ["pore-scale imaging", "image-based modelling", "pore size distribution", "microstructure imaging", "random sphere pack", "voxelization", "meshing algorithm", "microstructure", "soil structure", "heterogeneous materials"],
    "流固耦合与空化": ["unsteady cavitating flow", "eulerian-lagrangian", "compressible multiphase flow"],
    "CFD与数值方法参考": ["backward-facing step flow", "discontinuous galerkin", "finite element", "shallow water", "explicit interface"],
    "多孔介质基础与通用数值方法": ["porous flow", "flow through a porous medium", "two-phase flow", "gas-liquid", "multiphase flow", "special core analysis", "multiphase materials", "transport properties", "effective physical properties"],
}


def reference_taxonomy_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / REFERENCE_TAXONOMY_FILENAME


def load_reference_taxonomy() -> dict[str, dict]:
    path = reference_taxonomy_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("themes", {}) if isinstance(data, dict) else {}


def build_theme_signal_terms(theme_name: str, runtime: ThemeRuntime) -> list[str]:
    ref = runtime.reference_taxonomy.get(theme_name, {})
    signals: list[str] = []
    signals.extend(theme_alias_variants(theme_name))
    signals.extend(runtime.theme_aliases.get(theme_name, []))
    signals.extend(ref.get("prior_terms", []))
    return unique_preserve_order(signals)


def count_phrase_hits(text: str, terms: list[str]) -> tuple[int, list[str]]:
    lowered = normalize_match_text(text)
    hits = [term for term in terms if term and normalize_match_text(term) in lowered]
    return len(hits), hits


def keyword_match_score(keyword_terms: list[str], signal_terms: list[str], rule: ThemeRule) -> tuple[int, list[str]]:
    hits: list[str] = []
    score = 0
    for keyword in keyword_terms:
        keyword_lower = normalize_match_text(keyword)
        if any(normalize_match_text(signal) in keyword_lower or keyword_lower in normalize_match_text(signal) for signal in signal_terms if signal):
            hits.append(keyword)
            score += 6
            continue
        if first_match(rule.title_patterns + rule.front_patterns, keyword):
            hits.append(keyword)
            score += 5
    return min(20, score), hits


def topic_match_score(topic_terms: list[str], method_tags: list[str], signal_terms: list[str], rule: ThemeRule, theme_name: str, reference_terms: list[str]) -> tuple[int, list[str], list[str]]:
    hits: list[str] = []
    ref_hits: list[str] = []
    score = 0
    for tag in method_tags:
        if METHOD_TAG_TO_THEME.get(tag) == theme_name:
            hits.append(f"tag:{tag}")
            score += 7
    for term in topic_terms:
        term_lower = normalize_match_text(term)
        if any(normalize_match_text(signal) in term_lower or term_lower in normalize_match_text(signal) for signal in signal_terms if signal):
            hits.append(term)
            score += 2
        if any(normalize_match_text(ref) in term_lower or term_lower in normalize_match_text(ref) for ref in reference_terms if ref):
            ref_hits.append(term)
            score += 2
    if first_match(rule.body_patterns, " ".join(topic_terms)):
        score += 3
    return min(15, score), unique_preserve_order(hits), unique_preserve_order(ref_hits)


def calculate_signal_overlap(signal_terms: list[str], paper_terms: list[str]) -> float:
    if not signal_terms or not paper_terms:
        return 0.0
    paper_set = {normalize_match_text(term) for term in paper_terms if term}
    signal_set = {normalize_match_text(term) for term in signal_terms if term}
    matched = 0
    for paper in paper_set:
        if any(paper in signal or signal in paper for signal in signal_set):
            matched += 1
    return matched / max(1, len(paper_set))


def build_theme_score(
    rule: ThemeRule,
    title_text: str,
    abstract_text: str,
    keyword_terms: list[str],
    topic_terms: list[str],
    method_tags: list[str],
    runtime: ThemeRuntime,
) -> dict:
    signal_terms = build_theme_signal_terms(rule.folder, runtime)
    reference_info = runtime.reference_taxonomy.get(rule.folder, {})
    reference_terms = unique_preserve_order(reference_info.get("prior_terms", []))
    reference_labels = unique_preserve_order(reference_info.get("scopus_asjc", []) + reference_info.get("wos_research_areas", []))

    title_hits, title_aliases = count_phrase_hits(title_text, signal_terms)
    title_pattern_score, title_pattern_hit = score_pattern_hits(rule.title_patterns, title_text, 8)
    title_score = min(35, title_hits * 10 + title_pattern_score)

    abstract_hits, abstract_aliases = count_phrase_hits(abstract_text, signal_terms)
    abstract_pattern_score = count_matches(rule.front_patterns, abstract_text) * 6 + count_matches(rule.body_patterns, abstract_text) * 3
    abstract_score = min(30, abstract_hits * 8 + abstract_pattern_score)

    keyword_score, keyword_hits = keyword_match_score(keyword_terms, signal_terms, rule)
    topic_score, topic_hits, reference_hits = topic_match_score(topic_terms, method_tags, signal_terms, rule, rule.folder, reference_terms)
    total_score = min(100, title_score + abstract_score + keyword_score + topic_score)

    evidence_parts: list[str] = []
    if title_aliases or title_pattern_hit:
        evidence_parts.append(f"title:{title_aliases[:2] or [title_pattern_hit]}")
    if abstract_aliases:
        evidence_parts.append(f"abstract:{abstract_aliases[:2]}")
    if keyword_hits:
        evidence_parts.append(f"keywords:{keyword_hits[:3]}")
    if topic_hits:
        evidence_parts.append(f"topic:{topic_hits[:3]}")
    if reference_hits and reference_labels:
        evidence_parts.append(f"reference:{reference_labels[:2]}")

    overlap = calculate_signal_overlap(signal_terms, unique_preserve_order(keyword_terms + topic_terms))

    return {
        "theme": rule.folder,
        "title_score": title_score,
        "abstract_score": abstract_score,
        "keyword_score": keyword_score,
        "topic_score": topic_score,
        "theme_relevance_score": total_score,
        "reference_prior": "; ".join(reference_labels[:3]) if reference_hits and reference_labels else "",
        "theme_evidence": "; ".join(evidence_parts),
        "overlap": overlap,
    }


def resolve_candidate_theme(
    title_text: str,
    abstract_text: str,
    keyword_terms: list[str],
    runtime: ThemeRuntime,
    best_existing: dict,
    gate_reason: str,
) -> ThemeSelection:
    display_name, theme_key = suggest_candidate_theme(title_text, abstract_text, " ".join(keyword_terms))

    for source, target in runtime.theme_promotions.items():
        if normalize_theme_key(source) == theme_key:
            promoted = sanitize_folder_name(target)
            return ThemeSelection(
                primary_theme=promoted,
                confidence="medium",
                theme_evidence=f"candidate promotion: {display_name} -> {promoted}",
                theme_key=theme_key,
                theme_origin="promoted_candidate",
                theme_status="promoted",
                title_score=best_existing.get("title_score", 0),
                abstract_score=best_existing.get("abstract_score", 0),
                keyword_score=best_existing.get("keyword_score", 0),
                topic_score=best_existing.get("topic_score", 0),
                theme_relevance_score=best_existing.get("theme_relevance_score", 0),
                reference_prior=best_existing.get("reference_prior", ""),
                new_theme_gate=gate_reason,
            )

    registry_entry = runtime.registry_entries.get(theme_key)
    if registry_entry:
        promoted_to = registry_entry.get("promoted_to") or ""
        if registry_entry.get("status") == "promoted" and promoted_to:
            promoted = sanitize_folder_name(promoted_to)
            return ThemeSelection(
                primary_theme=promoted,
                confidence="medium",
                theme_evidence=f"registry promotion: {display_name} -> {promoted}",
                theme_key=theme_key,
                theme_origin="promoted_candidate",
                theme_status="promoted",
                title_score=best_existing.get("title_score", 0),
                abstract_score=best_existing.get("abstract_score", 0),
                keyword_score=best_existing.get("keyword_score", 0),
                topic_score=best_existing.get("topic_score", 0),
                theme_relevance_score=best_existing.get("theme_relevance_score", 0),
                reference_prior=best_existing.get("reference_prior", ""),
                new_theme_gate=gate_reason,
            )
        runtime.candidate_cache.setdefault(theme_key, registry_entry.get("display_name", display_name))
        return ThemeSelection(
            primary_theme=runtime.candidate_cache[theme_key],
            confidence="low",
            theme_evidence=f"registry candidate reuse: {runtime.candidate_cache[theme_key]}",
            theme_key=theme_key,
            theme_origin="candidate",
            theme_status="candidate",
            title_score=best_existing.get("title_score", 0),
            abstract_score=best_existing.get("abstract_score", 0),
            keyword_score=best_existing.get("keyword_score", 0),
            topic_score=best_existing.get("topic_score", 0),
            theme_relevance_score=best_existing.get("theme_relevance_score", 0),
            reference_prior=best_existing.get("reference_prior", ""),
            new_theme_gate=gate_reason,
        )

    cached = runtime.candidate_cache.setdefault(theme_key, display_name)
    return ThemeSelection(
        primary_theme=cached,
        confidence="low",
        theme_evidence=f"dynamic candidate: {cached}",
        theme_key=theme_key,
        theme_origin="candidate",
        theme_status="candidate",
        title_score=best_existing.get("title_score", 0),
        abstract_score=best_existing.get("abstract_score", 0),
        keyword_score=best_existing.get("keyword_score", 0),
        topic_score=best_existing.get("topic_score", 0),
        theme_relevance_score=best_existing.get("theme_relevance_score", 0),
        reference_prior=best_existing.get("reference_prior", ""),
        new_theme_gate=gate_reason,
    )


def select_anchor_theme(best: dict, status: str, gate_reason: str) -> ThemeSelection:
    return ThemeSelection(
        primary_theme=best["theme"],
        confidence=choose_confidence(best["theme_relevance_score"]),
        theme_evidence=best["theme_evidence"] or f"anchor theme: {best['theme']}",
        theme_key=normalize_theme_key(best["theme"]),
        theme_origin="anchor",
        theme_status=status,
        title_score=best["title_score"],
        abstract_score=best["abstract_score"],
        keyword_score=best["keyword_score"],
        topic_score=best["topic_score"],
        theme_relevance_score=best["theme_relevance_score"],
        reference_prior=best["reference_prior"],
        new_theme_gate=gate_reason,
    )


def fallback_anchor_theme(signal_text: str, theme_scores: list[dict]) -> dict | None:
    fallback_rules = [
        ("电化学多孔电极与浸润", r"\bbattery\b|\belectrolyte\b|锂离子电池|电解液|浸润|cathode|anode|graphite electrode|li plating"),
        ("孔隙结构与数字岩心", r"\bdigital rock\b|\bpore[- ]?size\b|\btomograph\b|数字岩心|孔隙结构|层析|断层扫描|random sphere pack|voxelization|meshing|microstructure|soil structure|heterogeneous materials"),
        ("PNM-孔隙网络模型", r"\bpore[- ]network\b|\bopenpnm\b|孔隙网络模型|pore-scale numerical simulator|special core analysis"),
        ("LBM-格子玻尔兹曼", r"\blattice boltzmann\b|\blbm\b|格子玻尔兹曼"),
        ("ML-机器学习与数据驱动", r"\bmachine learning\b|\bdeep learning\b|机器学习|深度学习|神经网络"),
        ("计算机算法", r"\bsparse\b|\blinear algebra\b|\bmatrix\b|\bsolver\b|矩阵|线性方程组|自动微分|调度"),
        ("CFD与数值方法参考", r"\bcfd\b|\bnavier[- ]stokes\b|\bfinite element\b|\bgalerkin\b|有限元|无量纲|shallow water|explicit interface"),
        ("自发渗吸与毛细现象", r"\bcapillary\b|\bimbibition\b|毛细|渗吸|芯吸|润湿"),
        ("多孔介质基础与通用数值方法", r"\bporous media\b|多孔介质|渗流|permeability|porosity|two-phase flow|multiphase flow|gas-liquid|multiphase materials|transport properties|effective physical properties"),
        ("流固耦合与空化", r"\bcavitation\b|\bfluid[- ]structure\b|空化|流固耦合"),
        ("页岩与纳米孔流动", r"\bshale\b|\bnanopore\b|页岩|纳米孔"),
        ("反应传输与吸附", r"\breactive transport\b|\badsorption\b|反应传输|吸附"),
        ("机器人与仿生运动", r"\brobot\b|\bgait\b|\bswimmer\b|机器人|步态"),
        ("腐蚀与材料", r"\bcorrosion\b|\balloy\b|腐蚀|合金"),
        ("迂曲度与有效输运", r"\btortuosity\b|迂曲度"),
    ]
    for theme_name, pattern in fallback_rules:
        if re.search(pattern, signal_text, re.IGNORECASE):
            for item in theme_scores:
                if item["theme"] == theme_name:
                    return item
    return None


def classify_theme(
    title_text: str,
    abstract_text: str,
    keyword_terms: list[str],
    topic_terms: list[str],
    method_tags: list[str],
    runtime: ThemeRuntime,
) -> ThemeSelection:
    theme_scores = [
        build_theme_score(rule, title_text, abstract_text, keyword_terms, topic_terms, method_tags, runtime)
        for rule in THEME_RULES
    ]
    theme_scores.sort(key=lambda item: item["theme_relevance_score"], reverse=True)
    best = theme_scores[0]
    second = theme_scores[1] if len(theme_scores) > 1 else {"theme_relevance_score": 0}
    gap = best["theme_relevance_score"] - second["theme_relevance_score"]

    if best["theme_relevance_score"] >= DIRECT_ASSIGN_THRESHOLD:
        return select_anchor_theme(best, "anchor", f"blocked:new-theme top_score={best['theme_relevance_score']}")

    if best["theme_relevance_score"] >= AMBIGUOUS_ASSIGN_THRESHOLD and gap >= 8:
        return select_anchor_theme(best, "anchor", f"blocked:new-theme score_gap={gap}")

    if LOW_CONFIDENCE_ASSIGN_THRESHOLD <= best["theme_relevance_score"] < AMBIGUOUS_ASSIGN_THRESHOLD:
        return select_anchor_theme(best, "review_low_confidence", f"blocked:new-theme low_confidence score={best['theme_relevance_score']}")

    if best["reference_prior"]:
        return select_anchor_theme(best, "reference_backfilled", f"blocked:new-theme reference_prior={best['reference_prior']}")

    signal_text = normalize_text(f"{title_text} {abstract_text} {' '.join(keyword_terms)} {' '.join(topic_terms)}")
    fallback = fallback_anchor_theme(signal_text, theme_scores)
    if fallback is not None:
        return select_anchor_theme(fallback, "reference_backfilled", f"blocked:new-theme fallback_context={fallback['theme']}")

    max_overlap = max(item["overlap"] for item in theme_scores)
    if max_overlap >= SIGNAL_OVERLAP_THRESHOLD and best["theme_relevance_score"] > 0:
        return select_anchor_theme(best, "review_low_confidence", f"blocked:new-theme overlap={max_overlap:.2f}")

    return resolve_candidate_theme(
        title_text,
        abstract_text,
        keyword_terms,
        runtime,
        best_existing=best,
        gate_reason=f"allowed:new-theme score={best['theme_relevance_score']} overlap={max_overlap:.2f}",
    )


def build_evidence_snippet(title_text: str, front_text: str) -> str:
    snippet = title_text or front_text[:240]
    return normalize_text(snippet)[:240]


def build_target_relpath(classification: Classification) -> str:
    if classification.doc_type in VALID_DOC_TYPES:
        parts = [classification.primary_theme, classification.doc_type]
        if classification.subtopic:
            parts.append(classification.subtopic)
        parts.append(classification.source_name)
        return str(Path(*parts))
    subtype = classification.doc_subtype or "待复核"
    return str(Path("待复核") / subtype / classification.source_name)


def assign_subtopics(rows: list[Classification]) -> None:
    bucket_counts = Counter((row.primary_theme, row.doc_type) for row in rows if row.doc_type in VALID_DOC_TYPES)
    for row in rows:
        key = (row.primary_theme, row.doc_type)
        if key not in ALWAYS_ASSIGN_SUBTOPIC_KEYS and bucket_counts.get(key, 0) <= 10:
            row.subtopic = ""
            continue
        rules = SUBTOPIC_RULES.get(key)
        if not rules:
            row.subtopic = ""
            continue
        title = row.source_name.lower()
        assigned = ""
        for label, patterns in rules:
            if any(pattern.search(title) for pattern in patterns):
                assigned = label
                break
        row.subtopic = assigned or SUBTOPIC_FALLBACKS.get(key, "其他研究")


def output_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    output_dir = root / DEFAULT_OUTPUT_DIRNAME
    return (
        output_dir,
        output_dir / "classification_manifest.csv",
        output_dir / "classification_summary.md",
        output_dir / THEME_REGISTRY_FILENAME,
    )


def load_user_config(root: Path) -> UserConfig:
    config_path = root / CONFIG_FILENAME
    if not config_path.exists():
        return UserConfig(MANUAL_OVERRIDES.copy(), {key: list(values) for key, values in DEFAULT_THEME_ALIASES.items()}, {})

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return UserConfig(MANUAL_OVERRIDES.copy(), {key: list(values) for key, values in DEFAULT_THEME_ALIASES.items()}, {})

    overrides = MANUAL_OVERRIDES.copy()
    for name, payload in data.get("manual_overrides", {}).items():
        if not isinstance(payload, dict):
            continue
        overrides[name] = (
            payload.get("doc_type", "工程文档"),
            payload.get("primary_theme", "新主题-待定"),
            payload.get("doc_subtype", ""),
            payload.get("reason", "user override"),
        )
    aliases: dict[str, list[str]] = {key: list(values) for key, values in DEFAULT_THEME_ALIASES.items()}
    for theme_name, values in data.get("theme_aliases", {}).items():
        if isinstance(values, list):
            aliases.setdefault(theme_name, [])
            aliases[theme_name].extend(normalize_text(str(value)) for value in values if str(value).strip())

    promotions: dict[str, str] = {}
    for source_theme, target_theme in data.get("theme_promotions", {}).items():
        if str(source_theme).strip() and str(target_theme).strip():
            promotions[str(source_theme).strip()] = sanitize_folder_name(str(target_theme).strip())

    return UserConfig(overrides, aliases, promotions)


def load_theme_registry(registry_path: Path) -> dict[str, dict]:
    if not registry_path.exists():
        return {}
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = data.get("themes", [])
    registry: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        theme_key = normalize_theme_key(str(entry.get("theme_key") or entry.get("display_name") or ""))
        if not theme_key:
            continue
        registry[theme_key] = entry
    return registry


def build_theme_runtime(root: Path, user_config: UserConfig) -> ThemeRuntime:
    _, _, _, registry_path = output_paths(root)
    registry_entries = load_theme_registry(registry_path)
    return ThemeRuntime(
        theme_aliases=user_config.theme_aliases,
        theme_promotions=user_config.theme_promotions,
        registry_entries=registry_entries,
        reference_taxonomy=load_reference_taxonomy(),
    )


def write_theme_registry(rows: list[Classification], registry_path: Path, previous_registry: dict[str, dict]) -> None:
    candidate_rows = [row for row in rows if row.theme_origin in {"candidate", "promoted_candidate"}]
    grouped: dict[str, list[Classification]] = defaultdict(list)
    for row in candidate_rows:
        grouped[row.theme_key].append(row)

    themes_payload = []
    for theme_key, bucket in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        existing = previous_registry.get(theme_key, {})
        sample_theme = bucket[0]
        status = "promoted" if sample_theme.theme_status == "promoted" else "candidate"
        promoted_to = sample_theme.primary_theme if status == "promoted" else existing.get("promoted_to", "")
        themes_payload.append(
            {
                "theme_key": theme_key,
                "display_name": existing.get("display_name", sample_theme.primary_theme if status == "candidate" else f"{CANDIDATE_THEME_PREFIX}{theme_key}"),
                "aliases": sorted(set(existing.get("aliases", []) + [sample_theme.primary_theme, theme_key])),
                "doc_count": len(bucket),
                "status": status,
                "promoted_to": promoted_to,
                "example_files": [row.source_name for row in bucket[:5]],
                "promotion_suggested": status == "candidate" and len(bucket) >= PROMOTION_SUGGESTION_THRESHOLD,
            }
        )

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({"themes": themes_payload}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_pdf(pdf_path: Path, user_config: UserConfig, runtime: ThemeRuntime) -> Classification:
    extracted = extract_text_with_optional_ocr(pdf_path)
    title_text = normalize_text(f"{extracted.metadata_title} {pdf_path.stem}")
    front_text = extracted.front_text
    full_text = extracted.full_text
    abstract_text = extracted.abstract_text or extract_abstract_text(front_text)
    keyword_terms = extracted.keyword_terms
    topic_terms = unique_preserve_order(extracted.topic_terms)
    evidence_snippet = build_evidence_snippet(title_text, front_text)
    method_tags = detect_method_tags(title_text, front_text, full_text)
    topic_terms = unique_preserve_order(topic_terms + method_tags)

    override = user_config.manual_overrides.get(pdf_path.name)
    if override:
        doc_type, primary_theme, doc_subtype, reason = override
        classification = Classification(
            source_path=pdf_path,
            source_name=pdf_path.name,
            doc_type=doc_type,
            doc_subtype=doc_subtype,
            primary_theme=primary_theme,
            confidence="high",
            type_evidence=reason,
            theme_evidence=reason,
            evidence_snippet=evidence_snippet,
            method_tags=method_tags,
            ocr_used=extracted.ocr_used,
            ocr_error=extracted.ocr_error,
            source_text_mode="manual-override" if extracted.source_text_mode == "native" else f"manual-override+{extracted.source_text_mode}",
            theme_key=normalize_theme_key(primary_theme),
            theme_origin="manual_override",
            theme_status="promoted" if not primary_theme.startswith(CANDIDATE_THEME_PREFIX) else "candidate",
            abstract_excerpt=abstract_text[:240],
            keyword_terms=keyword_terms,
            topic_terms=topic_terms,
        )
        classification.target_relpath = build_target_relpath(classification)
        return classification

    doc_type, doc_subtype, type_evidence = detect_doc_type(title_text, front_text, full_text, extracted.page_count)
    theme_selection = classify_theme(title_text, abstract_text, keyword_terms, topic_terms, method_tags, runtime)

    classification = Classification(
        source_path=pdf_path,
        source_name=pdf_path.name,
        doc_type=doc_type,
        doc_subtype=doc_subtype,
        primary_theme=theme_selection.primary_theme,
        confidence=theme_selection.confidence,
        type_evidence=type_evidence,
        theme_evidence=theme_selection.theme_evidence,
        evidence_snippet=evidence_snippet,
        method_tags=method_tags,
        ocr_used=extracted.ocr_used,
        ocr_error=extracted.ocr_error,
        source_text_mode=extracted.source_text_mode,
        theme_key=theme_selection.theme_key,
        theme_origin=theme_selection.theme_origin,
        theme_status=theme_selection.theme_status,
        title_score=theme_selection.title_score,
        abstract_score=theme_selection.abstract_score,
        keyword_score=theme_selection.keyword_score,
        topic_score=theme_selection.topic_score,
        theme_relevance_score=theme_selection.theme_relevance_score,
        reference_prior=theme_selection.reference_prior,
        new_theme_gate=theme_selection.new_theme_gate,
        abstract_excerpt=abstract_text[:240],
        keyword_terms=keyword_terms,
        topic_terms=topic_terms,
    )
    classification.target_relpath = build_target_relpath(classification)
    return classification


def write_manifest(rows: list[Classification], manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source_path",
                "doc_type",
                "doc_subtype",
                "primary_theme",
                "theme_key",
                "theme_origin",
                "theme_status",
                "title_score",
                "abstract_score",
                "keyword_score",
                "topic_score",
                "theme_relevance_score",
                "reference_prior",
                "new_theme_gate",
                "subtopic",
                "method_tags",
                "confidence",
                "type_evidence",
                "theme_evidence",
                "ocr_used",
                "ocr_error",
                "source_text_mode",
                "abstract_excerpt",
                "keyword_terms",
                "topic_terms",
                "evidence_snippet",
                "target_relpath",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    str(row.source_path),
                    row.doc_type,
                    row.doc_subtype,
                    row.primary_theme,
                    row.theme_key,
                    row.theme_origin,
                    row.theme_status,
                    row.title_score,
                    row.abstract_score,
                    row.keyword_score,
                    row.topic_score,
                    row.theme_relevance_score,
                    row.reference_prior,
                    row.new_theme_gate,
                    row.subtopic,
                    json.dumps(row.method_tags, ensure_ascii=False),
                    row.confidence,
                    row.type_evidence,
                    row.theme_evidence,
                    row.ocr_used,
                    row.ocr_error,
                    row.source_text_mode,
                    row.abstract_excerpt,
                    json.dumps(row.keyword_terms, ensure_ascii=False),
                    json.dumps(row.topic_terms, ensure_ascii=False),
                    row.evidence_snippet,
                    row.target_relpath,
                ]
            )


def write_summary(rows: list[Classification], summary_path: Path) -> None:
    type_counts = Counter(row.doc_type for row in rows)
    theme_counts = Counter(row.primary_theme for row in rows)
    theme_origin_counts = Counter(row.theme_origin for row in rows)
    theme_status_counts = Counter(row.theme_status for row in rows)
    method_counts = Counter(tag for row in rows for tag in row.method_tags)
    by_theme_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    subtopic_counts: dict[str, Counter[str]] = defaultdict(Counter)
    engineering_subtypes = Counter(row.doc_subtype for row in rows if row.doc_type == "工程文档" and row.doc_subtype)
    candidate_theme_counts = Counter(row.primary_theme for row in rows if row.theme_origin == "candidate")
    promotion_suggestions = sorted(theme for theme, count in candidate_theme_counts.items() if count >= PROMOTION_SUGGESTION_THRESHOLD)
    score_buckets = {
        ">=45": sum(1 for row in rows if row.theme_relevance_score >= DIRECT_ASSIGN_THRESHOLD),
        "35-44": sum(1 for row in rows if AMBIGUOUS_ASSIGN_THRESHOLD <= row.theme_relevance_score < DIRECT_ASSIGN_THRESHOLD),
        "25-34": sum(1 for row in rows if LOW_CONFIDENCE_ASSIGN_THRESHOLD <= row.theme_relevance_score < AMBIGUOUS_ASSIGN_THRESHOLD),
        "<25": sum(1 for row in rows if row.theme_relevance_score < LOW_CONFIDENCE_ASSIGN_THRESHOLD),
    }
    for row in rows:
        by_theme_and_type[row.primary_theme][row.doc_type] += 1
        if row.subtopic:
            subtopic_counts[f"{row.primary_theme}/{row.doc_type}"][row.subtopic] += 1

    ocr_used_count = sum(1 for row in rows if row.ocr_used)
    generated_themes = sorted(theme for theme in theme_counts if theme.startswith("新主题-"))

    lines = [
        "# 文献分类汇总",
        "",
        f"- 总 PDF 数: {len(rows)}",
        f"- 期刊论文: {type_counts.get('期刊论文', 0)}",
        f"- 学位论文: {type_counts.get('学位论文', 0)}",
        f"- 综述论文: {type_counts.get('综述论文', 0)}",
        f"- 会议论文: {type_counts.get('会议论文', 0)}",
        f"- 工程文档: {type_counts.get('工程文档', 0)}",
        "",
        "## 主题来源",
        "",
        f"- anchor: {theme_origin_counts.get('anchor', 0)}",
        f"- candidate: {theme_origin_counts.get('candidate', 0)}",
        f"- promoted_candidate: {theme_origin_counts.get('promoted_candidate', 0)}",
        f"- manual_override: {theme_origin_counts.get('manual_override', 0)}",
        "",
        "## 主题状态",
        "",
        f"- anchor: {theme_status_counts.get('anchor', 0)}",
        f"- review_low_confidence: {theme_status_counts.get('review_low_confidence', 0)}",
        f"- reference_backfilled: {theme_status_counts.get('reference_backfilled', 0)}",
        f"- candidate: {theme_status_counts.get('candidate', 0)}",
        f"- promoted: {theme_status_counts.get('promoted', 0)}",
        "",
        "## 主题相关性分数",
        "",
        f"- >=45: {score_buckets['>=45']}",
        f"- 35-44: {score_buckets['35-44']}",
        f"- 25-34: {score_buckets['25-34']}",
        f"- <25: {score_buckets['<25']}",
        "",
        "## OCR 状态",
        "",
        f"- OCR 可用: {'是' if OCR_AVAILABLE else '否'}",
        f"- Tesseract 路径: {TESSERACT_CMD or '未发现'}",
        f"- OCR 实际使用文件数: {ocr_used_count}",
        "",
        "## 一级主题分布",
        "",
    ]

    for theme, count in theme_counts.most_common():
        detail = by_theme_and_type[theme]
        parts = [f"{kind} {detail[kind]}" for kind in DOC_TYPE_ORDER if detail[kind]]
        lines.append(f"- {theme}: {count} ({', '.join(parts)})")

    lines.extend(["", "## 三级细分目录", ""])
    if subtopic_counts:
        for bucket, counter in sorted(subtopic_counts.items()):
            parts = [f"{name} {count}" for name, count in counter.most_common()]
            lines.append(f"- {bucket}: {', '.join(parts)}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 方法标签分布", ""])
    for tag, count in method_counts.most_common():
        lines.append(f"- {tag}: {count}")

    lines.extend(["", "## 工程文档细分", ""])
    if engineering_subtypes:
        for subtype, count in engineering_subtypes.most_common():
            lines.append(f"- {subtype}: {count}")
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 自动生成新主题",
            "",
        ]
    )

    if generated_themes:
        for theme in generated_themes:
            lines.append(f"- {theme}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 候选主题提升建议", ""])
    if promotion_suggestions:
        for theme in promotion_suggestions:
            lines.append(f"- {theme}")
    else:
        lines.append("- 无")

    blocked_candidates = [row for row in rows if row.new_theme_gate.startswith("blocked:new-theme")]
    lines.extend(["", "## 新主题抑制统计", ""])
    lines.append(f"- 已抑制并回填到已有主题: {len(blocked_candidates)}")

    lines.extend(
        [
            "",
            "## 同义词归一规则",
            "",
            "- PNM: pore network, pore-network, pore network modeling, PNM, PNW",
            "- LBM: lattice Boltzmann, lattice-Boltzmann, LBM",
            "- PINN: physics-informed neural networks, PINNs",
            "- ML: machine learning, deep learning, neural network, data-driven",
        ]
    )

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_target_free(target_path: Path, source_path: Path) -> None:
    if target_path.exists() and target_path.resolve() != source_path.resolve():
        raise FileExistsError(f"目标路径已存在: {target_path}")


def detect_target_conflicts(rows: list[Classification]) -> list[str]:
    counts = Counter(row.target_relpath for row in rows)
    return sorted(path for path, count in counts.items() if count > 1)


def move_files(rows: list[Classification], root: Path) -> None:
    conflicts = detect_target_conflicts(rows)
    if conflicts:
        preview = "\n".join(conflicts[:20])
        raise FileExistsError(f"目标路径冲突:\n{preview}")

    for row in rows:
        target = root / row.target_relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_target_free(target, row.source_path)
        if target.resolve() == row.source_path.resolve():
            continue
        shutil.move(str(row.source_path), str(target))


def cleanup_empty_dirs(root: Path) -> None:
    directories = sorted([path for path in root.rglob("*") if path.is_dir()], key=lambda item: len(item.parts), reverse=True)
    for directory in directories:
        if directory == root or ".omx" in directory.parts or DEFAULT_OUTPUT_DIRNAME in directory.parts:
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError:
                continue


def collect_pdfs(root: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in root.rglob("*.pdf")
            if path.is_file() and ".omx" not in path.parts and DEFAULT_OUTPUT_DIRNAME not in path.parts
        ],
        key=lambda item: str(item).lower(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reclassify and reorganize PDFs by research content.")
    parser.add_argument("--root", default=".", help="Root folder containing the PDF library.")
    parser.add_argument("--execute", action="store_true", help="Move files after generating manifest and summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    _, manifest_path, summary_path, registry_path = output_paths(root)
    user_config = load_user_config(root)
    runtime = build_theme_runtime(root, user_config)
    pdfs = collect_pdfs(root)
    if not pdfs:
        print("未找到 PDF 文件。", file=sys.stderr)
        return 1

    rows = [classify_pdf(pdf_path, user_config, runtime) for pdf_path in pdfs]
    assign_subtopics(rows)
    for row in rows:
        row.target_relpath = build_target_relpath(row)
    write_manifest(rows, manifest_path)
    write_summary(rows, summary_path)
    write_theme_registry(rows, registry_path, runtime.registry_entries)

    if args.execute:
        move_files(rows, root)
        cleanup_empty_dirs(root)

    print(
        json.dumps(
            {
                "total": len(rows),
                "type_counts": Counter(row.doc_type for row in rows),
                "ocr_available": OCR_AVAILABLE,
                "ocr_used": sum(1 for row in rows if row.ocr_used),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
