"""PDF and Excel files for a report (plan 6.6), in English or Arabic.

PDFs use ReportLab (pure Python). Arabic needs a font with Arabic letters and has to be
shaped (letters joined) and put in visual order before drawing: arabic-reshaper and
python-bidi do that, and Arabic pages read right to left (columns reversed, text aligned
right). Excel handles Arabic itself; Arabic sheets are set right to left.
"""

import io
import logging
import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import arabic_reshaper
from bidi import get_display
from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from cctv_cloud.config import get_settings
from cctv_cloud.services import texts
from cctv_cloud.services.reports import ReportData

log = logging.getLogger(__name__)

# Fonts with Latin and Arabic letters: Debian/Ubuntu (fonts-dejavu-core, in the API image),
# Windows, macOS.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
BRAND = colors.HexColor("#1d4ed8")
GRID = colors.HexColor("#cbd5e1")
HEADER_BG = colors.HexColor("#f1f5f9")


@lru_cache
def _font() -> str:
    configured = get_settings().report_font
    for candidate in ([configured] if configured else []) + list(FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            pdfmetrics.registerFont(TTFont("ReportFont", candidate))
            return "ReportFont"
    log.warning("no font with Arabic letters found (set CCTV_REPORT_FONT); using Helvetica")
    return "Helvetica"


def _shape(value: object) -> str:
    text = "" if value is None else str(value)
    return get_display(arabic_reshaper.reshape(text)) if _ARABIC.search(text) else text


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:g}%"


def _fmt_ms(value: int | None) -> str:
    return "-" if value is None else f"{value / 1000:.1f} s"


def _fmt(value: object) -> str:
    return "-" if value is None else str(value)


def summary_rows(data: ReportData, locale: str) -> list[tuple[str, str]]:
    t, totals, alerts = (lambda k: texts.t(locale, k)), data.totals, data.alerts
    rows = [
        (t("report.events"), str(totals.events)),
        (t("report.critical"), str(totals.critical)),
        (t("report.security"), str(totals.security)),
        (t("report.compliance"), str(totals.compliance)),
        (t("report.cashier"), str(totals.cashier)),
        (t("report.false_alarms"), str(totals.false_alarms)),
        (t("report.unreviewed"), str(totals.unreviewed)),
        (t("report.on_time"), str(totals.on_time)),
        (t("report.late"), str(totals.late)),
        (t("report.absent"), str(totals.absent)),
        (t("report.attendance_rate"), _fmt_pct(totals.attendance_pct)),
        (t("report.punctuality"), _fmt_pct(totals.punctuality_pct)),
        (t("report.customers"), _fmt(totals.customers_in)),
        (
            t("report.peak_hour"),
            "-" if totals.peak_hour is None else f"{totals.peak_hour:02d}:00",
        ),
        (t("report.peak_occupancy"), _fmt(totals.peak_occupancy)),
        (t("report.counter_staffed"), _fmt_pct(totals.counter_staffed_pct)),
        (t("report.alerts"), str(alerts.sent)),
        (t("report.alert_p95"), _fmt_ms(alerts.p95_ms)),
        (t("report.alerts_within_target"), _fmt_pct(alerts.within_10s_pct)),
        (t("report.alerts_failed"), str(alerts.failed)),
    ]
    if totals.drawer_no_sale is not None:  # the till is connected
        rows.insert(5, (t("report.drawer_no_sale"), str(totals.drawer_no_sale)))
    return rows


def _tables(data: ReportData, locale: str) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, header, rows) for each table, shared by PDF and Excel."""
    t = lambda k: texts.t(locale, k)  # noqa: E731
    tz = ZoneInfo(data.timezone)
    tables = []
    if len(data.days) > 1:
        tables.append(
            (
                t("report.by_day"),
                [
                    t(k)
                    for k in (
                        "report.date",
                        "report.events",
                        "report.critical",
                        "report.security",
                        "report.compliance",
                        "report.on_time",
                        "report.late",
                        "report.absent",
                        "report.customers",
                    )
                ],
                [
                    [
                        d.date.isoformat(),
                        str(d.events),
                        str(d.critical),
                        str(d.security),
                        str(d.compliance),
                        str(d.on_time),
                        str(d.late),
                        str(d.absent),
                        _fmt(d.customers_in),
                    ]
                    for d in data.days
                ],
            )
        )
    if data.hours:
        tables.append(
            (
                t("report.by_hour"),
                [t("report.hour"), t("report.customers")],
                [[f"{h:02d}:00", str(n)] for h, n in enumerate(data.hours) if n],
            )
        )
    tables.append(
        (
            t("report.by_type"),
            [
                t("report.type"),
                t("report.total"),
                t("report.valid"),
                t("report.false_alarms"),
                t("report.unreviewed"),
            ],
            [
                [
                    texts.event_label(locale, r.event_type),
                    str(r.total),
                    str(r.valid),
                    str(r.false_alarm),
                    str(r.unreviewed),
                ]
                for r in data.types
            ],
        )
    )
    tables.append(
        (
            t("report.staff"),
            [
                t("report.name"),
                t("report.code"),
                t("report.on_time"),
                t("report.late"),
                t("report.absent"),
                t("report.attendance_rate"),
                t("report.punctuality"),
                t("report.compliance"),
            ],
            [
                [
                    s.name,
                    s.employee_code,
                    str(s.on_time),
                    str(s.late),
                    str(s.absent),
                    _fmt_pct(s.attendance_pct),
                    _fmt_pct(s.punctuality_pct),
                    str(s.compliance_issues),
                ]
                for s in data.staff
            ],
        )
    )
    if data.busiest_areas:
        tables.append(
            (
                t("report.busiest_areas"),
                [t("report.camera"), t("report.area"), t("report.minutes"), t("report.share")],
                [
                    [a.camera, t(f"report.area.{a.area}"), str(a.minutes), _fmt_pct(a.share_pct)]
                    for a in data.busiest_areas
                ],
            )
        )
    tables.append(
        (
            t("report.security"),
            [t("report.time"), t("report.type"), t("report.camera"), t("report.status")],
            [
                [
                    f"{e.started_at.astimezone(tz):%Y-%m-%d %H:%M}",
                    texts.event_label(locale, e.event_type),
                    e.camera,
                    t(f"report.review.{e.review_status}"),
                ]
                for e in data.security_events
            ],
        )
    )
    return tables


def _title(data: ReportData, locale: str) -> tuple[str, list[str]]:
    """The title, and the subtitle as pieces that are each all-Arabic or all-Latin (in
    reading order), so right-to-left lines can be put together without bidi surprises."""
    title = f"{texts.t(locale, f'report.{data.period.value}')}: {data.branch_name}"
    generated = data.generated_at.astimezone(ZoneInfo(data.timezone))
    pieces = [data.start.isoformat()]
    if data.end != data.start:
        pieces += [texts.t(locale, "report.to"), data.end.isoformat()]
    pieces += ["·", texts.t(locale, "report.generated"), f"{generated:%Y-%m-%d %H:%M}"]
    if data.partial:
        pieces += ["·", texts.t(locale, "report.partial")]
    return title, pieces


def render_pdf(data: ReportData, locale: str) -> bytes:
    rtl = texts.lang(locale) == "ar"
    font = _font()
    align = TA_RIGHT if rtl else TA_LEFT
    h1 = ParagraphStyle(
        "h1", fontName=font, fontSize=16, leading=20, alignment=align, textColor=BRAND
    )
    h2 = ParagraphStyle(
        "h2", fontName=font, fontSize=12, leading=16, alignment=align, spaceBefore=8, spaceAfter=4
    )
    body = ParagraphStyle(
        "body",
        fontName=font,
        fontSize=9,
        leading=12,
        alignment=align,
        textColor=colors.HexColor("#475569"),
    )

    def order(cells: list[str]) -> list[str]:
        shaped = [_shape(c) for c in cells]
        return shaped[::-1] if rtl else shaped

    def table(header: list[str] | None, rows: list[list[str]], widths=None) -> Table:
        content = ([order(header)] if header else []) + [order(r) for r in rows]
        result = Table(
            content,
            colWidths=widths[::-1] if rtl and widths else widths,
            repeatRows=1 if header else 0,
            hAlign="RIGHT" if rtl else "LEFT",
        )
        style = [
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, GRID),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT" if rtl else "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), HEADER_BG))
        result.setStyle(TableStyle(style))
        return result

    title, pieces = _title(data, locale)
    subtitle = " ".join(_shape(p) for p in pieces[::-1]) if rtl else " ".join(pieces)
    story = [Paragraph(_shape(title), h1), Paragraph(subtitle, body), Spacer(1, 4 * mm)]
    story += [Paragraph(_shape(texts.t(locale, "report.summary")), h2)]
    summary = summary_rows(data, locale)
    half = (len(summary) + 1) // 2
    pairs = [
        [*left, *(summary[half + i] if half + i < len(summary) else ("", ""))]
        for i, left in enumerate(summary[:half])
    ]
    story.append(table(None, pairs, [55 * mm, 30 * mm, 55 * mm, 30 * mm]))

    if data.hours and any(data.hours):
        story.append(Paragraph(_shape(texts.t(locale, "report.by_hour")), h2))
        drawing = Drawing(170 * mm, 45 * mm)
        chart = VerticalBarChart()
        chart.x, chart.y, chart.width, chart.height = 10 * mm, 8 * mm, 155 * mm, 33 * mm
        chart.data = [data.hours[::-1] if rtl else data.hours]
        chart.bars[0].fillColor = BRAND
        chart.bars[0].strokeColor = None
        chart.categoryAxis.categoryNames = [f"{h:02d}" for h in range(24)][:: -1 if rtl else 1]
        chart.categoryAxis.labels.fontName = font
        chart.categoryAxis.labels.fontSize = 6
        chart.valueAxis.labels.fontName = font
        chart.valueAxis.labels.fontSize = 6
        chart.valueAxis.valueMin = 0
        drawing.add(chart)
        story.append(drawing)

    for heading, header, rows in _tables(data, locale):
        if heading == texts.t(locale, "report.by_hour"):
            continue  # drawn as a chart above
        story.append(Paragraph(_shape(heading), h2))
        if rows:
            story.append(table(header, rows))
        else:
            story.append(Paragraph(_shape(texts.t(locale, "report.none")), body))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title,
        author="AI CCTV",
    )
    doc.build(story)
    return buffer.getvalue()


def render_xlsx(data: ReportData, locale: str) -> bytes:
    rtl = texts.lang(locale) == "ar"
    bold = Font(bold=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = texts.t(locale, "report.summary")[:31]
    title, pieces = _title(data, locale)
    sheet.append([title])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([" ".join(pieces)])
    sheet.append([])
    for label, value in summary_rows(data, locale):
        sheet.append([label, value])
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 14
    sheet.sheet_view.rightToLeft = rtl

    for index, (heading, header, rows) in enumerate(_tables(data, locale), start=1):
        # Sheet names: at most 31 characters, unique, no []:*?/\
        name = re.sub(r"[\[\]:*?/\\]", " ", heading)[:28] + f" {index}"
        ws = workbook.create_sheet(name)
        ws.append(header)
        for cell in ws[1]:
            cell.font = bold
        for row in rows:
            ws.append([_number(v) for v in row])
        for column, label in zip(ws.columns, header, strict=False):
            width = max([len(label)] + [len(str(c.value or "")) for c in column])
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 8), 40)
        ws.freeze_panes = "A2"
        ws.sheet_view.rightToLeft = rtl

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _number(value: str) -> int | str:
    """Counts as numbers so Excel can sum them; everything else stays text."""
    return int(value) if value.isdigit() else value


def file_name(data: ReportData, extension: str) -> str:
    stamp = data.start.isoformat() if data.start == data.end else f"{data.start}_{data.end}"
    return f"report-{data.period.value}-{stamp}.{extension}"
