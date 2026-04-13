"""Pre-built demo report content — no LLM calls, no wiki, no uploads.

Used by /report demo to showcase the rendering engine and all CSS components.
Anyone can view and edit the demo. The bot resets it periodically.
"""

from __future__ import annotations

from .pipeline import ReportSpec, SectionSpec

DEMO_COMPANY = "Meridian Group Berhad"
DEMO_TITLE = "Sustainability Statement FYE 2025"
DEMO_FISCAL_YEAR = "FYE 2025"


def build_demo_spec(output_dir: str = "/tmp/report-demo") -> ReportSpec:
    """Return a fully-populated ReportSpec with demo content."""
    return ReportSpec(
        title=DEMO_TITLE,
        company_name=DEMO_COMPANY,
        fiscal_year=DEMO_FISCAL_YEAR,
        sections=_demo_sections(),
        output_dir=output_dir,
        primary="#2E7D32",
        accent="#C8A951",
        dark="#37474F",
    )


def _demo_sections() -> list[SectionSpec]:
    return [
        SectionSpec(
            title="Managing Director's Statement",
            body_html=_section_md_statement(),
        ),
        SectionSpec(
            title="Environmental Performance",
            body_html=_section_environment(),
        ),
        SectionSpec(
            title="Social Impact & Workforce",
            body_html=_section_social(),
        ),
        SectionSpec(
            title="Governance & Risk Management",
            body_html=_section_governance(),
        ),
        SectionSpec(
            title="Forward-Looking Targets",
            body_html=_section_targets(),
        ),
    ]


def _section_md_statement() -> str:
    return """
<div class="two-col">
  <div class="col-left">
    <div class="sidebar-panel">
      <div class="quote-text">"Our long-term success is inextricably linked to the resilience
        of the environment and the well-being of the communities in which we operate."</div>
      <div class="accent-line"></div>
      <div style="font-size: var(--small-size); opacity: 0.8;">Mr. Ahmad Razif bin Ismail</div>
      <div style="font-size: var(--caption-size); opacity: 0.6;">Managing Director</div>
    </div>
  </div>
  <div class="col-right">
    <p class="body-text drop-cap">
      On behalf of the Board of Directors, I am pleased to present Meridian Group Berhad's
      Sustainability Statement for the financial year ended 31 December 2025. This year marks
      a pivotal moment in our sustainability journey as we have exceeded several key environmental
      targets ahead of schedule, while simultaneously strengthening our social impact programmes
      across all operating states.
    </p>
    <p class="body-text">
      Our commitment to the United Nations Sustainable Development Goals remains unwavering.
      During the reporting period, we achieved a 42% reduction in Scope 1 and Scope 2 greenhouse
      gas emissions against our FY2020 baseline — surpassing our 2025 interim target of 35%.
      This was driven by the commissioning of our rooftop solar installations across three
      manufacturing facilities in Selangor and Johor, which now provide 31% of our total
      energy consumption from renewable sources.
    </p>
    <p class="body-text">
      In the social dimension, our workforce development programmes reached 2,400 employees,
      with an average of 24 training hours per employee — a 15% increase from the prior year.
      Community investment totalled RM 2.1 million, directed towards educational scholarships,
      environmental conservation programmes, and skills training for underserved communities
      in Pahang and Kelantan.
    </p>
    <p class="body-text">
      Looking ahead, we have set an ambitious target to achieve carbon neutrality in Scope 1
      and Scope 2 emissions by 2030. The Board Sustainability Committee will continue to
      oversee our progress quarterly, ensuring accountability and transparency in our
      reporting to all stakeholders.
    </p>

    <div class="signature-block">
      <div class="signature-name">Mr. Ahmad Razif bin Ismail</div>
      <div class="signature-title">Managing Director</div>
      <div class="signature-company">Meridian Group Berhad</div>
    </div>
  </div>
</div>
"""


def _section_environment() -> str:
    return """
<p class="body-text">
  Our environmental management framework encompasses energy efficiency, water stewardship,
  waste reduction, and biodiversity conservation. We adopted the Task Force on Climate-related
  Financial Disclosures (TCFD) framework in FY2023 and continue to enhance transparency
  in our climate risk management and reporting.
</p>

<div class="card-grid card-grid--3">
  <div class="metric-card">
    <div class="value">42%</div>
    <div class="label">Reduction in GHG Emissions</div>
    <div class="detail">vs. FY2020 baseline</div>
  </div>
  <div class="metric-card metric-card--green">
    <div class="value">31%</div>
    <div class="label">Renewable Energy Share</div>
    <div class="detail">+9pp year-on-year</div>
  </div>
  <div class="metric-card metric-card--dark">
    <div class="value">74%</div>
    <div class="label">Waste Diverted from Landfill</div>
    <div class="detail">Target: 90% by 2028</div>
  </div>
</div>

<div class="subsection-title">Emissions & Energy Performance</div>

<table class="data-table">
  <thead>
    <tr>
      <th>Indicator</th>
      <th>Unit</th>
      <th class="num">FY2023</th>
      <th class="num">FY2024</th>
      <th class="num">FY2025</th>
      <th class="num">Change</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Total GHG Emissions (Scope 1 + 2)</td>
      <td>tCO2e</td>
      <td class="num">14,200</td>
      <td class="num">12,450</td>
      <td class="num">10,890</td>
      <td class="num bold">-12.5%</td>
    </tr>
    <tr>
      <td>Scope 1 (Direct)</td>
      <td>tCO2e</td>
      <td class="num">8,100</td>
      <td class="num">7,250</td>
      <td class="num">6,480</td>
      <td class="num bold">-10.6%</td>
    </tr>
    <tr>
      <td>Scope 2 (Indirect — Electricity)</td>
      <td>tCO2e</td>
      <td class="num">6,100</td>
      <td class="num">5,200</td>
      <td class="num">4,410</td>
      <td class="num bold">-15.2%</td>
    </tr>
    <tr>
      <td>Total Energy Consumption</td>
      <td>GJ</td>
      <td class="num">198,000</td>
      <td class="num">185,000</td>
      <td class="num">172,300</td>
      <td class="num bold">-6.9%</td>
    </tr>
    <tr>
      <td>Renewable Energy Generation</td>
      <td>GJ</td>
      <td class="num">31,700</td>
      <td class="num">40,700</td>
      <td class="num">53,413</td>
      <td class="num bold">+31.2%</td>
    </tr>
    <tr>
      <td>Energy Intensity</td>
      <td>GJ/RM mil revenue</td>
      <td class="num">42.1</td>
      <td class="num">38.5</td>
      <td class="num">34.8</td>
      <td class="num bold">-9.6%</td>
    </tr>
    <tr class="total-row">
      <td colspan="2">Renewable Energy Share</td>
      <td class="num">16%</td>
      <td class="num">22%</td>
      <td class="num">31%</td>
      <td class="num bold">+9pp</td>
    </tr>
  </tbody>
</table>

<div class="subsection-title">Water & Waste Management</div>

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
      <td>Total Water Withdrawal</td>
      <td>ML</td>
      <td class="num">45.2</td>
      <td class="num">41.8</td>
      <td class="num bold">-7.5%</td>
    </tr>
    <tr>
      <td>Water Recycled</td>
      <td>ML</td>
      <td class="num">12.4</td>
      <td class="num">15.1</td>
      <td class="num bold">+21.8%</td>
    </tr>
    <tr>
      <td>Total Waste Generated</td>
      <td>tonnes</td>
      <td class="num">2,340</td>
      <td class="num">2,180</td>
      <td class="num bold">-6.8%</td>
    </tr>
    <tr class="total-row">
      <td colspan="2">Waste Diversion Rate</td>
      <td class="num">68%</td>
      <td class="num">74%</td>
      <td class="num bold">+6pp</td>
    </tr>
  </tbody>
</table>

<div class="quote-panel">
  <div class="quote-text">"We have invested RM 4.2 million in solar installations across three facilities,
    generating 53,413 GJ of clean energy — equivalent to powering 1,200 homes for a year."</div>
  <div class="quote-attr">&mdash; Dato' Ir. Wan Haziq, Group Chief Operating Officer</div>
</div>

<div class="callout-box">
  <div class="callout-title">TCFD Alignment</div>
  <div class="callout-text">Our climate risk assessment covers physical risks (flooding, extreme heat)
    and transition risks (carbon pricing, regulatory changes). Full TCFD disclosure is available in
    our Annual Report, Section 7.</div>
</div>
"""


def _section_social() -> str:
    return """
<p class="body-text">
  We remain committed to creating positive social impact through responsible employment
  practices, community engagement, and supply chain stewardship. Our workforce development
  programmes reached over 2,400 employees during the reporting period.
</p>

<div class="card-grid card-grid--2">
  <div class="card card--accent-green">
    <div class="card-title">Workforce Development</div>
    <div class="card-text">Average 24 hours of training per employee. Leadership development
      programme expanded to mid-level management with 85% completion rate. Zero fatalities
      for the third consecutive year.</div>
  </div>
  <div class="card card--accent-gold">
    <div class="card-title">Community Engagement</div>
    <div class="card-text">12 community programmes across 5 states. 3,200 volunteer hours
      contributed by employees. RM 450,000 in educational scholarships awarded to 48 students
      from B40 families.</div>
  </div>
</div>

<div class="subsection-title">Workforce Composition</div>

<table class="data-table data-table--compact">
  <thead>
    <tr>
      <th>Category</th>
      <th class="num">Male</th>
      <th class="num">Female</th>
      <th class="num">Total</th>
      <th class="num">% Female</th>
    </tr>
  </thead>
  <tbody>
    <tr class="sub-header">
      <td colspan="5">By Management Level</td>
    </tr>
    <tr>
      <td>Board of Directors</td>
      <td class="num">5</td>
      <td class="num">3</td>
      <td class="num">8</td>
      <td class="num bold">37.5%</td>
    </tr>
    <tr>
      <td>Senior Management</td>
      <td class="num">18</td>
      <td class="num">7</td>
      <td class="num">25</td>
      <td class="num bold">28.0%</td>
    </tr>
    <tr>
      <td>Middle Management</td>
      <td class="num">89</td>
      <td class="num">64</td>
      <td class="num">153</td>
      <td class="num bold">41.8%</td>
    </tr>
    <tr>
      <td>Executive / Supervisory</td>
      <td class="num">320</td>
      <td class="num">280</td>
      <td class="num">600</td>
      <td class="num bold">46.7%</td>
    </tr>
    <tr>
      <td>Non-Executive</td>
      <td class="num">1,050</td>
      <td class="num">564</td>
      <td class="num">1,614</td>
      <td class="num bold">34.9%</td>
    </tr>
    <tr class="total-row">
      <td>Total Workforce</td>
      <td class="num">1,482</td>
      <td class="num">918</td>
      <td class="num">2,400</td>
      <td class="num bold">38.3%</td>
    </tr>
  </tbody>
</table>

<div class="subsection-title">Health & Safety</div>

<div class="card-grid card-grid--4">
  <div class="metric-card">
    <div class="value">0</div>
    <div class="label">Fatalities</div>
    <div class="detail">3rd consecutive year</div>
  </div>
  <div class="metric-card">
    <div class="value">1.2</div>
    <div class="label">LTIFR</div>
    <div class="detail">Lost Time Injury Frequency Rate</div>
  </div>
  <div class="metric-card metric-card--green">
    <div class="value">98%</div>
    <div class="label">Safety Training Coverage</div>
    <div class="detail">All operational staff</div>
  </div>
  <div class="metric-card">
    <div class="value">12</div>
    <div class="label">Safety Audits</div>
    <div class="detail">Across all facilities</div>
  </div>
</div>

<div class="zero-badges">
  <div class="zero-badge">Zero Fatalities</div>
  <div class="zero-badge">Zero Environmental Fines</div>
  <div class="zero-badge">Zero Corruption Cases</div>
</div>

<div class="subsection-title">Community Investment</div>

<div class="numbered-cards">
  <div class="numbered-card">
    <div class="num-circle">1</div>
    <div class="num-text"><strong>STEM Education Programme</strong> — Partnered with 8 secondary schools
      in Pahang to deliver robotics and coding workshops reaching 1,200 students.</div>
  </div>
  <div class="numbered-card">
    <div class="num-circle">2</div>
    <div class="num-text"><strong>Mangrove Restoration</strong> — Planted 15,000 mangrove seedlings
      along the Kuantan river estuary with 400 employee volunteers and local communities.</div>
  </div>
  <div class="numbered-card">
    <div class="num-circle">3</div>
    <div class="num-text"><strong>Skills Training for B40</strong> — Funded technical certification
      courses for 120 participants from underserved communities in Kelantan and Terengganu.</div>
  </div>
</div>
"""


def _section_governance() -> str:
    return """
<p class="body-text">
  Strong governance underpins our sustainability strategy. The Board Sustainability Committee
  meets quarterly to review ESG performance, risk management, and strategic alignment.
  We maintain zero tolerance for corruption and have established comprehensive
  anti-bribery policies in line with the Malaysian Anti-Corruption Commission Act 2009.
</p>

<div class="subsection-title">Board Sustainability Committee</div>

<div class="committee-cards">
  <div class="committee-card">
    <div class="committee-name">Dato' Seri Hj. Kamal</div>
    <div class="committee-detail">Chairman (Independent)</div>
  </div>
  <div class="committee-card">
    <div class="committee-name">Puan Siti Nurhaliza</div>
    <div class="committee-detail">Member (Independent)</div>
  </div>
  <div class="committee-card">
    <div class="committee-name">Mr. Ahmad Razif</div>
    <div class="committee-detail">Member (Executive)</div>
  </div>
</div>

<div class="subsection-title">Anti-Corruption & Ethics</div>

<div class="zt-card">
  <div class="zt-icon">!</div>
  <div class="zt-content">
    <div class="zt-category">Zero Tolerance: Bribery & Corruption</div>
    <div class="zt-position">All employees, directors, contractors, and business partners</div>
  </div>
</div>
<div class="zt-card">
  <div class="zt-icon">!</div>
  <div class="zt-content">
    <div class="zt-category">Zero Tolerance: Fraud & Misrepresentation</div>
    <div class="zt-position">Includes financial reporting, ESG data, and procurement</div>
  </div>
</div>

<div class="subsection-title">Material Sustainability Topics</div>

<div class="material-topics-list">
  <span class="material-topic-tag">Climate Change</span>
  <span class="material-topic-tag">Energy Management</span>
  <span class="material-topic-tag">Water Stewardship</span>
  <span class="material-topic-tag">Waste Management</span>
  <span class="material-topic-tag">Occupational Safety</span>
  <span class="material-topic-tag">Workforce Diversity</span>
  <span class="material-topic-tag">Community Investment</span>
  <span class="material-topic-tag">Supply Chain Ethics</span>
  <span class="material-topic-tag">Data Privacy</span>
  <span class="material-topic-tag">Anti-Corruption</span>
  <span class="material-topic-tag">Board Independence</span>
  <span class="material-topic-tag">Human Rights</span>
</div>

<div class="subsection-title">Certifications & Standards</div>

<div class="cert-badges">
  <div class="cert-badge">
    <div class="cert-icon">🏗️</div>
    <div class="cert-info">
      <div class="cert-name">ISO 14001:2015</div>
      <div class="cert-detail">Environmental Management</div>
    </div>
  </div>
  <div class="cert-badge">
    <div class="cert-icon">⚙️</div>
    <div class="cert-info">
      <div class="cert-name">ISO 45001:2018</div>
      <div class="cert-detail">Occupational Health & Safety</div>
    </div>
  </div>
  <div class="cert-badge">
    <div class="cert-icon">📊</div>
    <div class="cert-info">
      <div class="cert-name">GRI Standards 2021</div>
      <div class="cert-detail">Sustainability Reporting</div>
    </div>
  </div>
</div>
"""


def _section_targets() -> str:
    return """
<p class="body-text">
  Our forward-looking targets are aligned with the Paris Agreement, Malaysia's Enhanced NDC,
  and the Bursa Malaysia Sustainability Reporting Framework. The Board reviews progress
  against these targets quarterly.
</p>

<div class="target-grid">
  <div class="target-card">
    <div class="target-area">Carbon Neutral by 2030</div>
    <div class="target-text">Achieve net-zero Scope 1 and Scope 2 emissions through renewable energy
      transition, energy efficiency improvements, and verified carbon offsets for residual emissions.</div>
  </div>
  <div class="target-card target-card--gold">
    <div class="target-area">90% Waste Diversion by 2028</div>
    <div class="target-text">Divert 90% of operational waste from landfill through recycling, composting,
      and circular economy partnerships with downstream processors.</div>
  </div>
  <div class="target-card target-card--navy">
    <div class="target-area">40% Board Diversity by 2027</div>
    <div class="target-text">Achieve at least 40% women representation on the Board of Directors
      through targeted director succession planning and governance reforms.</div>
  </div>
  <div class="target-card">
    <div class="target-area">RM 10M Community Investment</div>
    <div class="target-text">Invest RM 10 million in community development programmes over the next
      5 years, focusing on STEM education, environmental conservation, and B40 skills training.</div>
  </div>
</div>

<div class="subsection-title">Progress Tracking</div>

<table class="data-table">
  <thead>
    <tr>
      <th>Target</th>
      <th>Deadline</th>
      <th class="num">Baseline</th>
      <th class="num">Current</th>
      <th class="num">Target</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>GHG Reduction (vs FY2020)</td>
      <td>2030</td>
      <td class="num">18,750 tCO2e</td>
      <td class="num">10,890 tCO2e</td>
      <td class="num">0 tCO2e</td>
      <td><span class="pillar-badge pillar-badge--env">On Track</span></td>
    </tr>
    <tr>
      <td>Renewable Energy Share</td>
      <td>2028</td>
      <td class="num">8%</td>
      <td class="num">31%</td>
      <td class="num">60%</td>
      <td><span class="pillar-badge pillar-badge--env">On Track</span></td>
    </tr>
    <tr>
      <td>Waste Diversion Rate</td>
      <td>2028</td>
      <td class="num">52%</td>
      <td class="num">74%</td>
      <td class="num">90%</td>
      <td><span class="pillar-badge pillar-badge--env">On Track</span></td>
    </tr>
    <tr>
      <td>Board Gender Diversity</td>
      <td>2027</td>
      <td class="num">25%</td>
      <td class="num">37.5%</td>
      <td class="num">40%</td>
      <td><span class="pillar-badge pillar-badge--social">Near Target</span></td>
    </tr>
    <tr>
      <td>Training Hours per Employee</td>
      <td>2026</td>
      <td class="num">16 hrs</td>
      <td class="num">24 hrs</td>
      <td class="num">30 hrs</td>
      <td><span class="pillar-badge pillar-badge--social">On Track</span></td>
    </tr>
  </tbody>
</table>

<div class="callout-box">
  <div class="callout-title">Assurance Statement</div>
  <div class="callout-text">Selected environmental and safety KPIs in this Statement have been
    independently verified by SIRIM QAS International Sdn Bhd in accordance with ISAE 3000
    (Revised). The full limited assurance report is available upon request.</div>
</div>
"""
