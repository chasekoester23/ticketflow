"""Extractors for the offline synthetic evaluation, behind TicketFlow's own
Extractor interface (extract(image) -> (ExtractedTicket, raw dict)).

None of these read the image. They recognise which crop TicketFlow made (by a
hash of its pixels, from the same splitter run beforehand) and answer from the
labels. So their results test the PIPELINE, the review workflow and the money
math, not reading accuracy:

  oracle            the label's answer for that slot: should score 100%.
  oracle-crop-aware the label's answer, but only for fields whose box lies inside
                    the crop TicketFlow actually made: what a perfect reader could
                    see. The gap to `oracle` is what the splitter costs.
  noisy-simulated   the oracle plus a seeded error model: ambiguous characters
                    misread as their look-alike, random digit slips that grow with
                    poor handwriting, dropped fields, and guesses at unreadable ones.
                    Every planted error is recorded, so counts can be checked.
  gemini            the real paid provider. Refuses to run without an explicit
                    --i-authorize-paid-calls. Never used in tests.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PIL import Image

from . import geometry as g
from .glyphs import AMBIGUOUS
from .util import rng

SIX = ("ticket_number", "delivery_date", "material_type", "tonnage", "supplier", "job_location")
OURS = {"ticket_number": "ticket_number", "delivery_date": "delivery_date", "material_type": "material",
        "tonnage": "net_tons", "supplier": "supplier", "job_location": "job_location"}
DIGIT_SLIPS = {"0": "8", "1": "7", "2": "7", "3": "8", "4": "9", "5": "6", "6": "5", "7": "1", "8": "3", "9": "4"}

DESCRIPTIONS = {
    "oracle": "Returns the labels. A pipeline and financial-math test, not OCR accuracy.",
    "oracle-crop-aware": "Returns the labels for fields fully inside TicketFlow's crop. Measures what the "
                         "splitter's crops cost a perfect reader; not OCR accuracy.",
    "noisy-simulated": "Labels plus a seeded, recorded error model. Exercises review, correction and approval "
                       "metrics; its error rates are invented, not OCR accuracy.",
    "gemini": "The paid Gemini provider. Real model output on synthetic handwriting; still not real-world accuracy.",
}


def crop_key(image: Image.Image) -> str:
    return hashlib.sha256(f"{image.mode}{image.size}".encode() + image.tobytes()).hexdigest()


@dataclass
class SlotInfo:
    file: str
    page: int
    position: int
    crop_box: tuple  # in rendered-page px, as TicketFlow cut it
    scale: float  # rendered px per synthetic page px
    expected: dict | None  # the six fields, None when the slot holds no ticket
    ticket: dict | None = None  # labels.json entry
    slot: dict | None = None  # pages.json slot


class ExtractionRefused(RuntimeError):
    pass


class OracleExtractor:
    name = "oracle"

    def __init__(self, slots_by_key: dict[str, SlotInfo]):
        self.slots_by_key = slots_by_key
        self.calls = 0
        self.unrecognised = 0

    def lookup(self, image: Image.Image) -> SlotInfo:
        from app.services.extraction import ExtractionError
        info = self.slots_by_key.get(crop_key(image))
        if info is None:
            self.unrecognised += 1
            raise ExtractionError("Synthetic oracle didn't recognise this crop")
        return info

    def answer(self, info: SlotInfo) -> dict:
        return dict(info.expected) if info.expected else {f: None for f in SIX}

    def extract(self, image: Image.Image):
        from app.services.extraction import validated_ticket
        self.calls += 1
        raw = self.answer(self.lookup(image))
        return validated_ticket(raw), raw


class CropAwareOracle(OracleExtractor):
    name = "oracle-crop-aware"
    FIELD_INSIDE = 0.98

    def __init__(self, slots_by_key):
        super().__init__(slots_by_key)
        self.hidden = 0

    def answer(self, info: SlotInfo) -> dict:
        raw = super().answer(info)
        if not info.slot:
            return raw
        x0, y0, x1, y1 = (v / info.scale for v in info.crop_box)
        for f in SIX:
            quad = [tuple(p) for p in info.slot["fields"][OURS[f]]["quad"]]
            if raw[f] is not None and g.inside_fraction(quad, (x0, y0, x1, y1)) < self.FIELD_INSIDE:
                raw[f] = None
                self.hidden += 1
        return raw


DEFAULT_ERROR_MODEL = {
    "drop": 0.03,  # a readable field returned empty
    "ambiguous_misread": 0.5,  # each ambiguous character read as its look-alike
    "digit_slip_printed": 0.001, "digit_slip_clear": 0.004, "digit_slip_degraded": 0.015,
    "digit_slip_poor": 0.04, "digit_slip_ambiguous": 0.015,
    "guess_unreadable": 0.2,  # something invented for a field that can't be read
}


@dataclass
class Planted:
    ticket_id: str
    field: str
    kind: str  # dropped | misread | guessed
    expected: str | None
    returned: str | None


class NoisySimulatedExtractor(OracleExtractor):
    name = "noisy-simulated"

    def __init__(self, slots_by_key, seed: int = 0, model: dict | None = None):
        super().__init__(slots_by_key)
        self.seed = seed
        self.model = {**DEFAULT_ERROR_MODEL, **(model or {})}
        unknown = set(self.model) - set(DEFAULT_ERROR_MODEL)
        if unknown:
            raise ValueError(f"Unknown error-model settings: {sorted(unknown)}")
        self.planted: dict[tuple, Planted] = {}  # keyed by slot and field: a retry overwrites, never double counts

    def _read_text(self, f: str, written: str, label: dict, r) -> str:
        chars = list(written)
        amb = {a["index"] for a in label["ambiguous_chars"]}
        slip = self.model.get("digit_slip_" + label["legibility"], 0.0)
        for i, c in enumerate(chars):
            if i in amb and r.random() < self.model["ambiguous_misread"]:
                chars[i] = AMBIGUOUS[c]
            elif c.isdigit() and r.random() < slip:
                chars[i] = DIGIT_SLIPS[c]
        return "".join(chars)

    @staticmethod
    def _as_output(f: str, text: str, label: dict, fmt: str | None):
        """Report a reading the way a model would: dates as YYYY-MM-DD when they parse."""
        if f == "delivery_date":
            from .verify import _parse_written_date
            return _parse_written_date(text, fmt or "M/D/YY") or text
        if f in ("material_type", "supplier", "job_location") and text.casefold() == str(label["true_value"]).casefold():
            return label["true_value"]
        return text

    def answer(self, info: SlotInfo) -> dict:
        raw = super().answer(info)
        if not info.ticket:
            return raw
        from .templates import TEMPLATES
        specs = {s["field"]: s for s in TEMPLATES[info.ticket["template"]]["fields"]}
        for f in SIX:
            ours = OURS[f]
            label = info.ticket["fields"][ours]
            r = rng("noisy", self.seed, info.ticket["ticket_id"], f)
            key = (info.file, info.page, info.position, f)
            expected = raw[f]
            if expected is None:
                if label["written_value"] is not None and r.random() < self.model["guess_unreadable"]:
                    guess = self._as_output(f, label["written_value"], label, specs[ours].get("fmt"))
                    raw[f] = guess
                    self.planted[key] = Planted(info.ticket["ticket_id"], f, "guessed", None, guess)
                continue
            if r.random() < self.model["drop"]:
                raw[f] = None
                self.planted[key] = Planted(info.ticket["ticket_id"], f, "dropped", expected, None)
                continue
            if label["ink"] in ("print", "stamp", "hand") and label["visible_value"]:
                read = self._read_text(f, label["visible_value"], label, r)
                if read != label["visible_value"]:
                    out = self._as_output(f, read, label, specs[ours].get("fmt"))
                    raw[f] = out
                    self.planted[key] = Planted(info.ticket["ticket_id"], f, "misread", expected, out)
        return raw


def make_extractor(name: str, slots_by_key: dict, *, seed: int = 0, error_model: dict | None = None,
                   authorize_paid: bool = False):
    if name == "oracle":
        return OracleExtractor(slots_by_key)
    if name == "oracle-crop-aware":
        return CropAwareOracle(slots_by_key)
    if name == "noisy-simulated":
        return NoisySimulatedExtractor(slots_by_key, seed, error_model)
    if name == "gemini":
        if not authorize_paid:
            raise ExtractionRefused("--extractor gemini makes paid API calls. It runs only with "
                                    "--i-authorize-paid-calls, and a configured GEMINI key.")
        from app.config import settings
        from app.services.benchmark_providers import GeminiBenchmarkExtractor
        return GeminiBenchmarkExtractor(model=settings.gemini_model)
    raise ValueError(f"Unknown extractor {name!r}; choose from {sorted(DESCRIPTIONS)}")
