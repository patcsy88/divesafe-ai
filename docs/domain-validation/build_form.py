"""Build the fillable PDF form from the questionnaire, the rule template and the checklist.

Run it with a Python that has `reportlab` (not a project dependency; use a throwaway venv):

    python build_form.py [output.pdf]

The wording comes from the markdown files in this folder, so the form cannot drift from them. It
holds no limits and pre-fills nothing. Filled-in copies name people: keep them out of git (the
`returned/` folder is ignored).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

HERE = Path(__file__).parent
OUT_DEFAULT = HERE / "divesafe-domain-validation-form.pdf"
RULE_COPIES = 8
FACTORS = (
    "wind",
    "wave_height",
    "swell",
    "swell_period",
    "current",
    "tidal_current",
    "weather",
    "forecast_uncertainty",
    "site_constraint",
)

W, H = A4
LEFT = 18 * mm
RIGHT = W - 18 * mm
WIDTH = RIGHT - LEFT
TOP = H - 20 * mm
BOTTOM = 18 * mm

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=11.5)
SMALL = ParagraphStyle(
    "small", parent=BODY, fontSize=8, leading=10, textColor=colors.HexColor("#444444")
)
H1 = ParagraphStyle("h1", parent=BODY, fontName="Helvetica-Bold", fontSize=15, leading=18)
H2 = ParagraphStyle("h2", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=14)
LABEL = ParagraphStyle("label", parent=BODY, fontName="Helvetica-Bold", fontSize=8.5, leading=10.5)
NOTICE = ParagraphStyle(
    "notice",
    parent=BODY,
    backColor=colors.HexColor("#FFF4CC"),
    borderPadding=5,
    borderColor=colors.HexColor("#C9A400"),
    borderWidth=0.6,
)


def markup(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)
    text = re.sub(r"_{3,}", "", text)
    return text.strip()


class Form:
    def __init__(self, path: Path) -> None:
        self.c = Canvas(str(path), pagesize=A4)
        self.c.setTitle("DiveSafe AI - domain validation form")
        self.c.setAuthor("DiveSafe AI project")
        self.c.setSubject("Fillable form for a dive professional. Not a safety document.")
        self.y = TOP
        self.page = 1
        self.names: set[str] = set()
        self.n = 0

    def _footer(self) -> None:
        self.c.setFont("Helvetica", 7.5)
        self.c.setFillColor(colors.HexColor("#555555"))
        self.c.drawString(
            LEFT,
            10 * mm,
            "DiveSafe AI domain validation form. Nothing here is a recommended limit. Decision support only; it does not guarantee safety.",
        )
        self.c.drawRightString(RIGHT, 10 * mm, f"Page {self.page}")
        self.c.setFillColor(colors.black)

    def new_page(self) -> None:
        self._footer()
        self.c.showPage()
        self.page += 1
        self.y = TOP

    def need(self, h: float) -> None:
        if self.y - h < BOTTOM:
            self.new_page()

    def para(
        self, text: str, style: ParagraphStyle = BODY, gap: float = 4, raw: bool = False
    ) -> None:
        p = Paragraph(text if raw else markup(text), style)
        _, h = p.wrap(WIDTH, H)
        pad = 2 * getattr(style, "borderPadding", 0) if style is NOTICE else 0
        self.need(h + gap + pad)
        p.drawOn(self.c, LEFT, self.y - h - (pad / 2))
        self.y -= h + gap + pad

    def flush_para(self, buf: list[str]) -> None:
        if buf:
            self.para(" ".join(buf), gap=3)
            buf.clear()

    def space(self, h: float) -> None:
        self.y -= h

    def _name(self, stem: str) -> str:
        self.n += 1
        name = f"{stem}_{self.n}"
        assert name not in self.names
        self.names.add(name)
        return name

    def field(
        self, label: str, stem: str, height: float = 16, *, multiline: bool = False,
        readonly: bool = False, label_style: ParagraphStyle = LABEL,
    ) -> None:  # fmt: skip
        if label:
            _, lh = Paragraph(markup(label), label_style).wrap(WIDTH, H)
            self.need(lh + 1.5 + height + 5)  # a label never sits alone at the foot of a page
            self.para(label, label_style, gap=1.5)
        self.need(height + 5)
        flags = ("multiline " if multiline else "") + ("readOnly" if readonly else "")
        self.c.acroForm.textfield(
            name=self._name(stem), tooltip=re.sub(r"<[^>]+>", "", label)[:100] or stem,
            x=LEFT, y=self.y - height, width=WIDTH, height=height, fontName="Helvetica", fontSize=9,
            borderStyle="inset", borderWidth=0.8, borderColor=colors.HexColor("#7A7A7A"),
            fillColor=colors.HexColor("#EDEDED") if readonly else colors.HexColor("#F7FBFF"),
            forceBorder=True, fieldFlags=flags.strip(), maxlen=0, value="",
        )  # fmt: skip
        self.y -= height + 5

    def box(self, label: str, stem: str, *, indent: float = 0) -> None:
        p = Paragraph(markup(label), BODY)
        _, h = p.wrap(WIDTH - 8 * mm - indent, H)
        self.need(max(h, 10) + 3)
        self.c.acroForm.checkbox(
            name=self._name(stem), tooltip=re.sub(r"<[^>]+>", "", markup(label))[:100],
            x=LEFT + indent, y=self.y - 9, size=9, borderWidth=0.8, borderColor=colors.HexColor("#7A7A7A"),
            fillColor=colors.white, textColor=colors.black, forceBorder=True, checked=False,
        )  # fmt: skip
        p.drawOn(self.c, LEFT + indent + 7 * mm, self.y - h)
        self.y -= max(h, 10) + 3

    def combo(self, label: str, stem: str, options: tuple[str, ...]) -> None:
        self.para(label, LABEL, gap=1.5)
        self.need(21)
        self.c.acroForm.choice(
            name=self._name(stem), tooltip=label[:100], value="(choose)", options=["(choose)", *options],
            x=LEFT, y=self.y - 16, width=WIDTH * 0.5, height=16, fontName="Helvetica", fontSize=9,
            borderStyle="inset", borderWidth=0.8, borderColor=colors.HexColor("#7A7A7A"),
            fillColor=colors.HexColor("#F7FBFF"), forceBorder=True, fieldFlags="combo",
        )  # fmt: skip
        self.y -= 21

    def rule(self) -> None:
        self.c.setStrokeColor(colors.HexColor("#999999"))
        self.c.line(LEFT, self.y, RIGHT, self.y)
        self.y -= 6

    def heading(self, text: str, style: ParagraphStyle = H2, *, new_page: bool = False) -> None:
        if new_page:
            self.new_page()
        self.need(40)
        self.para(text, style, gap=4)

    def save(self) -> None:
        self._footer()
        self.c.save()


def read(name: str) -> list[str]:
    return (HERE / name).read_text(encoding="utf8").splitlines()


def split_sections(lines: list[str], level: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith(level + " "):
            out.append((line[len(level) + 1 :].strip(), []))
        elif out:
            out[-1][1].append(line)
    return out


def parse_part_a(lines: list[str]) -> list[tuple[str, list[str]]]:
    text = "\n".join(lines)
    a = text[text.index("## Part A") : text.index("## Part B")]
    sections = split_sections(a.splitlines(), "###")
    assert [s[0][:2] for s in sections] == ["A1", "A2", "A3", "A4", "A5"], "Part A changed shape"
    return sections


def parse_part_b(lines: list[str]) -> list[tuple[str, list[str]]]:
    text = "\n".join(lines)
    b = text[text.index("## Part B") : text.index("## Part C")]
    sections = split_sections(b.splitlines(), "###")
    ids = [re.search(r"\(`(\w+)`\)", s[0]).group(1) for s in sections]  # type: ignore[union-attr]
    assert ids == list(FACTORS), f"Part B factors changed: {ids}"
    return sections


def paragraphs(body: list[str]) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for line in body:
        if not line.strip():
            if cur:
                out.append(" ".join(cur))
                cur = []
        else:
            cur.append(line.strip())
    if cur:
        out.append(" ".join(cur))
    return out


def build(path: Path) -> Form:
    q = read("questionnaire.md")
    part_a, part_b = parse_part_a(q), parse_part_b(q)
    part_c = [x for x in q[q.index("## Part C: not asked, not evaluated") :] if x.startswith("- ")]
    f = Form(path)

    f.heading("DiveSafe AI: domain validation form", H1)
    f.para(
        "Fillable form for a dive professional. Fill it in on screen (any PDF reader), save it, and send the saved file back. You can also print it and write by hand.",
        gap=6,
    )
    f.para(
        "**Signing a limit does not make any dive safe.** This form feeds a decision-support tool. The diver, dive master or dive leader always makes the final decision.",
        NOTICE,
        gap=8,
    )
    f.para(
        '**Do not guess.** A blank is safe for the software, because it stays cautious. A guessed number is not. Every limit needs a named, dated source: a training-agency standard, a local operator\'s written rule, an official marine authority publication, or your own documented site briefing. "Common practice" is not a source. **No numbers are suggested on purpose.**',
        gap=6,
    )
    f.para(
        '**"Not assessed" is an honest answer.** While any factor is unassessed the software cannot say GO or CAUTION, so a partial form cannot unlock a recommendation. A NO-GO limit you supply can still feed the decision support.',
        gap=6,
    )
    f.para(
        "**What the software can apply today:** a limit scoped to named sites and a maximum depth, for wind speed, wave height, swell, swell period or current, with your forecast margin, the worse direction, up to three bands, your source, reviewers and an expiry date. It cannot yet apply a limit that depends on diver qualification, dive type, season, exposure direction or per-factor data age. Such a limit is recorded as written and refused, never widened. Please still fill in the fields: your answers decide what we build next.",
        gap=8,
    )
    f.heading("About you")
    f.field("Name and role", "who_name")
    f.field("Qualifications / certifications", "who_quals")
    f.field("Organisation", "who_org")
    f.field("Date", "who_date", 16)

    f.heading("Part A: decisions about caution", H1, new_page=True)
    f.para(
        "These need a person with authority over safety policy. Each has a safe default that the software uses until you answer.",
        gap=6,
    )
    for title, body in part_a:
        stem = title[:2].lower()
        f.heading(title)
        buf: list[str] = []
        for line in body:
            s = line.strip()
            if not s:
                f.flush_para(buf)
            elif s.startswith("- [ ]"):
                f.flush_para(buf)
                f.box(s[5:].strip(), f"{stem}_option", indent=2 * mm)
                if "____" in s:
                    f.field("", f"{stem}_option_text", 16)
            elif s.startswith("- "):
                f.flush_para(buf)
                f.field(s[2:], f"{stem}_point", 30, multiline=True, label_style=BODY)
            elif s.startswith("Your reasoning"):
                f.flush_para(buf)  # the answer box below carries this prompt
            else:
                buf.append(s)
        f.flush_para(buf)
        f.field(
            "Your answer and reasoning (what protects the diver if the model is wrong?)",
            f"{stem}_answer",
            60,
            multiline=True,
        )
        f.box(
            "Deferred: I am not answering this now (the safe default stays)",
            f"{stem}_deferred",
            indent=2 * mm,
        )
        f.space(4)

    f.heading("Part B: one section per risk factor", H1, new_page=True)
    f.para(
        "For each factor you see what data we hold, so you can judge whether it can support a limit, then questions. Use a rule definition (after Part C) for every limit you supply, and say here which ones you attached.",
        gap=6,
    )
    for i, (title, body) in enumerate(part_b):
        factor = FACTORS[i]
        f.rule()
        f.heading(title)
        for para in paragraphs(body):
            if (
                para.startswith("**Questions:**")
                or para.startswith("**Your answer")
                or para.startswith("- [ ]")
            ):
                continue
            if re.match(r"\d+\. ", para):
                continue
            f.para(para, gap=4)
        qs = [ln.strip() for ln in body if re.match(r"\s*\d+\. ", ln)]
        f.para("**Questions**", gap=2)
        for qn in qs:
            f.field(qn, f"b_{factor}_q", 38, multiline=True, label_style=BODY)
        f.field(
            "Your answer / rule definitions attached (numbers of the rule forms)",
            f"b_{factor}_answer",
            30,
            multiline=True,
        )
        f.box("Not assessed (the factor stays TBD)", f"b_{factor}_not_assessed")
        f.space(4)

    f.heading("Part C: not asked, not evaluated", H1, new_page=True)
    f.para(
        "The software holds or may later hold data for these, but they are not risk factors with a form, so they do not influence any recommendation today. If you think one should be, say so.",
        gap=4,
    )
    for item in part_c:
        f.para("- " + item[2:], gap=2)
    f.field("Your comments", "c_comments", 70, multiline=True)

    for n in range(1, RULE_COPIES + 1):
        rule_form(f, n)

    signoff(f)
    f.save()
    return f


def rule_form(f: Form, n: int) -> None:
    f.heading(f"Rule definition {n} of {RULE_COPIES}: one per limit", H1, new_page=True)
    f.para(
        "Leave anything you do not know blank. **Do not fill a field with a number you cannot source.** Leave a copy empty if you have no limit to supply. Need more copies? Ask for another form or send a second file.",
        gap=6,
    )
    p = f"r{n}"
    f.field("RULE ID (a short unique name, e.g. wave_height.shore.open_water)", f"{p}_id")
    f.combo("RISK FACTOR", f"{p}_factor", FACTORS)
    f.para(
        "**STATUS:** REQUIRES DOMAIN VALIDATION. This changes to VALIDATED only at sign-off, by the developer, after your written read-back confirmation.",
        gap=6,
    )
    f.heading("Applies to", LABEL)
    f.field("Site(s)", f"{p}_sites")
    f.para("Dive type", LABEL, gap=1.5)
    for t in ("shore", "boat", "drift"):
        f.box(t, f"{p}_type_{t}", indent=2 * mm)
    f.field("Other dive type", f"{p}_type_other")
    f.field("Diver qualification", f"{p}_qual")
    f.field("Maximum depth covered (with unit)", f"{p}_depth")
    f.field("Seasons or conditions only", f"{p}_season")
    f.heading("Limits (write the unit; one line per level you want)", LABEL)
    f.field("NO-GO when", f"{p}_nogo")
    f.field("CAUTION when", f"{p}_caution")
    f.field("GO is acceptable when (all other rules and evidence must also be OK)", f"{p}_go")
    f.field(
        "Or describe the limit in your own words if it is not a simple band (conditional, combined with another factor, depends on a direction, ...)",
        f"{p}_words",
        54,
        multiline=True,
    )
    f.field(
        "Quantity the limit applies to (which kind of height, which direction convention)",
        f"{p}_quantity",
    )
    f.field(
        "Direction or exposure matters? (e.g. depends on swell direction relative to the site)",
        f"{p}_direction",
        30,
        multiline=True,
    )
    f.heading("Forecast margin", LABEL)
    f.field(
        "How much error must the system allow for because the input is a forecast or a model?",
        f"{p}_margin",
        30,
        multiline=True,
    )
    f.field(
        "If the data are older than (age) or further ahead than (look-ahead), treat as:", f"{p}_age"
    )
    f.heading("If the input is missing or unusable", LABEL)
    f.para('The system will report "not evaluated" and INSUFFICIENT EVIDENCE. Do you agree?', gap=2)
    f.box("Yes, I agree", f"{p}_missing_yes", indent=2 * mm)
    f.box("No", f"{p}_missing_no", indent=2 * mm)
    f.field("Because", f"{p}_missing_why", 30, multiline=True)
    f.heading("Source (required: a limit without a source cannot be used)", LABEL)
    f.field("Document / authority", f"{p}_src_doc")
    f.field("Version / date", f"{p}_src_ver")
    f.field("Page or section", f"{p}_src_page")
    f.box("It is written down publicly", f"{p}_src_public", indent=2 * mm)
    f.field("Where", f"{p}_src_where")
    f.heading(
        "Can the software apply this today? (the developer fills this in; leave blank)", LABEL
    )
    f.field("Fields the software cannot represent yet", f"{p}_dev_unrep", readonly=True)
    f.field("Encoded narrower / refused", f"{p}_dev_refused", readonly=True)
    f.heading("Review", LABEL)
    f.field("Prepared by (name, role, qualification)", f"{p}_by")
    f.field("Date", f"{p}_date")
    f.field(
        "Independent second reviewer (REQUIRED for any limit that can allow GO or CAUTION; for a NO-GO-only limit, record why if there is none)",
        f"{p}_second",
        30,
        multiline=True,
    )
    f.field(
        "Review expires on (the rule must not be used after this date without re-review)",
        f"{p}_expires",
    )
    f.field("Limitations you want shown to the user", f"{p}_limits", 54, multiline=True)


def _items(text: str, heading: str) -> list[str]:
    """The `- [ ]` items of one section, with their indented continuation lines joined."""
    items: list[str] = []
    for ln in _section(text, heading):
        if ln.startswith("- [ ]"):
            items.append(ln[5:].strip())
        elif ln.startswith("      ") and items:
            items[-1] += " " + ln.strip()
    return items


def signoff(f: Form) -> None:
    text = "\n".join(read("signoff-checklist.md"))
    per_rule = _items(text, "## Per rule")
    per_factor = _items(text, "## Per factor you did NOT assess")
    part_a = _items(text, "## Part A decisions")
    overall = _items(text, "## Overall")
    assert len(per_rule) >= 7 and len(per_factor) == 1 and len(part_a) == 1 and overall, (
        "checklist changed shape"
    )
    f.heading("Sign-off checklist", H1, new_page=True)
    f.para(
        "A rule is only encoded as VALIDATED when every box below is ticked for it. Tick the boxes that are true for the rules you attach.",
        gap=6,
    )
    for title, group, stem in (
        ("Per rule", per_rule, "s_rule"),
        ("Per factor you did NOT assess", per_factor, "s_factor"),
        ("Part A decisions", part_a, "s_a"),
        ("Overall", overall, "s_all"),
    ):
        f.heading(title)
        for k, it in enumerate(group):
            f.box(it, f"{stem}_{k}", indent=2 * mm)
    f.space(6)
    f.field("Name and role", "s_name")
    f.field("Qualifications / certifications", "s_quals")
    f.field("Organisation", "s_org")
    f.field("Date", "s_date")
    f.field(
        "Signature (type your name to sign on screen, or print this page and sign by hand)",
        "s_sig",
        28,
    )
    f.para(
        "The signed document is the only evidence that a rule is validated; the software cannot check it. The developer will send you a read-back table (limit, unit, quantity, scope, as encoded) and needs your written confirmation before marking anything VALIDATED.",
        SMALL,
        gap=2,
    )


def _section(text: str, heading: str) -> list[str]:
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = re.search(r"^## |^```", rest, flags=re.M)
    return (rest[: end.start()] if end else rest).splitlines()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    form = build(target)
    print(f"wrote {target} ({form.page} pages, {len(form.names)} fields)")
