"""研究方向定义：分类、关键词与数据源映射。

顺序即推送顺序。主方向判定由 LLM 完成，这里提供粗筛与 prompt 上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Direction:
    key: str
    name: str
    order: int
    description: str
    arxiv_categories: tuple[str, ...] = ()
    biorxiv_categories: tuple[str, ...] = ()
    europepmc_terms: tuple[str, ...] = ()
    crossref_filters: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = field(default_factory=tuple)
    exclude_keywords: tuple[str, ...] = field(default_factory=tuple)


# 顺序：计算机 -> 理论物理 -> 天体物理 -> 量子化学 -> 计算生物学 -> 医学 -> 量子生物学
DIRECTIONS: tuple[Direction, ...] = (
    Direction(
        key="computer_science",
        name="计算机",
        order=1,
        description=(
            "Computer science, especially artificial intelligence, machine learning, "
            "computer vision, natural language processing, learning theory, software "
            "engineering, systems, security and databases."
        ),
        arxiv_categories=("cs.AI", "cs.CV", "cs.CL", "cs.LG", "cs.SE"),
        include_keywords=(
            "large language model", "llm", "transformer", "diffusion model",
            "reinforcement learning", "self-supervised", "multimodal", "agent",
            "benchmark", "dataset", "reasoning", "retrieval-augmented",
            "computer vision", "object detection", "segmentation", "code generation",
            "program synthesis", "software engineering", "neural network",
        ),
        exclude_keywords=("quantum biology",),
    ),
    Direction(
        key="theoretical_physics",
        name="理论物理",
        order=2,
        description=(
            "Theoretical physics: high-energy theory, particle phenomenology, "
            "condensed matter theory, gravitation and cosmology theory, quantum "
            "information theory, statistical mechanics."
        ),
        arxiv_categories=(
            "hep-th", "hep-ph", "gr-qc", "quant-ph",
            "cond-mat.stat-mech", "cond-mat.str-el", "cond-mat.soft",
        ),
        include_keywords=(
            "quantum field theory", "string theory", "holography", "ads/cft",
            "standard model", "dark matter", "neutrino", "supersymmetry",
            "condensed matter", "topological phase", "entanglement",
            "quantum information", "quantum computing", "statistical mechanics",
            "gravitational wave", "black hole",
        ),
        exclude_keywords=(),
    ),
    Direction(
        key="astrophysics",
        name="天体物理",
        order=3,
        description=(
            "Astrophysics and astronomy: observations and theory of stars, galaxies, "
            "exoplanets, cosmology, high-energy astrophysics, gravitational waves and "
            "astronomical instrumentation."
        ),
        arxiv_categories=(
            "astro-ph.CO", "astro-ph.GA", "astro-ph.SR", "astro-ph.EP",
            "astro-ph.HE", "astro-ph.IM",
        ),
        crossref_filters=(
            "issn:0004-637X",   # ApJ
            "issn:2041-8205",   # ApJL
            "issn:0004-6256",   # AJ
            "issn:0035-8711",   # MNRAS
            "issn:0004-6361",   # A&A
            "issn:1475-7516",   # JCAP
        ),
        include_keywords=(
            "galaxy", "exoplanet", "supernova", "black hole", "dark energy",
            "cosmic microwave background", "stellar", "gravitational wave",
            "astrochemistry", "interstellar medium", "pulsar", "quasar",
            "cosmology", "planet formation", "spectroscopic survey",
        ),
        exclude_keywords=(),
    ),
    Direction(
        key="quantum_chemistry",
        name="量子化学",
        order=4,
        description=(
            "Quantum chemistry and electronic structure theory: density functional "
            "theory, wavefunction methods, coupled cluster, molecular spectroscopy, "
            "reaction dynamics, excited states, quantum chemistry software and "
            "machine-learning potentials for chemistry."
        ),
        arxiv_categories=("physics.chem-ph",),
        crossref_filters=("prefix:10.26434",),  # chemRxiv
        include_keywords=(
            "quantum chemistry", "density functional", "dft", "coupled cluster",
            "electronic structure", "wavefunction", "basis set", "molecular dynamics",
            "potential energy surface", "excited state", "spectroscopy",
            "reaction dynamics", "ab initio", "machine learning potential",
            "charge transfer", "spin state",
        ),
        exclude_keywords=(),
    ),
    Direction(
        key="computational_biology",
        name="计算生物学",
        order=5,
        description=(
            "Computational biology and bioinformatics: sequence analysis, genomics, "
            "transcriptomics, single-cell analysis, structural bioinformatics, "
            "systems biology, phylogenetics, protein design, and machine learning "
            "for biological data."
        ),
        arxiv_categories=(
            "q-bio.BM", "q-bio.GN", "q-bio.MN", "q-bio.QM",
            "q-bio.SC", "q-bio.TO", "q-bio.PE", "q-bio.NC",
        ),
        biorxiv_categories=(
            "bioinformatics", "genomics", "systems biology", "synthetic biology",
        ),
        europepmc_terms=(
            "computational biology", "bioinformatics", "single-cell",
            "protein structure prediction", "systems biology", "genome assembly",
            "sequence alignment", "phylogenetic analysis",
        ),
        include_keywords=(
            "bioinformatics", "computational biology", "single-cell", "scrna-seq",
            "genome", "transcriptome", "proteome", "protein structure",
            "alphafold", "sequence alignment", "phylogenetic", "systems biology",
            "gene regulatory network", "variant calling", "metagenomics",
            "structural bioinformatics", "molecular docking",
        ),
        exclude_keywords=("clinical trial",),
    ),
    Direction(
        key="medicine",
        name="医学",
        order=6,
        description=(
            "Clinical medicine, epidemiology and public health: diagnosis, treatment, "
            "clinical trials, medical imaging, drug safety, health services, and "
            "population health research."
        ),
        biorxiv_categories=("medrxiv",),  # handled as medRxiv source
        europepmc_terms=(
            "clinical trial", "diagnosis", "treatment outcome", "epidemiology",
            "public health", "medical imaging", "drug safety", "cohort study",
            "randomized controlled trial", "prognosis",
        ),
        crossref_filters=(
            "issn:0028-4793",   # NEJM
            "issn:0140-6736",   # Lancet
            "issn:0098-7484",   # JAMA
            "issn:0959-8138",   # BMJ
        ),
        include_keywords=(
            "clinical trial", "randomized", "cohort", "meta-analysis",
            "diagnosis", "treatment", "patients", "hospital", "epidemiology",
            "public health", "medical imaging", "drug", "vaccine", "prognosis",
            "mortality", "screening",
        ),
        exclude_keywords=("single-cell",),
    ),
    Direction(
        key="quantum_biology",
        name="量子生物学",
        order=7,
        description=(
            "Quantum biology: quantum effects in biological systems, including "
            "photosynthetic energy transfer, magnetoreception, enzyme tunneling, "
            "spin chemistry, quantum coherence in biology, and quantum effects in "
            "biomolecules."
        ),
        arxiv_categories=("q-bio.BM", "q-bio.SC", "q-bio.QM", "quant-ph"),
        biorxiv_categories=("biophysics",),
        europepmc_terms=(
            "quantum biology", "photosynthetic energy transfer", "magnetoreception",
            "enzyme tunneling", "spin chemistry", "quantum coherence biology",
            "quantum effects biological",
        ),
        include_keywords=(
            "quantum biology", "photosynthesis", "photosynthetic energy transfer",
            "magnetoreception", "enzyme tunneling", "spin chemistry",
            "quantum coherence", "radical pair", "quantum effect", "biomolecule",
            "electron transfer protein", "cryptochrome",
        ),
        exclude_keywords=(),
    ),
)

DIRECTION_BY_KEY = {d.key: d for d in DIRECTIONS}
DIRECTION_NAMES = {d.key: d.name for d in DIRECTIONS}
