"""报告子包：绩效指标、Markdown 报告、结构化 JSON."""

from solidrock.report.json_report import render_result_json, write_result_json
from solidrock.report.markdown import render_report_markdown, write_report_markdown
from solidrock.report.metrics import compute_metrics, format_metrics, yearly_returns

__all__ = [
    "compute_metrics",
    "format_metrics",
    "render_report_markdown",
    "render_result_json",
    "write_report_markdown",
    "write_result_json",
    "yearly_returns",
]
