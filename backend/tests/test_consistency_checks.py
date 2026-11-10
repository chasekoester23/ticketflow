"""Catching a job number typed or read as the ticket number, and names that mean a catalog name."""
from types import SimpleNamespace

from app.services import consistency, pipeline
from app.services.extraction import PROMPT
from app.services.validation import ExtractedTicket

from .sample_data import make_scan_pdf


def _ticket(id, number, supplier="INDIANA VALLEY TRUCKING", job=None, raw=None):
    return SimpleNamespace(id=id, ticket_number=number, supplier=supplier, job_location=job, raw_extraction=raw)


ROWS = [(1, "INDIANA VALLEY TRUCKING", "IV-52331"), (2, "INDIANA VALLEY TRUCKING", "IV-52332"), (3, "Indiana Valley", "004821")]


def test_the_indiana_valley_case_a_job_number_in_the_ticket_number_is_called_out():
    numbers = consistency.TicketNumbers(ROWS)
    warning = numbers.warning(_ticket(3, "004821", supplier="Indiana Valley"))
    assert "IV-5233" in warning and "job, PO or truck number" in warning
    assert numbers.warning(_ticket(1, "IV-52331")) is None  # the supplier's own pattern is fine


def test_too_few_tickets_or_no_clear_pattern_says_nothing():
    assert consistency.TicketNumbers(ROWS[:1] + ROWS[2:]).warning(_ticket(3, "004821")) is None
    mixed = consistency.TicketNumbers([(1, "X Co", "A-1"), (2, "X Co", "22"), (3, "X Co", "B-33-C"), (4, "X Co", "9")])
    assert mixed.warning(_ticket(5, "004821", supplier="X Co")) is None


def test_the_number_matching_the_job_number_or_the_job_is_called_out():
    numbers = consistency.TicketNumbers([])
    assert "job number" in numbers.warning(_ticket(1, "004821", raw={"job_number": "004821"}))
    assert "also in the job" in numbers.warning(_ticket(1, "004821", job="Job 004821, I-65"))
    assert numbers.warning(_ticket(1, "004", job="Job 004821")) is None  # part of a longer number isn't a match


def test_the_prompt_separates_the_ticket_number_from_the_job_number_and_keeps_its_letters():
    assert "IV-52330" in PROMPT and "NEVER the job number" in PROMPT
    assert "job_number" in PROMPT and "hours" in PROMPT
    assert ExtractedTicket.model_validate({"ticket_number": " IV-52330 "}).ticket_number == "IV-52330"
    assert ExtractedTicket.model_validate({"ticket_number": "004821"}).ticket_number == "004821"  # leading zeros kept


def test_a_prefixed_ticket_number_survives_editing_and_the_list_warns_on_the_outlier(client):
    batch = client.post("/api/batches", data={"layout": "grid_2x2"},
                        files=[("files", ("scan.pdf", make_scan_pdf(), "application/pdf"))]).json()["id"]
    tickets = client.get(f"/api/batches/{batch}/tickets").json()
    for ticket, number in zip(tickets, ["IV-52331", "IV-52332", "IV-52333", "004821"]):
        response = client.patch(f"/api/tickets/{ticket['id']}", json={"ticket_number": number, "supplier": "Indiana Valley Trucking"})
        assert response.status_code == 200 and response.json()["ticket_number"] == number
    rows = client.get(f"/api/batches/{batch}/tickets").json()
    assert [r["number_warning"] is not None for r in rows] == [False, False, False, True]
    # The answer to an edit carries the same check, worked out on its own.
    again = client.patch(f"/api/tickets/{rows[3]['id']}", json={"job_location": "Lot B"}).json()
    assert again["number_warning"] == rows[3]["number_warning"]


def test_a_name_that_differs_only_by_company_words_suggests_the_catalog_name():
    names = consistency.SimilarNames(["INDIANA VALLEY TRUCKING", "Example Aggregates Inc."], ["#53 Crushed Stone", "No. 57 Stone"])
    assert names.suggest("supplier", "Indiana Valley") == "INDIANA VALLEY TRUCKING"
    assert names.suggest("supplier", "example aggregates") == "Example Aggregates Inc."
    assert names.suggest("material", "No 53 Crushed Stone") == "#53 Crushed Stone"
    assert names.suggest("material", "53 crushed stone") is None  # already the same name once punctuation is ignored
    assert names.suggest("material", "57 stone") == "No. 57 Stone"


def test_a_name_suggestion_is_never_a_guess():
    names = consistency.SimilarNames(["Smith Aggregates", "Smith Materials", "Valley Sand"], ["#53 Crushed Stone", "#53 Washed Stone"])
    assert names.suggest("supplier", "Smith") is None  # one word: too little to go on
    assert names.suggest("supplier", "Smith Gravel") is None  # a different word isn't a variant
    assert names.suggest("supplier", "Valley Sand") is None  # already the catalog name
    assert names.suggest("material", "53") is None
    assert names.suggest("material", "53 stone") is None  # matches neither name exactly


def test_an_hourly_ticket_says_so_instead_of_tonnage_missing():
    ticket = SimpleNamespace(flags=[], tonnage=None)
    extracted = ExtractedTicket.model_validate({"ticket_number": "H-1001", "supplier": "Fictional Hauling", "tonnage": None})
    pipeline.apply_extraction(ticket, extracted, {"ticket_number": "H-1001", "hours": "6.5", "tonnage": None})
    assert any(flag.startswith("Hourly ticket (6.5 h)") for flag in ticket.flags)
    ticket = SimpleNamespace(flags=[], tonnage=None)
    pipeline.apply_extraction(ticket, ExtractedTicket.model_validate({"tonnage": "22.41"}), {"tonnage": "22.41", "hours": None})
    assert not any("Hourly" in flag for flag in ticket.flags)
