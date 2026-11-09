"""Synthetic handwritten delivery tickets: generation, rendering, scanning and offline evaluation.

Everything here is fictional: suppliers, projects, people, numbers and layouts are
invented, and the handwriting is drawn from vector strokes defined in this package.
Nothing is downloaded and no model is called.

Results produced with this data are an ENGINEERING BENCHMARK. They test the
splitter, the pipeline, the review workflow and the financial math. They are not
evidence of how well anything reads real handwriting. See
docs/engineering/synthetic-tickets.md.
"""

GENERATOR_VERSION = "1.0.0"
STAMP = "SYNTHETIC — engineering benchmark, not real-world accuracy"
RECORDS_SCHEMA = "ticketflow.synthetic.records/1"
LABELS_SCHEMA = "ticketflow.synthetic.labels/1"
PAGES_SCHEMA = "ticketflow.synthetic.pages/1"
DATASET_SCHEMA = "ticketflow.synthetic.dataset/1"
REPORT_SCHEMA = "ticketflow.synthetic.report/1"
