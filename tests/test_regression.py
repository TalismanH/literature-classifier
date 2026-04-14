from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "classify_literature.py"
sys.modules.setdefault("fitz", types.SimpleNamespace())
pil_module = types.ModuleType("PIL")
pil_module.Image = object()
sys.modules.setdefault("PIL", pil_module)
sys.modules.setdefault("pytesseract", types.SimpleNamespace(pytesseract=types.SimpleNamespace()))
SPEC = importlib.util.spec_from_file_location("literature_classifier", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load classifier module from {MODULE_PATH}")
classifier = importlib.util.module_from_spec(SPEC)
sys.modules["literature_classifier"] = classifier
SPEC.loader.exec_module(classifier)


class LiteratureClassifierRegressionTests(unittest.TestCase):
    def build_runtime(
        self,
        aliases: dict[str, list[str]] | None = None,
        promotions: dict[str, str] | None = None,
        registry: dict | None = None,
        reference_taxonomy: dict | None = None,
    ):
        return classifier.ThemeRuntime(
            theme_aliases=aliases or {},
            theme_promotions=promotions or {},
            registry_entries=registry or {},
            reference_taxonomy=reference_taxonomy or {},
        )

    def test_other_material_type_removed(self) -> None:
        self.assertNotIn("其他资料", classifier.VALID_DOC_TYPES)

    def test_journal_markers_beat_publisher(self) -> None:
        doc_type, doc_subtype, evidence = classifier.detect_doc_type(
            "Simulation-Based and Data-Driven Techniques for Quantifying Battery Electrodes",
            "Publisher accepted article DOI: 10.1000/test Abstract Introduction Journal of Power Sources",
            "Abstract Introduction Results Conclusions References",
            8,
        )
        self.assertEqual(doc_type, "期刊论文")
        self.assertEqual(doc_subtype, "")
        self.assertNotEqual(evidence, "ISBN")

    def test_algorithm_theme_for_annealing(self) -> None:
        selection = classifier.classify_theme(
            "Enhanced simulated annealing techniques for multiprocessor scheduling",
            "Simulated annealing scheduling optimization framework",
            ["simulated annealing", "scheduling", "optimization"],
            ["simulated annealing", "scheduling", "optimization"],
            [],
            self.build_runtime(),
        )
        self.assertEqual(selection.primary_theme, "计算机算法")
        self.assertIn(selection.confidence, {"high", "medium", "low"})
        self.assertEqual(selection.theme_origin, "anchor")
        self.assertTrue(selection.theme_evidence)

    def test_fem_stays_out_of_pore_theme(self) -> None:
        selection = classifier.classify_theme(
            "A direct discontinuous Galerkin method for the compressible Navier-Stokes equations",
            "Finite element CFD Navier-Stokes solver",
            ["finite element", "navier-stokes", "cfd"],
            ["finite element", "navier-stokes", "cfd"],
            [],
            self.build_runtime(),
        )
        self.assertEqual(selection.primary_theme, "CFD与数值方法参考")

    def test_pore_theme_is_merged_and_gets_stable_subtopic(self) -> None:
        row = classifier.Classification(
            source_path=Path("dummy.pdf"),
            source_name="Micro-CT segmentation for digital rock pore-network extraction.pdf",
            doc_type="期刊论文",
            doc_subtype="",
            primary_theme="孔隙结构与数字岩心",
            confidence="high",
            type_evidence="test",
            theme_evidence="test",
            evidence_snippet="test",
        )
        classifier.assign_subtopics([row])
        self.assertEqual(row.primary_theme, "孔隙结构与数字岩心")
        self.assertTrue(row.subtopic)

    def test_unknown_non_paper_defaults_to_engineering_doc(self) -> None:
        doc_type, doc_subtype, _ = classifier.detect_doc_type(
            "Lecture 34 Transport in Porous Media",
            "Lecture notes by instructor handout",
            "Lecture notes by instructor handout",
            2,
        )
        self.assertEqual(doc_type, "工程文档")
        self.assertEqual(doc_subtype, "教学笔记")

    def test_corrosion_is_explicit_theme(self) -> None:
        selection = classifier.classify_theme(
            "Localized corrosion behavior and surface corrosion film microstructure of Mg alloy",
            "Corrosion alloy surface film microstructure",
            ["corrosion", "alloy", "surface film"],
            ["corrosion", "alloy", "surface film"],
            [],
            self.build_runtime(),
        )
        self.assertEqual(selection.primary_theme, "腐蚀与材料")

    def test_unknown_domain_becomes_candidate_theme(self) -> None:
        selection = classifier.classify_theme(
            "Effective viscosity hypothesis for generalized suspension transport",
            "Received revised accepted abstract introduction",
            ["effective viscosity hypothesis", "suspension transport"],
            ["effective viscosity hypothesis", "suspension transport"],
            [],
            self.build_runtime(),
        )
        self.assertTrue(selection.primary_theme.startswith("新主题-"))
        self.assertEqual(selection.theme_origin, "candidate")
        self.assertEqual(selection.theme_status, "candidate")

    def test_candidate_theme_can_be_promoted_from_config(self) -> None:
        selection = classifier.classify_theme(
            "Effective viscosity hypothesis for generalized suspension transport",
            "Received revised accepted abstract introduction",
            ["effective viscosity hypothesis", "suspension transport"],
            ["effective viscosity hypothesis", "suspension transport"],
            [],
            self.build_runtime(promotions={"新主题-effective-viscosity-hypothesis": "流变与有效黏度理论"}),
        )
        self.assertEqual(selection.primary_theme, "流变与有效黏度理论")
        self.assertEqual(selection.theme_origin, "promoted_candidate")
        self.assertEqual(selection.theme_status, "promoted")

    def test_candidate_theme_is_reused_from_registry(self) -> None:
        selection = classifier.classify_theme(
            "Effective viscosity hypothesis for generalized suspension transport",
            "Received revised accepted abstract introduction",
            ["effective viscosity hypothesis", "suspension transport"],
            ["effective viscosity hypothesis", "suspension transport"],
            [],
            self.build_runtime(
                registry={
                    "effective-viscosity-hypothesis": {
                        "theme_key": "effective-viscosity-hypothesis",
                        "display_name": "新主题-effective-viscosity-hypothesis",
                        "aliases": ["effective viscosity hypothesis"],
                        "status": "candidate",
                    }
                }
            ),
        )
        self.assertEqual(selection.primary_theme, "新主题-effective-viscosity-hypothesis")
        self.assertEqual(selection.theme_origin, "candidate")

    def test_extract_abstract_and_keywords(self) -> None:
        front_text = "Abstract This paper studies capillary pressure in porous media. Keywords capillary pressure; porous media; drainage Introduction ..."
        abstract_text = classifier.extract_abstract_text(front_text)
        keywords = classifier.extract_keyword_terms(front_text, "")
        self.assertIn("capillary pressure", abstract_text.lower())
        self.assertIn("capillary pressure", [item.lower() for item in keywords])

    def test_cover_page_is_skipped_when_content_starts_later(self) -> None:
        start_index = classifier.select_content_start_index(
            [
                "Downloaded from publisher cover page all rights reserved",
                "Title page author affiliations",
                "Abstract This paper studies multiphase flow in porous media. Keywords multiphase flow; porous media; microstructure",
                "Introduction ...",
            ]
        )
        self.assertEqual(start_index, 2)

    def test_reference_prior_blocks_new_theme(self) -> None:
        selection = classifier.classify_theme(
            "Shape Factor Correlations of Hydraulic Conductance in Noncircular Capillaries",
            "Hydraulic conductance in capillaries",
            ["hydraulic conductance", "capillaries", "two-phase creeping flow"],
            ["hydraulic conductance", "capillaries", "two-phase creeping flow"],
            [],
            self.build_runtime(
                reference_taxonomy={
                    "自发渗吸与毛细现象": {
                        "scopus_asjc": ["Fluid Flow and Transfer Processes"],
                        "wos_research_areas": ["Physics"],
                        "prior_terms": ["hydraulic conductance", "capillaries", "capillary pressure"],
                    }
                }
            ),
        )
        self.assertEqual(selection.primary_theme, "自发渗吸与毛细现象")
        self.assertIn(selection.theme_status, {"reference_backfilled", "review_low_confidence", "anchor"})

    def test_auth_springer_book_is_not_journal(self) -> None:
        doc_type, doc_subtype, evidence = classifier.detect_doc_type(
            "Salvatore Torquato (auth.) - Random Heterogeneous Materials_ Microstructure and Macroscopic Properties (2002, Springer)",
            "Salvatore Torquato (auth.) Random Heterogeneous Materials (2002, Springer)",
            "Salvatore Torquato (auth.) Random Heterogeneous Materials (2002, Springer)",
            320,
        )
        self.assertEqual(doc_type, "工程文档")
        self.assertEqual(doc_subtype, "书籍教材")
        self.assertTrue(evidence)

    def test_macroscopic_properties_paper_is_not_forced_to_book(self) -> None:
        doc_type, doc_subtype, evidence = classifier.detect_doc_type(
            "Predicting porosity, permeability, and tortuosity of porous media from images by deep learning",
            "Scientific Reports DOI: 10.1038/s41598-020-78415-x Abstract Introduction",
            "Scientific Reports DOI: 10.1038/s41598-020-78415-x Abstract Introduction Results Conclusions References",
            8,
        )
        self.assertEqual(doc_type, "期刊论文")
        self.assertEqual(doc_subtype, "")
        self.assertTrue(evidence)


if __name__ == "__main__":
    unittest.main()
