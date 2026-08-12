"""Build docs/livespec-tools.pdf — the current MCP tool surface, one page-flow
per tier, each tool with its signature and its purpose in one sentence.

Usage: uv run python scripts/build_tools_pdf.py [out.pdf]

The tool inventory lives in TOOLS below and must stay in sync with README.md
("Tools (45 total)") and the registrars under src/livespec_mcp/tools/.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

VERSION = "0.31.4"

INK = colors.HexColor("#16181d")
MUTED = colors.HexColor("#5c6370")
ACCENT = colors.HexColor("#1f5fa9")
RULE = colors.HexColor("#d8dce3")
CHIP_BG = colors.HexColor("#eef2f8")

# (section title, blurb, [(signature, purpose), ...])
TOOLS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "Indexado (1)",
        "Construye y refresca todo el estado. Incremental por hash de contenido "
        "(xxh3); respeta .gitignore (raíz y anidados, con negaciones) y "
        "[index] de .livespec.toml.",
        [
            (
                "index_project(force=False, watch=False, explorer=False)",
                "Recorre el workspace, parsea, persiste símbolos y aristas, "
                "reconstruye los chunks de búsqueda y auto-escanea las "
                "anotaciones @spec:. Es la única puerta de entrada al índice.",
            ),
        ],
    ),
    (
        "Búsqueda (1)",
        "FTS5 sobre chunks AST-aware. Los vectores densos se removieron en "
        "v0.29: keyword search es el único carril.",
        [
            (
                "search(query, scope='all'|'code'|'specs', limit=20)",
                "Recupera código o Specs que *hablan de* algo cuando no hay un "
                "nombre exacto que buscar; divide snake_case en tokens OR.",
            ),
        ],
    ),
    (
        "Code intelligence (14)",
        "Las preguntas que un agente hace de verdad sobre un repo ajeno. "
        "Todos aceptan limit / cursor / summary_only donde el payload puede "
        "crecer sin techo; los conteos siempre son exactos.",
        [
            (
                "find_symbol(query, kind, limit)",
                "Lookup de símbolo agnóstico al separador (punto, slash, "
                "doble dos-puntos): el primer salto desde un nombre a medias.",
            ),
            (
                "get_symbol_source(qname)",
                "Devuelve solo el cuerpo del símbolo — más liviano que traer "
                "el archivo o la metadata completa.",
            ),
            (
                "quick_orient(qname)",
                "Primer contacto canónico: metadata, lead del docstring, "
                "top-5 callers y callees por PageRank, Specs enlazadas y flag "
                "de entry point en una sola llamada.",
            ),
            (
                "who_calls(qname, max_depth=1)",
                "Cono hacia atrás. Agrega route_callers cuando el símbolo es "
                "un endpoint HTTP que un frontend consume por ruta "
                "(cross-repo vía group_db).",
            ),
            (
                "who_does_this_call(qname, max_depth=1)",
                "Cono hacia adelante. Agrega invokes_endpoints: las rutas de "
                "backend que este símbolo golpea vía fetch / axios / requests.",
            ),
            (
                "analyze_impact(target_type, target, max_depth)",
                "Radio de impacto de un símbolo, archivo o Spec — incluye "
                "cascada por el grafo Spec→Spec. Con max_depth=1 cubre el "
                "viejo 'find references'.",
            ),
            (
                "git_diff_impact(base_ref, head_ref, max_depth=5)",
                "Punto de entrada de revisión de PR: archivos cambiados → "
                "llamadores impactados → Specs afectadas → tests sugeridos.",
            ),
            (
                "get_project_overview(include_infrastructure=False)",
                "Los símbolos más centrales por PageRank, con el ruido de "
                "infraestructura filtrado por defecto.",
            ),
            (
                "find_dead_code(include_infrastructure=False, corroborate_with=None)",
                "Candidatos sin llamadores ni links de Spec. Reporta "
                "filtered_out por flag; corroborate_with descarta los que un "
                "segundo extractor todavía ve referenciados.",
            ),
            (
                "find_orphan_tests(max_depth=10, corroborate_with=None)",
                "Tests cuyo cono hacia adelante nunca alcanza un símbolo de "
                "producción — es decir, que probablemente no prueban nada.",
            ),
            (
                "find_legacy_flows(project=None, include_infra_routes=False)",
                "Flujos HTTP probablemente muertos: servidores sin cliente "
                "indexado y clientes sin servidor que matchee. Mejor con "
                "group_db. Grafo, no tráfico.",
            ),
            (
                "find_endpoints(framework=None)",
                "Entry points por framework: decoradores, routing por "
                "filesystem, bases de CBV de Django y routing call-style "
                "(Express, Hono). Devuelve http_method / http_path donde se "
                "puede.",
            ),
            (
                "grep_in_indexed_files(pattern, path_glob, kind, limit=50)",
                "Grep acotado a los archivos que están en el índice — evita "
                "node_modules y .venv sin configurar excludes a mano.",
            ),
            (
                "audit_coverage()",
                "Reporte de cobertura Spec: módulos sin Spec, cubiertos "
                "transitivamente, huérfanos o en lenguajes sin extractor; "
                "Specs sin implementación; y cobertura de tests derivada del "
                "cono de llamadas.",
            ),
        ],
    ),
    (
        "Spec agentic — consulta y bootstrap (5)",
        "La mitad de lectura de la trazabilidad: siempre visible, porque son "
        "preguntas, no ceremonia.",
        [
            (
                "list_specs(status, module, priority, kind, has_implementation)",
                "Superficie de descubrimiento: qué Specs existen y cuáles "
                "tienen (o no) implementación.",
            ),
            (
                "get_spec_implementation(spec_id)",
                "Responde '¿qué código implementa auth-user-login?' — con "
                "scenarios, símbolos enlazados por scenario y flag verified.",
            ),
            (
                "propose_specs_from_codebase(module_depth=2, ...)",
                "Descubrimiento heurístico sobre un repo sin Specs: agrupa "
                "por módulo y PageRank (o por comunidad detectada con "
                "community_graph) y propone candidatos con título humanizado.",
            ),
            (
                "bulk_link_spec_symbols(mappings)",
                "Enlaza N pares (spec_id, símbolo) en una transacción. "
                "Escape hatch para archivos donde la anotación en fuente no "
                "llega: configs, SQL, YAML. Idempotente.",
            ),
            (
                "import_specs_from_markdown(path, fmt='openspec')",
                "Crea o actualiza Specs desde markdown OpenSpec — un archivo "
                "o un árbol entero. Siempre visible para poder arrancar "
                "brownfield sin tocar env vars.",
            ),
        ],
    ),
    (
        "Interop OpenSpec (5)",
        "Round-trip completo con el formato de Fission-AI: livespec no "
        "reemplaza la autoría de specs, la enlaza al código.",
        [
            (
                "sync_openspec(openspec_dir=None)",
                "Importa el árbol OpenSpec entero de una: requirements "
                "canónicos de specs/ más cada change en changes/ y archive/.",
            ),
            (
                "export_openspec(out_dir='openspec', include_changes=True)",
                "El inverso: escribe la DB de vuelta como "
                "specs/<capability>/spec.md. Cierra el round-trip.",
            ),
            (
                "validate_openspec(strict=False)",
                "Espeja `openspec validate --strict`; el chequeo que carga "
                "peso es que todo requirement tenga al menos un scenario.",
            ),
            (
                "list_spec_changes(status=None)",
                "Lista las propuestas de cambio con su estado (proposed / archived).",
            ),
            (
                "get_spec_change(name)",
                "Inspecciona una propuesta: prosa de proposal / design / "
                "tasks más los deltas ADD / MODIFY / REMOVE / RENAME.",
            ),
        ],
    ),
    (
        "Higiene y descubribilidad (2)",
        "Dos tools que existen porque lo invisible es peor que lo ausente.",
        [
            (
                "scan_annotation_verbs(sample_per_group=10)",
                "Saca a la luz comentarios con forma de anotación (@word:) "
                "que el matcher NO va a enlazar, separando verbo no "
                "reconocido (con did_you_mean) de payload no linkeable.",
            ),
            (
                "get_cross_repo_guide()",
                "El how-to de polyrepo (group_db, Specs xrepo-*, Flow "
                "Explorer, trampas) más un snapshot vivo del grupo, expuesto "
                "como tool porque algunos hosts cachean un resources/list "
                "viejo.",
            ),
        ],
    ),
    (
        "Plugin livespec-spec — mutación de Specs (12)",
        "Lo que ejecuta un operador, no lo que pregunta un agente. Visible "
        "cuando el workspace ya tiene filas spec, o con LIVESPEC_PLUGINS=spec.",
        [
            ("create_spec(title, ...)", "Crea una Spec nueva en el store."),
            (
                "update_spec(spec_id, ...)",
                "Modifica campos de una Spec existente (estado, prioridad, kind, módulo, texto).",
            ),
            (
                "delete_spec(spec_id)",
                "Borra la Spec y, en cascada, sus links a símbolos.",
            ),
            (
                "link_spec_symbol(spec_id, symbol_qname, relation, ...)",
                "Enlaza o desenlaza un par Spec↔símbolo, con relación, confianza y origen.",
            ),
            (
                "link_scenario_symbol(spec_id, scenario_name, symbol_qname, ...)",
                "Trazabilidad a nivel scenario: ata código o tests a un "
                "`#### Scenario:` puntual, más fino que el requirement entero.",
            ),
            (
                "link_spec_dependency(parent, child, kind='requires')",
                "Arista del grafo Spec→Spec (requires / extends / conflicts); "
                "los ciclos se rechazan en el insert.",
            ),
            (
                "unlink_spec_dependency(parent, child)",
                "Quita una arista del grafo Spec→Spec.",
            ),
            (
                "get_spec_dependency_graph(spec_id, ...)",
                "Camina el grafo Spec→Spec: qué depende de esto, transitivamente.",
            ),
            (
                "scan_spec_annotations()",
                "Matcher de dos niveles sobre las anotaciones @spec: del "
                "código; corre solo tras cada index_project. Los ids que no "
                "existen vuelven como unknown_annotation_ids en vez de "
                "perderse.",
            ),
            (
                "scan_docstrings_for_spec_hints()",
                "Propone candidatos a Spec desde los docstrings existentes "
                "(primera oración, verbo inicial) y devuelve el histograma de "
                "verbos dominantes.",
            ),
            (
                "apply_spec_change(name, dry_run=False)",
                "Pliega los deltas de un change sobre el set canónico "
                "(upsert, deprecate, y RENAMED mueve la trazabilidad al "
                "nombre nuevo). Con dry_run devuelve plan y warnings sin "
                "mutar.",
            ),
            (
                "archive_spec_change(name)",
                "Marca el change como archivado, cerrando su ciclo de vida.",
            ),
        ],
    ),
    (
        "Plugin livespec-docs — docs y Explorer (5)",
        "Ceremonia de tier humano. Visible con filas doc, con un bundle "
        ".mcp-docs/explorer/ en disco, o con LIVESPEC_PLUGINS=docs.",
        [
            (
                "generate_docs(target_type, identifier, content=None, ...)",
                "Genera documentación en tres modos (caller_supplied, "
                "sampling, needs_caller_content): funciona tanto en Claude "
                "Code como en hosts con sampling.",
            ),
            (
                "list_docs(target_type, only_stale=False)",
                "Lista docs o, con only_stale, los que driftearon — el drift "
                "dispara por body_hash O signature_hash.",
            ),
            (
                "export_documentation(format, out_subdir)",
                "Vuelca la documentación a markdown o JSON.",
            ),
            (
                "export_explorer(base=None, head=None, generated_at=None)",
                "Escribe el bundle estático del Spec Explorer "
                "(.mcp-docs/explorer/): vista tipo Swagger por Spec, Try-it "
                "HTTP y playground MCP al servirlo.",
            ),
            (
                "export_flow_explorer(...)",
                "Bundle companion del Flow Explorer: los flujos HTTP "
                "cross-repo dibujados de cliente a handler.",
            ),
        ],
    ),
]

EXTRAS = [
    (
        "Resources",
        "project://overview · project://index/status · project://specs · "
        "project://specs/{spec_id} · project://files/{path*} · "
        "project://symbols/{qname*} · project://cross-repo · "
        "doc://symbol/{qname*} · doc://spec/{spec_id} · code://symbol/{qname*}",
    ),
    (
        "Prompts (slash commands)",
        "agent_playbook (la guía principal) · openspec_workflow · "
        "onboard_project · analyze_change_impact · audit_spec_coverage · "
        "extract_specs_from_module · document_undocumented_symbols · "
        "refresh_stale_docs · explain_symbol",
    ),
    (
        "Contratos que no se rompen",
        "workspace absoluto obligatorio en toda llamada (sin fallback a cwd ni "
        "env) · errores solo vía mcp_error(): {error, isError, did_you_mean?, "
        "hint?} · paginación (limit / cursor / summary_only) en agregadores, "
        "con conteos siempre exactos · migraciones append-only · el resolver "
        "de refs es INSERT OR IGNORE, nunca DELETE.",
    ),
]


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=26,
            leading=30,
            textColor=INK,
            spaceAfter=4,
            alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=MUTED,
            spaceAfter=14,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13.5,
            leading=17,
            textColor=ACCENT,
            spaceBefore=14,
            spaceAfter=3,
        ),
        "blurb": ParagraphStyle(
            "blurb",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=12.5,
            textColor=MUTED,
            spaceAfter=7,
        ),
        "sig": ParagraphStyle(
            "sig",
            parent=base["Normal"],
            fontName="Courier-Bold",
            fontSize=8.4,
            leading=11,
            textColor=INK,
        ),
        "purpose": ParagraphStyle(
            "purpose",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.2,
            leading=13,
            textColor=INK,
        ),
        "extra": ParagraphStyle(
            "extra",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.8,
            leading=12.5,
            textColor=INK,
            spaceAfter=6,
        ),
    }


def _tool_table(rows: list[tuple[str, str]], st: dict) -> Table:
    data = [[Paragraph(sig, st["sig"]), Paragraph(purpose, st["purpose"])] for sig, purpose in rows]
    table = Table(data, colWidths=[66 * mm, 100 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (0, -1), 5),
                ("RIGHTPADDING", (0, 0), (0, -1), 7),
                ("BACKGROUND", (0, 0), (0, -1), CHIP_BG),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ]
        )
    )
    return table


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm, f"livespec v{VERSION} — superficie de tools")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, f"{doc.page}")
    canvas.restoreState()


def build(out_path: Path) -> Path:
    st = _styles()
    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A4,
        title=f"livespec v{VERSION} — tools",
        author="livespec",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
    )
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="body",
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    story: list = [
        Paragraph("livespec", st["title"]),
        Paragraph(
            f"Superficie completa de tools MCP — v{VERSION}. 45 tools: 28 en el "
            "núcleo siempre visible, 12 en el plugin <b>livespec-spec</b> "
            "(mutación de Specs) y 5 en el plugin <b>livespec-docs</b> "
            "(documentación y Explorer). Los plugins registran al boot pero "
            "<b>PluginVisibilityMiddleware</b> los oculta de <i>tools/list</i> "
            "hasta que el workspace los justifica — o hasta que se fuerza con "
            "<font face='Courier'>LIVESPEC_PLUGINS</font>. Todo tool exige "
            "<font face='Courier'>workspace</font> absoluto.",
            st["subtitle"],
        ),
    ]

    for title, blurb, rows in TOOLS:
        story.append(
            KeepTogether(
                [
                    Paragraph(title, st["h2"]),
                    Paragraph(blurb, st["blurb"]),
                    _tool_table(rows[:1], st),
                ]
            )
        )
        if len(rows) > 1:
            story.append(_tool_table(rows[1:], st))

    story.append(Spacer(1, 10))
    for title, body in EXTRAS:
        story.append(KeepTogether([Paragraph(title, st["h2"]), Paragraph(body, st["extra"])]))

    doc.build(story)
    return out_path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/livespec-tools.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    print(build(target))
