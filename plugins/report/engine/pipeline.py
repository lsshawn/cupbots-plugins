"""Report engine pipeline — pure-async orchestrator, no CupBots imports.

Usage:
    python -m cupbots_plugins.plugins.report.engine.pipeline \
        --title "Test Report" \
        --company "Geohan Corporation Berhad" \
        --body-html /path/to/body.html \
        --out /tmp/report-test

Or from Python:
    from cupbots_plugins.plugins.report.engine.pipeline import build_report, ReportSpec
    result = await build_report(spec)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

ENGINE_DIR = Path(__file__).parent


@dataclass
class SectionSpec:
    """One report section (already frozen as HTML)."""

    title: str
    body_html: str
    divider_photo: Optional[str] = None
    intro: Optional[str] = None


@dataclass
class ReportSpec:
    """Everything needed to render one report."""

    title: str
    company_name: str
    fiscal_year: str = ""
    sections: list[SectionSpec] = field(default_factory=list)

    # Brand palette (CSS variable overrides)
    primary: str = "#2E7D32"
    primary_light: str = "#4CAF50"
    primary_pale: str = "#E8F5E9"
    accent: str = "#C8A951"
    dark: str = "#37474F"
    secondary: str = "#795548"
    navy: str = "#1F4E79"

    # Assets
    cover_photo: Optional[str] = None
    logo: Optional[str] = None
    back_cover: Optional[str] = None
    back_cover_photo: Optional[str] = None

    # Output
    output_dir: str = "/tmp/report-output"


@dataclass
class BuildResult:
    """What build_report() returns."""

    html_path: Path
    pdf_path: Path
    page_count: int = 0


async def build_report(spec: ReportSpec) -> BuildResult:
    """Render a report from spec → HTML → PDF.

    Steps:
    1. Render design_system.css.tmpl with the brand palette.
    2. Render report.html.tmpl with the CSS + sections.
    3. Call pdf.render_pdf() to produce the final PDF.
    """
    out = Path(spec.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(ENGINE_DIR)),
        autoescape=False,  # HTML is pre-sanitised; we need raw output
    )

    # 1. Render CSS
    css_tmpl = env.get_template("design_system.css.tmpl")
    css = css_tmpl.render(
        primary=spec.primary,
        primary_light=spec.primary_light,
        primary_pale=spec.primary_pale,
        accent=spec.accent,
        dark=spec.dark,
        secondary=spec.secondary,
        navy=spec.navy,
        company_name=spec.company_name,
    )

    # 2. Render HTML
    html_tmpl = env.get_template("report.html.tmpl")
    sections = [
        {
            "title": s.title,
            "body_html": s.body_html,
            "divider_photo": s.divider_photo,
            "intro": s.intro,
        }
        for s in spec.sections
    ]
    html = html_tmpl.render(
        css=css,
        title=spec.title,
        company_name=spec.company_name,
        fiscal_year=spec.fiscal_year,
        sections=sections,
        cover_photo=spec.cover_photo,
        logo=spec.logo,
        back_cover=spec.back_cover,
        back_cover_photo=spec.back_cover_photo,
    )

    html_path = out / "report.html"
    html_path.write_text(html, encoding="utf-8")

    # 3. Render PDF
    from .pdf import render_pdf

    pdf_path = out / "report.pdf"
    await render_pdf(html_path, pdf_path)

    # 4. Count pages (rough: extract via pdfplumber if available)
    page_count = 0
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
    except ImportError:
        pass

    return BuildResult(html_path=html_path, pdf_path=pdf_path, page_count=page_count)


# ---------------------------------------------------------------------------
# CLI entry point for standalone testing
# ---------------------------------------------------------------------------


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="Build a report from HTML body content")
    parser.add_argument("--title", default="Test Report")
    parser.add_argument("--company", default="Test Company")
    parser.add_argument("--fiscal-year", default="FYE 2025")
    parser.add_argument(
        "--body-html",
        help="Path to an HTML file containing the report body (will be wrapped in a single section)",
    )
    parser.add_argument(
        "--body-text",
        help="Plain text content (wrapped in <p class='body-text'> tags)",
    )
    parser.add_argument("--cover-photo", help="Path to cover photo")
    parser.add_argument("--logo", help="Path to logo image")
    parser.add_argument("--out", default="/tmp/report-output")
    parser.add_argument("--primary", default="#2E7D32")
    parser.add_argument("--accent", default="#C8A951")
    parser.add_argument("--dark", default="#37474F")
    args = parser.parse_args()

    # Build body from provided content
    if args.body_html:
        body = Path(args.body_html).read_text(encoding="utf-8")
    elif args.body_text:
        paragraphs = args.body_text.split("\n\n")
        body = "\n".join(f'<p class="body-text">{p}</p>' for p in paragraphs if p.strip())
    else:
        # Demo content if nothing provided
        body = _demo_body()

    sections = [SectionSpec(title="Main Content", body_html=body)]

    spec = ReportSpec(
        title=args.title,
        company_name=args.company,
        fiscal_year=args.fiscal_year,
        sections=sections,
        primary=args.primary,
        accent=args.accent,
        dark=args.dark,
        cover_photo=args.cover_photo,
        logo=args.logo,
        output_dir=args.out,
    )

    result = asyncio.run(build_report(spec))
    print(f"HTML: {result.html_path}")
    print(f"PDF:  {result.pdf_path}")
    if result.page_count:
        print(f"Pages: {result.page_count}")


def _demo_body() -> str:
    """Generate demo content that exercises the main CSS components."""
    return """
<div class="subsection-title">Executive Summary</div>
<p class="body-text">
  This report presents the sustainability performance and strategic outlook for the fiscal year.
  Our commitment to environmental stewardship, social responsibility, and robust governance
  continues to drive long-term value creation for all stakeholders. The following sections detail
  our progress across key sustainability pillars, supported by quantitative metrics and
  forward-looking targets.
</p>

<div class="card-grid card-grid--3">
  <div class="metric-card">
    <div class="value">42%</div>
    <div class="label">Reduction in Carbon Emissions</div>
    <div class="detail">vs. FY2020 baseline</div>
  </div>
  <div class="metric-card metric-card--green">
    <div class="value">RM 2.1M</div>
    <div class="label">Community Investment</div>
    <div class="detail">+18% year-on-year</div>
  </div>
  <div class="metric-card metric-card--dark">
    <div class="value">Zero</div>
    <div class="label">Fatalities</div>
    <div class="detail">3rd consecutive year</div>
  </div>
</div>

<div class="subsection-title">Environmental Performance</div>
<p class="body-text">
  Our environmental management framework encompasses energy efficiency, water stewardship,
  waste reduction, and biodiversity conservation. We have adopted the Task Force on
  Climate-related Financial Disclosures (TCFD) framework to enhance transparency
  in our climate risk management and reporting.
</p>

<table class="data-table">
  <thead>
    <tr>
      <th>Indicator</th>
      <th>Unit</th>
      <th class="num">FY2024</th>
      <th class="num">FY2025</th>
      <th class="num">Change</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Total GHG Emissions (Scope 1 + 2)</td>
      <td>tCO2e</td>
      <td class="num">12,450</td>
      <td class="num">10,890</td>
      <td class="num bold">-12.5%</td>
    </tr>
    <tr>
      <td>Energy Consumption</td>
      <td>GJ</td>
      <td class="num">185,000</td>
      <td class="num">172,300</td>
      <td class="num bold">-6.9%</td>
    </tr>
    <tr>
      <td>Water Withdrawal</td>
      <td>ML</td>
      <td class="num">45.2</td>
      <td class="num">41.8</td>
      <td class="num bold">-7.5%</td>
    </tr>
    <tr>
      <td>Waste Diverted from Landfill</td>
      <td>%</td>
      <td class="num">68%</td>
      <td class="num">74%</td>
      <td class="num bold">+6pp</td>
    </tr>
    <tr class="total-row">
      <td colspan="2">Renewable Energy Share</td>
      <td class="num">22%</td>
      <td class="num">31%</td>
      <td class="num bold">+9pp</td>
    </tr>
  </tbody>
</table>

<div class="quote-panel">
  <div class="quote-text">"Our long-term success is inextricably linked to the resilience
    of the environment and the well-being of the communities in which we operate."</div>
  <div class="quote-attr">&mdash; Managing Director</div>
</div>

<div class="subsection-title">Social Impact</div>
<p class="body-text">
  We remain committed to creating positive social impact through responsible employment
  practices, community engagement, and supply chain stewardship. Our workforce development
  programmes reached over 2,400 employees during the reporting period.
</p>

<div class="card-grid card-grid--2">
  <div class="card card--accent-green">
    <div class="card-title">Workforce Development</div>
    <div class="card-text">Average 24 hours of training per employee. Leadership development
      programme expanded to mid-level management with 85% completion rate.</div>
  </div>
  <div class="card card--accent-gold">
    <div class="card-title">Community Engagement</div>
    <div class="card-text">12 community programmes across 5 states. 3,200 volunteer hours
      contributed by employees. RM 450,000 in educational scholarships awarded.</div>
  </div>
</div>

<div class="subsection-title">Governance Framework</div>
<p class="body-text">
  Strong governance underpins our sustainability strategy. The Board Sustainability Committee
  meets quarterly to review ESG performance, risk management, and strategic alignment.
  We maintain zero tolerance for corruption and have established comprehensive
  anti-bribery policies in line with the Malaysian Anti-Corruption Commission Act 2009.
</p>

<div class="target-grid">
  <div class="target-card">
    <div class="target-area">Carbon Neutral</div>
    <div class="target-text">Achieve carbon neutrality in Scope 1 and 2 emissions by 2030</div>
  </div>
  <div class="target-card target-card--gold">
    <div class="target-area">Zero Waste</div>
    <div class="target-text">90% waste diversion rate from landfill by 2028</div>
  </div>
  <div class="target-card target-card--navy">
    <div class="target-area">Board Diversity</div>
    <div class="target-text">Achieve 40% women representation on the Board by 2027</div>
  </div>
  <div class="target-card">
    <div class="target-area">Community Investment</div>
    <div class="target-text">Invest RM 10M in community programmes over the next 5 years</div>
  </div>
</div>

<div class="subsection-title">Forward-Looking Statement</div>
<p class="body-text">
  As we look ahead, our sustainability strategy remains anchored in the United Nations
  Sustainable Development Goals and the Bursa Malaysia Sustainability Reporting Framework.
  We are committed to continuous improvement in our ESG performance and transparent
  disclosure of our progress to all stakeholders.
</p>

<div class="signature-block">
  <div class="signature-name">Mr. Lee Kim Seng</div>
  <div class="signature-title">Managing Director</div>
  <div class="signature-company">Test Company</div>
</div>
"""


if __name__ == "__main__":
    _cli()
