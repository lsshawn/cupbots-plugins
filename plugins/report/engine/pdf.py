"""Render HTML report to PDF via Playwright (headless Chromium).

One function, one browser launch, one page.pdf() call.
No per-page rendering, no PNG stitching, no PIL.
"""

from pathlib import Path


async def render_pdf(html_path: Path, out_path: Path) -> Path:
    """Render a single HTML file to an A4 PDF using CSS Paged Media.

    The HTML must contain @page rules for pagination, margins,
    headers, and footers. Playwright's page.pdf() delegates to
    Chrome's built-in print-to-PDF which respects @page fully.

    Args:
        html_path: Path to the rendered report HTML.
        out_path:  Where to write the PDF.

    Returns:
        The out_path (for chaining).
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(
            f"file://{html_path.resolve()}",
            wait_until="networkidle",
        )
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            # @page owns all margins — don't override
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        await browser.close()

    return out_path
