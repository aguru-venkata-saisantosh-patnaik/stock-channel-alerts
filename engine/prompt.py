"""Fundamental-check prompt to paste into an AI assistant that has web search turned on."""

TEMPLATE = """Please research {name} ({ticker}) as a possible long-term investment, using current sources from the web.

Context: the stock has dropped far below its own 10-year price trend, and I handle the timing of the entry myself. I only need your help with the business side, specifically these two questions:

1. Temporary or lasting? Explain why the share price has fallen. Is the cause something the company can recover from (market panic, a weak quarter, sector rotation, a fixable problem) or does it point to permanent damage to the business (lost competitive position, broken business model, structural decline)?

2. Can it survive? Judge how financially strong the company is: debt and when it falls due, cash and liquidity, free cash flow, interest coverage, and anything that could hurt existing shareholders even without bankruptcy (big share dilution, a dividend cut forced by debt, covenant problems). Use the measures that fit the industry. For banks and insurers, for example, look at capital ratios, liquidity and asset quality. Rate the bankruptcy risk as Low, Moderate, High or Unclear, and explain why.

How to research: start from the most recent annual report and quarterly filing (on SEC EDGAR or the investor-relations site), the latest earnings release, and reliable news about the decline. Treat what management says as claims to check, not as facts.

How to answer: write for a beginner, briefly explaining any jargon, in about 500-800 words. List your sources at the end. Leave out chart or technical analysis.

End with exactly one verdict and two or three sentences explaining it:
- PASSES both checks
- FAILS one or both checks
- NOT ENOUGH EVIDENCE to decide
"""


def build(name: str, ticker: str) -> str:
    return TEMPLATE.format(name=name, ticker=ticker)
