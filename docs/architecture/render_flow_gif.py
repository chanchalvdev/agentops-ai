"""Render docs/architecture/system-flow.gif: animated end-to-end request flows.

    uv run --with pillow python docs/architecture/render_flow_gif.py

Each component carries a status badge ("built" or the plan day that delivers it), so the
animation never presents planned parts as finished. Update STATUS as days land.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 940
PANEL_X = 1190  # right-hand narration panel
OUT = Path(__file__).with_name("system-flow.gif")

# Arial Unicode covers → ⊆ ≤ ✓ ● ✕; Arial Bold is used only for text without symbols.
UNI = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MONO = "/System/Library/Fonts/Menlo.ttc"
F_TITLE = ImageFont.truetype(BOLD, 26)
F_GROUP = ImageFont.truetype(BOLD, 14)
F_BOX = ImageFont.truetype(UNI, 13)
F_BOX_B = ImageFont.truetype(UNI, 14)
F_BADGE = ImageFont.truetype(BOLD, 10)
F_STEP = ImageFont.truetype(UNI, 15)
F_SCENE = ImageFont.truetype(UNI, 21)
F_SMALL = ImageFont.truetype(MONO, 12)

BG, INK, MUTED, LINE = "#0f1419", "#e6edf3", "#8b949e", "#30363d"
ACCENT, OK, WARN, BAD = "#58a6ff", "#3fb950", "#d29922", "#f85149"

# Component boxes: key -> (x, y, w, h, label, group)
BOXES: dict[str, tuple[int, int, int, int, str, str]] = {
    "ui": (40, 92, 230, 50, "Web UI · chat · docs · terminal", "clients"),
    "cli": (290, 92, 230, 50, "CLI · ingest · search · ask", "clients"),
    "auth": (40, 190, 230, 50, "Auth · tenant · request ID", "api"),
    "routes": (290, 190, 470, 50, "Routers: documents · retrieval · agent/run (SSE)", "api"),
    "approvals_api": (780, 190, 380, 50, "approvals · workspace (WS terminal) · evals", "api"),
    "ctx": (40, 300, 230, 50, "ExecutionContext + budgets", "harness"),
    "policy": (40, 365, 230, 50, "Policy gate (perm · args · budget)", "harness"),
    "ledger": (40, 430, 230, 50, "Execution ledger", "harness"),
    "classify": (310, 300, 145, 42, "classify_request", "graph"),
    "plan": (470, 300, 145, 42, "plan_workflow", "graph"),
    "retrieve": (630, 300, 145, 42, "retrieve_evidence", "graph"),
    "tools": (790, 300, 145, 42, "execute_tools", "graph"),
    "generate": (310, 370, 145, 42, "generate_answer", "graph"),
    "citations": (470, 370, 145, 42, "validate_citations", "graph"),
    "verify": (630, 370, 145, 42, "verify_answer", "graph"),
    "retry": (790, 370, 145, 42, "retry_or_revise ≤2", "graph"),
    "finalize": (470, 440, 145, 42, "finalize_execution", "graph"),
    "approval": (790, 440, 145, 42, "human_approval", "graph"),
    "doctools": (975, 300, 185, 50, "document_search · metadata · calc", "tools"),
    "wstools": (975, 365, 185, 50, "workspace read / write / exec", "tools"),
    "sandbox": (975, 440, 185, 50, "Sandbox: path jail · allowlist", "tools"),
    "wsdir": (975, 505, 185, 34, "WORKSPACE_ROOT/<tenant>", "tools"),
    "ingest": (40, 600, 250, 56, "Ingestion: validate · extract · chunk", "rag"),
    "hybrid": (310, 600, 270, 56, "Hybrid retrieval: vector + FTS → RRF", "rag"),
    "router": (600, 600, 220, 56, "LLM router: retry · fallback", "rag"),
    "nomic": (310, 700, 170, 46, "nomic-embed-text", "ollama"),
    "small": (495, 700, 150, 46, "qwen3:1.7b", "ollama"),
    "big": (660, 700, 160, 46, "qwen3:8b", "ollama"),
    "db": (
        40,
        800,
        780,
        52,
        "PostgreSQL 17 + pgvector · documents · chunks · executions · evals",
        "data",
    ),
    "trace": (860, 600, 300, 44, "Tracing (Langfuse / JSON logs)", "ops"),
    "eval": (860, 656, 300, 44, "Eval runner · golden JSONL · judges", "ops"),
    "drift": (860, 712, 300, 44, "Cost · latency · drift · alerts", "ops"),
    "recorder": (860, 800, 300, 52, "Recorder agent → journal + interview", "ops"),
}

GROUPS = {  # key -> (x, y, w, h, title)
    "clients": (25, 60, 510, 95, "Clients"),
    "api": (25, 160, 1150, 95, "FastAPI  /api/v1"),
    "harness": (25, 268, 260, 225, "Agent harness"),
    "graph": (295, 268, 655, 225, "LangGraph workflow (checkpointed)"),
    "tools": (960, 268, 215, 285, "Tool registry"),
    "rag": (25, 568, 810, 100, "RAG + model routing"),
    "ollama": (295, 676, 540, 85, "Ollama (local)"),
    "data": (25, 768, 810, 95, "Persistence"),
    "ops": (845, 568, 330, 295, "Observability · Evaluation · Dev process"),
}

# What exists today. Everything else shows the day that delivers it.
STATUS = {
    k: "built"
    for k in (
        "cli",
        "auth",
        "routes",
        "ingest",
        "hybrid",
        "router",
        "nomic",
        "small",
        "big",
        "db",
        "recorder",
    )
}
PLANNED = {
    "ui": "Day 5",
    "approvals_api": "Day 2-3",
    "trace": "Day 3",
    "eval": "Day 3",
    "drift": "Day 4",
    "wstools": "Day 3",
    "sandbox": "Day 3",
    "wsdir": "Day 3",
}

# Scenes: (title, subtitle, steps). Each step = (component, narration, style)
# style: "ok" normal hop, "bad" rejected/blocked, "warn" loop/approval.
SCENES = [
    (
        "1 · Document ingestion",
        "Upload → index, atomically",
        [
            ("ui", "User uploads runbook.pdf", "ok"),
            ("auth", "X-API-Key → tenant_a; request ID assigned", "ok"),
            ("routes", "POST /documents: size, type, magic bytes", "ok"),
            ("ingest", "Extract text (pages kept), sha256 dedupe", "ok"),
            ("routes", "202 Accepted → client polls status", "ok"),
            ("ingest", "Background: structured chunking", "ok"),
            ("nomic", "Embed chunks ('search_document:')", "ok"),
            ("db", "One transaction: chunks + READY", "ok"),
        ],
    ),
    (
        "2 · Grounded question answering",
        "classify → plan → retrieve → answer",
        [
            ("ui", "Ask: 'RPO in v2 vs v1?'", "ok"),
            ("auth", "Authenticate, scope to tenant", "ok"),
            ("routes", "POST /agent/run (SSE stream)", "ok"),
            ("ctx", "Budgets: steps, calls, deadline, tokens", "ok"),
            ("classify", "Classify intent", "ok"),
            ("small", "Cheap model: qwen3:1.7b", "ok"),
            ("plan", "Plan: needs evidence", "ok"),
            ("retrieve", "Retrieve evidence", "ok"),
            ("hybrid", "Vector + keyword → RRF, tenant filter", "ok"),
            ("db", "pgvector HNSW + GIN tsvector", "ok"),
            ("generate", "Draft answer from fenced evidence", "ok"),
            ("big", "qwen3:8b, JSON-schema output", "ok"),
            ("citations", "Cited IDs ⊆ retrieved IDs", "ok"),
            ("verify", "Claims supported by sources?", "ok"),
            ("finalize", "Persist result + usage", "ok"),
            ("ui", "Stream answer with citations", "ok"),
        ],
    ),
    (
        "3 · Tool call through the policy gate",
        "the model asks, the harness decides",
        [
            ("plan", "Plan: needs a calculation", "ok"),
            ("tools", "Model requests calculator('17% of 2340')", "ok"),
            ("policy", "Args fail schema → rejected", "bad"),
            ("tools", "Correction: calculator('0.17*2340')", "warn"),
            ("policy", "Allowed · in budget · timeout set", "ok"),
            ("doctools", "Safe AST evaluator → 397.8", "ok"),
            ("ledger", "Record call, args, result, latency", "ok"),
            ("generate", "Answer uses tool result", "ok"),
        ],
    ),
    (
        "4 · Workspace write with human approval",
        "sandboxed read/write + terminal",
        [
            ("tools", "Agent wants to edit config.yaml", "ok"),
            ("wstools", "workspace_edit is a WRITE tool", "ok"),
            ("approval", "Graph interrupts at human_approval", "warn"),
            ("approvals_api", "Pending approval + diff shown in UI", "warn"),
            ("ui", "User approves", "ok"),
            ("approval", "Resume from checkpoint", "ok"),
            ("sandbox", "realpath ⊂ root · deny .env/keys", "ok"),
            ("wsdir", "Write applied, audited", "ok"),
            ("sandbox", "'../../etc/passwd' → blocked", "bad"),
        ],
    ),
    (
        "5 · Failure handling and bounded retries",
        "never loop forever",
        [
            ("generate", "Draft cites [S42] (not retrieved)", "ok"),
            ("citations", "Invalid citation detected", "bad"),
            ("retry", "Revise (attempt 1 of 2)", "warn"),
            ("generate", "Regenerate with valid IDs listed", "ok"),
            ("router", "Ollama down → retry w/ jitter", "warn"),
            ("router", "Transient only → optional fallback", "warn"),
            ("ctx", "Budget exhausted → stop", "bad"),
            ("finalize", "Return safe, explicit failure", "ok"),
        ],
    ),
    (
        "6 · Observability, evaluation, drift",
        "measure, don't assume",
        [
            ("ledger", "Every step: tokens, latency, cost", "ok"),
            ("trace", "One trace ID: API → graph → tools", "ok"),
            ("eval", "Golden set v1 (30+ cases) runs agent", "ok"),
            ("routes", "Same API path as real users", "ok"),
            ("eval", "Recall@k, MRR, citation validity", "ok"),
            ("db", "Store run vs baseline", "ok"),
            ("drift", "Threshold breached → alert", "bad"),
        ],
    ),
    (
        "7 · Development process",
        "every milestone is recorded",
        [
            ("cli", "Milestone done: tests + live run", "ok"),
            ("recorder", "Recorder reads code + results", "ok"),
            ("recorder", "→ journal: decisions, trade-offs", "ok"),
            ("recorder", "→ interview Q&A, 7 categories", "ok"),
        ],
    ),
]

STYLE_COLOR = {"ok": ACCENT, "bad": BAD, "warn": WARN}


def center(key: str) -> tuple[float, float]:
    x, y, w, h, *_ = BOXES[key]
    return x + w / 2, y + h / 2


def draw_groups(d: ImageDraw.ImageDraw) -> None:
    d.text((40, 18), "AgentOps AI - end-to-end flow", font=F_TITLE, fill=INK)
    for gx, gy, gw, gh, title in GROUPS.values():
        d.rounded_rectangle((gx, gy, gx + gw, gy + gh), 10, fill="#161b22", outline=LINE)
        d.text((gx + 10, gy + 6), title, font=F_GROUP, fill=MUTED)


def draw_boxes(d: ImageDraw.ImageDraw, active: set[str], done: set[str]) -> None:
    for key, (x, y, w, h, label, _) in BOXES.items():
        is_active, is_done = key in active, key in done
        # Visited = dim blue, deliberately not green: green means "built" in the legend.
        fill = "#1f3a5f" if is_active else ("#172433" if is_done else "#21262d")
        outline = ACCENT if is_active else ("#2f5f8f" if is_done else LINE)
        d.rounded_rectangle(
            (x, y, x + w, y + h), 8, fill=fill, outline=outline, width=3 if is_active else 1
        )
        font = F_BOX_B if is_active else F_BOX
        tw = d.textlength(label, font=font)
        while tw > w - 12 and len(label) > 4:  # shrink-to-fit by truncation
            label = label[:-2] + "…"
            tw = d.textlength(label, font=font)
        d.text((x + (w - tw) / 2, y + h / 2 - 8), label, font=font, fill=INK)
        # Badge sits on the top border so it never covers the label.
        badge = "built" if key in STATUS else PLANNED.get(key, "Day 2")
        color = OK if badge == "built" else "#6e7681"
        bw = d.textlength(badge, font=F_BADGE) + 10
        d.rounded_rectangle((x + w - bw - 8, y - 7, x + w - 8, y + 6), 4, fill=color)
        d.text((x + w - bw - 3, y - 6), badge, font=F_BADGE, fill=BG)


def _wrap(d: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and d.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return [*lines, line]


def draw_panel(
    d: ImageDraw.ImageDraw,
    scene: int,
    step: int,
    steps: list[tuple[str, str, str]],
    title: str,
    subtitle: str,
) -> None:
    d.rectangle((PANEL_X, 0, W, H), fill="#0b0f14")
    d.line((PANEL_X, 0, PANEL_X, H), fill=LINE, width=2)
    x = PANEL_X + 24
    overview = step < 0
    if not overview:
        d.text((x, 24), f"Flow {scene + 1} of {len(SCENES)}", font=F_SMALL, fill=MUTED)
    y = 46
    for line in _wrap(d, title, F_SCENE, W - x - 20):
        d.text((x, y), line, font=F_SCENE, fill=INK)
        y += 28
    d.text((x, y + 4), subtitle, font=F_STEP, fill=MUTED)
    y += 46
    for i, (_, text, style) in enumerate(steps):
        if overview:
            color, marker = MUTED, "•"
        elif i > step:
            color, marker = "#484f58", "○"
        elif i == step:
            color, marker = STYLE_COLOR[style], "●"
        else:
            color, marker = (BAD if style == "bad" else MUTED), "✓" if style != "bad" else "✕"
        d.text((x, y), marker, font=F_STEP, fill=color)
        words, line = text.split(), ""
        for word in words:  # wrap to panel width
            trial = f"{line} {word}".strip()
            if d.textlength(trial, font=F_STEP) > W - x - 50:
                d.text((x + 22, y), line, font=F_STEP, fill=color if i <= step else "#484f58")
                y += 20
                line = word
            else:
                line = trial
        d.text(
            (x + 22, y),
            line,
            font=F_STEP,
            fill=INK if i == step or overview else (color if i < step else "#484f58"),
        )
        y += 30
    ly = H - 120
    d.text((x, ly), "Legend", font=F_GROUP, fill=MUTED)
    for j, (c, t) in enumerate(
        [
            (ACCENT, "active hop"),
            (WARN, "loop / approval"),
            (BAD, "rejected / blocked"),
            (OK, "built (Day 1)"),
        ]
    ):
        d.rounded_rectangle((x, ly + 26 + j * 20, x + 14, ly + 38 + j * 20), 3, fill=c)
        d.text((x + 22, ly + 24 + j * 20), t, font=F_SMALL, fill=MUTED)


Segment = tuple[tuple[float, float], tuple[float, float], str]


def compose(
    active: set[str],
    done: set[str],
    trail: list[Segment],
    moving: Segment | None,
    cross: str | None,
    panel: tuple[int, int, list[tuple[str, str, str]], str, str],
) -> Image.Image:
    """Layering: groups -> path lines -> boxes -> moving dot -> narration panel."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_groups(d)
    for i, (a, b, c) in enumerate(trail):  # older hops fade so the current path stands out
        recent = i >= len(trail) - 4
        d.line((a, b), fill=c if recent else "#2b3f57", width=4 if recent else 2)
    if moving:
        d.line((moving[0], moving[1]), fill=moving[2], width=5)
    draw_boxes(d, active, done)
    if moving:
        px, py = moving[1]
        d.ellipse((px - 10, py - 10, px + 10, py + 10), fill=moving[2], outline=INK, width=2)
    if cross:
        x, y, _, h, *_ = BOXES[cross]
        cx, cy = x + 2, y + h / 2  # on the left border, so the label stays readable
        d.line((cx - 14, cy - 14, cx + 14, cy + 14), fill=BAD, width=5)
        d.line((cx - 14, cy + 14, cx + 14, cy - 14), fill=BAD, width=5)
    draw_panel(d, *panel)
    return img


def palette_image() -> Image.Image:
    """One shared palette for all frames, seeded with every semantic color so rare ones
    (amber, red) are never dropped by per-frame quantization."""
    ref = compose(set(BOXES), set(), [], None, None, (0, 0, SCENES[0][2], "", ""))
    d = ImageDraw.Draw(ref)
    for i, c in enumerate(
        [
            ACCENT,
            OK,
            WARN,
            BAD,
            INK,
            MUTED,
            "#1f3a5f",
            "#172433",
            "#2f5f8f",
            "#2b3f57",
            "#484f58",
            "#6e7681",
        ]
    ):
        d.rectangle((i * 40, 0, i * 40 + 39, 60), fill=c)
    return ref.quantize(colors=96, dither=Image.Dither.NONE)


def render() -> None:
    pal = palette_image()
    frames: list[Image.Image] = []
    durations: list[int] = []

    def add(img: Image.Image, ms: int) -> None:
        frames.append(img.quantize(palette=pal, dither=Image.Dither.NONE))
        durations.append(ms)

    add(
        compose(
            set(),
            set(),
            [],
            None,
            None,
            (
                0,
                -1,
                [("", t, "ok") for t, _, _ in SCENES],
                "System overview",
                "Badges show built vs the day it lands.",
            ),
        ),
        4000,
    )

    for si, (title, subtitle, steps) in enumerate(SCENES):
        trail: list[Segment] = []
        visited: set[str] = set()
        prev: str | None = None
        for i, (key, _, style) in enumerate(steps):
            color = STYLE_COLOR[style]
            if prev is not None and prev != key:
                (x0, y0), (x1, y1) = center(prev), center(key)
                for f in range(1, 8):
                    t = f / 7
                    head = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
                    add(
                        compose(
                            {prev},
                            visited,
                            trail,
                            ((x0, y0), head, color),
                            None,
                            (si, i - 1, steps, title, subtitle),
                        ),
                        55,
                    )
                trail.append(((x0, y0), (x1, y1), color))
            visited.add(key)
            add(
                compose(
                    {key},
                    visited - {key},
                    trail,
                    None,
                    key if style == "bad" else None,
                    (si, i, steps, title, subtitle),
                ),
                1600 if i == 0 else 1100,
            )
            prev = key
        durations[-1] += 1400  # linger on the completed scene

    frames[0].save(
        OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=False
    )
    print(
        f"{OUT} - {len(frames)} frames, {sum(durations) / 1000:.0f}s, "
        f"{OUT.stat().st_size / 1e6:.1f} MB"
    )


if __name__ == "__main__":
    render()
