"""SVG thumbnail generator for proposal slides."""
from html import escape

PALETTES = {
    "小売": ("#0E2A47", "#F4A261", "#F8FAFC"),
    "製造": ("#1F2937", "#60A5FA", "#F1F5F9"),
    "広告": ("#2D1B69", "#F472B6", "#FAF5FF"),
    "EC": ("#064E3B", "#FBBF24", "#ECFDF5"),
    "金融": ("#1E1B4B", "#A78BFA", "#EEF2FF"),
    "通信": ("#0F172A", "#22D3EE", "#F1F5F9"),
    "ヘルスケア": ("#134E4A", "#FB7185", "#F0FDFA"),
    "人材": ("#3F1D1D", "#F59E0B", "#FEF2F2"),
}
DEFAULT_PALETTE = ("#0F172A", "#38BDF8", "#F1F5F9")


def _wrap(text: str, per_line: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    rest = text
    while rest and len(lines) < max_lines:
        lines.append(rest[:per_line])
        rest = rest[per_line:]
    if rest and lines:
        lines[-1] = lines[-1][: per_line - 1] + "…"
    return lines


def _chart(graph_type: str, accent: str) -> str:
    if graph_type == "棒グラフ":
        bars = [60, 95, 75, 130, 110, 160]
        return "".join(
            f'<rect x="{540 + i * 40}" y="{400 - h}" width="28" height="{h}" rx="3" '
            f'fill="{accent}" opacity="{0.5 + i * 0.08:.2f}"/>'
            for i, h in enumerate(bars)
        )
    if graph_type == "折れ線":
        pts = [(540, 380), (600, 320), (660, 340), (720, 260), (780, 220), (840, 180)]
        path = " ".join(f"{'M' if i == 0 else 'L'} {x} {y}" for i, (x, y) in enumerate(pts))
        dots = "".join(f'<circle cx="{x}" cy="{y}" r="5" fill="{accent}"/>' for x, y in pts)
        return f'<path d="{path}" stroke="{accent}" stroke-width="3" fill="none"/>{dots}'
    if graph_type == "円グラフ":
        return (
            f'<circle cx="720" cy="320" r="100" fill="{accent}" opacity="0.25"/>'
            f'<path d="M720,320 L720,220 A100,100 0 0,1 814,355 Z" fill="{accent}" opacity="0.95"/>'
            f'<path d="M720,320 L814,355 A100,100 0 0,1 660,407 Z" fill="{accent}" opacity="0.55"/>'
        )
    if graph_type == "ファネル":
        return "".join(
            f'<polygon points="{540 + i * 12},{230 + i * 40} {860 - i * 12},{230 + i * 40} '
            f'{840 - i * 12},{260 + i * 40} {560 + i * 12},{260 + i * 40}" '
            f'fill="{accent}" opacity="{0.85 - i * 0.18:.2f}"/>'
            for i in range(4)
        )
    if graph_type == "散布図":
        return "".join(
            f'<circle cx="{540 + (i * 37) % 310}" cy="{220 + (i * 53) % 180}" r="6" '
            f'fill="{accent}" opacity="{0.4 + (i % 5) * 0.12:.2f}"/>'
            for i in range(24)
        )
    if graph_type == "テーブル":
        rows = "".join(
            f'<line x1="540" y1="{240 + r * 35}" x2="860" y2="{240 + r * 35}" '
            f'stroke="{accent}" stroke-width="1" opacity="0.4"/>'
            for r in range(5)
        )
        cols = "".join(
            f'<line x1="{540 + c * 107}" y1="240" x2="{540 + c * 107}" y2="410" '
            f'stroke="{accent}" stroke-width="1" opacity="0.4"/>'
            for c in range(3)
        )
        return f'<rect x="540" y="240" width="320" height="170" fill="{accent}" opacity="0.08"/>{rows}{cols}'
    if graph_type == "ロードマップ":
        return "".join(
            f'<rect x="{530 + i * 80}" y="300" width="70" height="40" rx="4" '
            f'fill="{accent}" opacity="{0.4 + i * 0.15:.2f}"/>'
            f'<text x="{565 + i * 80}" y="325" text-anchor="middle" fill="#0f172a" '
            f'font-size="14" font-weight="700">Q{i + 1}</text>'
            for i in range(4)
        )
    return (
        f'<rect x="540" y="240" width="320" height="170" fill="{accent}" opacity="0.18" rx="6"/>'
        f'<rect x="560" y="270" width="280" height="14" fill="{accent}" opacity="0.5" rx="2"/>'
        f'<rect x="560" y="300" width="220" height="14" fill="{accent}" opacity="0.4" rx="2"/>'
        f'<rect x="560" y="330" width="260" height="14" fill="{accent}" opacity="0.35" rx="2"/>'
        f'<rect x="560" y="360" width="180" height="14" fill="{accent}" opacity="0.3" rx="2"/>'
    )


def _layout_frame(layout: str, accent: str) -> str:
    if layout == "左右比較":
        return (
            f'<line x1="480" y1="200" x2="480" y2="440" stroke="{accent}" '
            f'stroke-width="1.5" stroke-dasharray="6 6" opacity="0.5"/>'
        )
    if layout == "上下分割":
        return (
            f'<line x1="80" y1="320" x2="880" y2="320" stroke="{accent}" '
            f'stroke-width="1.5" stroke-dasharray="6 6" opacity="0.5"/>'
        )
    if layout == "4象限":
        return (
            f'<line x1="480" y1="200" x2="480" y2="440" stroke="{accent}" stroke-width="1.5" stroke-dasharray="6 6" opacity="0.45"/>'
            f'<line x1="80" y1="320" x2="880" y2="320" stroke="{accent}" stroke-width="1.5" stroke-dasharray="6 6" opacity="0.45"/>'
        )
    if layout == "Before/After":
        return (
            f'<rect x="80" y="200" width="380" height="240" fill="{accent}" opacity="0.06" rx="6"/>'
            f'<rect x="480" y="200" width="380" height="240" fill="{accent}" opacity="0.14" rx="6"/>'
        )
    if layout == "ロードマップ":
        return f'<line x1="80" y1="320" x2="880" y2="320" stroke="{accent}" stroke-width="2" opacity="0.5"/>'
    return ""


def render_thumbnail_svg(slide: dict) -> str:
    bg, accent, text_color = PALETTES.get(slide["industry"], DEFAULT_PALETTE)
    title_lines = _wrap(slide["slideTitle"], 18, 2)
    subtitle = f'{slide["industry"]} / {slide["proposalType"]}'
    meta = f'{slide["fileName"]}  p.{slide["pageNo"]}'
    title_svg = "\n  ".join(
        f'<text x="40" y="{130 + i * 52}" fill="{text_color}" '
        f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="42" font-weight="700">{escape(line)}</text>'
        for i, line in enumerate(title_lines)
    )
    sub_y = 130 + len(title_lines) * 52 + 18
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540" preserveAspectRatio="xMidYMid slice">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{bg}"/>'
        '<stop offset="100%" stop-color="#000000" stop-opacity="0.45"/>'
        '</linearGradient></defs>'
        '<rect width="960" height="540" fill="url(#g)"/>'
        f'<rect x="0" y="0" width="6" height="540" fill="{accent}"/>'
        f'<text x="40" y="64" fill="{accent}" font-family="ui-sans-serif, system-ui, sans-serif" '
        f'font-size="14" font-weight="700" letter-spacing="3">{escape(subtitle.upper())}</text>'
        f'{title_svg}'
        f'<text x="40" y="{sub_y}" fill="{text_color}" opacity="0.6" '
        f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="16">'
        f'{escape(slide["layoutType"])} ・ {escape(slide["graphType"])}</text>'
        f'{_layout_frame(slide["layoutType"], accent)}'
        f'{_chart(slide["graphType"], accent)}'
        f'<text x="40" y="500" fill="{text_color}" opacity="0.5" font-family="ui-monospace, monospace" '
        f'font-size="14">{escape(meta)}</text>'
        f'<text x="920" y="500" text-anchor="end" fill="{accent}" opacity="0.8" '
        f'font-family="ui-monospace, monospace" font-size="14" font-weight="700">'
        f'SLIDE {slide["pageNo"]:02d}</text>'
        '</svg>'
    )
