"""The comparison key two statements have to agree on.

Every case here is a shape a Canadian statement actually prints. The point of
the table is that it is a table: when someone improves the regex, the diff shows
exactly which keys changed — and every one that changes is a row in the database
whose key no longer matches what this function would produce.
"""

from __future__ import annotations

import pytest

from app.services.normalization import VERSION, merchant, normalized


class TestTheKey:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            # Store numbers: the same shop, two statements, two renderings.
            ("TIM HORTONS #4821 OTTAWA ON", "tim hortons ottawa on"),
            ("TIM HORTONS 4821 OTTAWA ON", "tim hortons ottawa on"),
            ("tim hortons #4821 ottawa on", "tim hortons ottawa on"),
            # Bank prefixes are noise the merchant did not choose.
            ("POS PURCHASE LOBLAWS 1042", "loblaws"),
            ("PURCHASE LOBLAWS 1042", "loblaws"),
            ("VISA DEBIT LOBLAWS 1042", "loblaws"),
            # Reference numbers are unique per transaction: left in, every row
            # is unique and the dedup key never matches anything.
            ("AMZN MKTP CA*2R41K", "amzn mktp ca"),
            ("SPOTIFY P3A4B5C6", "spotify p3a4b5c6"),
            ("POS PURCHASE ••••2345 PETRO-CANADA 2281", "petro canada"),
            # Dates inside a description vary by statement period.
            ("HYDRO OTTAWA 2026-08-06 PREAUTH", "hydro ottawa preauth"),
            ("BELL CANADA 08/21 PREAUTH", "bell canada preauth"),
            # Punctuation and padding.
            ("RESTAURANT   LE  MOULIN", "restaurant le moulin"),
            ("NETFLIX.COM", "netflix com"),
            # `interac` is a name here, not a prefix — removing it leaves
            # "e transfer fee", which reads and categorizes worse.
            ("INTERAC E-TRANSFER FEE", "interac e transfer fee"),
        ],
    )
    def test_shapes_a_statement_actually_prints(self, description, expected):
        assert normalized(description) == expected

    def test_the_same_input_always_gives_the_same_output(self):
        """Determinism is the whole contract: the key is stored, and a stored
        key that a later call would not reproduce is a row dedup can no longer
        see."""
        description = "POS PURCHASE TIM HORTONS #4821 OTTAWA ON"

        assert len({normalized(description) for _ in range(100)}) == 1

    def test_nothing_readable_gives_an_empty_key(self):
        assert normalized(None) == ""
        assert normalized("") == ""
        assert normalized("#### 1234") == ""

    def test_two_different_merchants_do_not_collide(self):
        assert normalized("LOBLAWS 1042") != normalized("METRO 8821")

    def test_a_chain_keeps_its_location(self):
        """Deliberate: stripping city and province would collapse two branches
        into one key, so two real $5.25 coffees at different locations on one
        day would look like an exact duplicate and one would be dropped with no
        trace. A missed collapse becomes a near-match a human resolves; a wrong
        collapse deletes somebody's transaction."""
        assert normalized("TIM HORTONS #1 OTTAWA ON") != normalized(
            "TIM HORTONS #2 TORONTO ON"
        )


class TestTheReadableName:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("TIM HORTONS #4821 OTTAWA ON", "Tim Hortons Ottawa On"),
            ("POS PURCHASE ••••2345 PETRO-CANADA 2281", "Petro-Canada"),
            ("INTERAC E-TRANSFER FEE", "Interac E-Transfer Fee"),
            ("SHOPPERS DRUG MART #0847", "Shoppers Drug Mart"),
        ],
    )
    def test_reads_like_a_name(self, description, expected):
        assert merchant(description) == expected

    def test_nothing_readable_gives_nothing(self):
        assert merchant(None) is None
        assert merchant("#### 1234") is None

    def test_it_carries_no_reference_numbers_into_a_prompt(self):
        """This string is what the categorizer receives (Appendix A.3), so the
        identifiers stripped for the key must not come back here."""
        assert "2345" not in (merchant("POS PURCHASE ••••2345 PETRO-CANADA") or "")
        assert "4821" not in (merchant("TIM HORTONS #4821") or "")


def test_the_version_is_an_integer_that_someone_will_have_to_bump():
    """A guard on the docstring's promise. Changing the output without changing
    this leaves every stored key computed by a function that no longer exists."""
    assert isinstance(VERSION, int)
