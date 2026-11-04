"""`/splits`, end to end: contacts, a receipt read by a fake model, and
splits worked out exactly by the server."""

from __future__ import annotations

import pytest

from app.api import splits as endpoint
from app.services.llm import LlmError
from tests.conftest import requires_db
from tests.test_ai_budget_api import FakeModel
from tests.test_dashboard import a_household
from tests.test_statements_endpoint import consent_to_ai

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

RECEIPT = "\n".join(
    [
        "THE COFFEE HOUSE",
        "Connaught Place, New Delhi",
        "12 Oct 2026 10:24 AM",
        "Cappuccino 180.00",
        "Latte 160.00",
        "Blueberry Muffin 120.00",
        "Chicken Sandwich 250.00",
        "Subtotal 710.00",
        "Tax (5%) 35.50",
        "Total 745.50",
        "Card ****1234",
    ]
)

ANSWER = {
    "merchant": "The Coffee House",
    "date": "2026-10-12",
    "items": [
        {"name": "Cappuccino", "quantity": 1, "amount": "180.00"},
        {"name": "Latte", "quantity": 1, "amount": "160.00"},
        {"name": "Blueberry Muffin", "quantity": 1, "amount": "120.00"},
        {"name": "Chicken Sandwich", "quantity": 1, "amount": "250.00"},
        {"name": "Imaginary Cake", "quantity": 1, "amount": "999.00"},
    ],
    "subtotal": "710.00",
    "tax": "35.50",
    "tip": None,
    "total": "745.50",
}


async def friends(api_client, *pairs) -> list[str]:
    ids = []
    for name, phone in pairs:
        made = await api_client.post(
            "/splits/contacts", json={"name": name, "phone": phone}
        )
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    return ids


class TestContacts:
    async def test_saved_once_and_found_again(self, api_client):
        await a_household(api_client, "+14165580101")

        first = await api_client.post(
            "/splits/contacts", json={"name": "Rohan", "phone": "(416) 555-0142"}
        )
        again = await api_client.post(
            "/splits/contacts", json={"name": "Rohan S.", "phone": "+1 416 555 0142"}
        )
        listed = (await api_client.get("/splits/contacts")).json()["contacts"]

        assert (
            first.json()["phone"] == "+14165550142"
        ), "read with the household's region"
        assert again.json()["id"] == first.json()["id"], "the same number is one person"
        assert [(c["name"], c["phone"]) for c in listed] == [
            ("Rohan S.", "+14165550142")
        ]

    async def test_a_number_whatsapp_cannot_reach_is_refused(self, api_client):
        await a_household(api_client, "+14165580102")

        response = await api_client.post(
            "/splits/contacts", json={"name": "Nobody", "phone": "123"}
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_phone"

    async def test_deleting_a_contact_keeps_the_bill_it_was_on(self, api_client):
        await a_household(api_client, "+14165580103")
        (rohan,) = await friends(api_client, ("Rohan", "+14165550143"))
        split = await api_client.post(
            "/splits",
            json={
                "title": "Lunch",
                "occurred_on": "2026-10-12",
                "mode": "equal",
                "total": "40.00",
                "participants": [{}, {"contact_id": rohan}],
            },
        )

        gone = await api_client.delete(f"/splits/contacts/{rohan}")
        kept = (await api_client.get(f"/splits/{split.json()['id']}")).json()

        assert gone.status_code == 204
        assert kept["participants"][1]["name"] == "Rohan"
        assert kept["participants"][1]["phone"] == "+14165550143"

    async def test_another_household_cannot_see_them(self, api_client):
        await a_household(api_client, "+14165580104")
        (rohan,) = await friends(api_client, ("Rohan", "+14165550144"))
        await a_household(api_client, "+14165580105")

        response = await api_client.delete(f"/splits/contacts/{rohan}")

        assert response.status_code == 404
        assert (await api_client.get("/splits/contacts")).json()["contacts"] == []


class TestSplitting:
    async def test_equally_the_shares_add_up_exactly(self, api_client):
        await a_household(api_client, "+14165580201")
        rohan, priya = await friends(
            api_client, ("Rohan", "+14165550201"), ("Priya", "+14165550202")
        )

        response = await api_client.post(
            "/splits",
            json={
                "title": "Dinner",
                "occurred_on": "2026-10-12",
                "mode": "equal",
                "total": "10.00",
                "participants": [{}, {"contact_id": rohan}, {"contact_id": priya}],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert [p["share"] for p in body["participants"]] == ["3.34", "3.33", "3.33"]
        assert body["participants"][0]["is_self"] is True
        assert body["owed_to_you"] == "6.66"

    async def test_by_items_tax_and_tip_follow_what_each_had(self, api_client):
        await a_household(api_client, "+14165580202")
        alex, rhea, dev = await friends(
            api_client,
            ("Alex", "+14165550203"),
            ("Rhea", "+14165550204"),
            ("Dev", "+14165550205"),
        )

        response = await api_client.post(
            "/splits",
            json={
                "title": "Bistro Verde",
                "occurred_on": "2026-10-24",
                "mode": "items",
                "items": [
                    {
                        "name": "Truffle pasta",
                        "amount": "28.50",
                        "participants": [1, 3],
                    },
                    {"name": "Salmon", "amount": "34.00", "participants": [2]},
                    {
                        "name": "Mocktails",
                        "amount": "18.00",
                        "participants": [0, 1, 2, 3],
                    },
                    {"name": "Mille-feuille", "amount": "16.50", "participants": [0]},
                ],
                "tax": "8.61",
                "tip": "17.46",
                "participants": [
                    {},
                    {"contact_id": alex},
                    {"contact_id": rhea},
                    {"contact_id": dev},
                ],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["total"] == "123.07"
        assert [p["share"] for p in body["participants"]] == [
            "26.64",
            "23.79",
            "48.85",
            "23.79",
        ]
        assert body["participants"][2]["items"] == ["Salmon", "Mocktails"]

    @pytest.mark.parametrize(
        ("phone", "change", "code"),
        [
            ("+14165580211", {"participants": [{}]}, None),
            ("+14165580212", {"participants": [{}, {}]}, "invalid_participants"),
            ("+14165580213", {"mode": "both"}, "invalid_mode"),
            ("+14165580214", {"total": "0"}, "invalid_amount"),
        ],
    )
    async def test_a_bill_it_cannot_split_is_refused(
        self, api_client, phone, change, code
    ):
        await a_household(api_client, phone)
        (rohan,) = await friends(api_client, ("Rohan", phone[:-1] + "9"))
        body = {
            "title": "Lunch",
            "occurred_on": "2026-10-12",
            "mode": "equal",
            "total": "40.00",
            "participants": [{}, {"contact_id": rohan}],
        } | change

        response = await api_client.post("/splits", json=body)

        assert response.status_code == 422, response.text
        if code:
            assert response.json()["detail"]["code"] == code

    async def test_an_item_with_nobody_is_refused(self, api_client):
        await a_household(api_client, "+14165580215")
        (rohan,) = await friends(api_client, ("Rohan", "+14165550215"))

        response = await api_client.post(
            "/splits",
            json={
                "title": "Lunch",
                "occurred_on": "2026-10-12",
                "mode": "items",
                "items": [{"name": "Tea", "amount": "3.00", "participants": [5]}],
                "participants": [{}, {"contact_id": rohan}],
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_items"

    async def test_someone_paying_up_is_marked_and_listed(self, api_client):
        await a_household(api_client, "+14165580221")
        (rohan,) = await friends(api_client, ("Rohan", "+14165550221"))
        made = (
            await api_client.post(
                "/splits",
                json={
                    "title": "Taxi",
                    "occurred_on": "2026-10-12",
                    "mode": "equal",
                    "total": "30.00",
                    "participants": [{}, {"contact_id": rohan}],
                },
            )
        ).json()

        settled = await api_client.patch(
            f"/splits/{made['id']}/participants/{made['participants'][1]['id']}",
            json={"settled": True},
        )
        listed = (await api_client.get("/splits")).json()["splits"]

        assert settled.json()["owed_to_you"] == "0.00"
        assert listed[0]["settled"] is True
        assert (await api_client.delete(f"/splits/{made['id']}")).status_code == 204
        assert (await api_client.get("/splits")).json()["splits"] == []


class TestAReceipt:
    async def test_only_amounts_on_the_receipt_come_back(self, api_client, monkeypatch):
        await a_household(api_client, "+14165580301")
        await consent_to_ai(api_client)
        model = FakeModel(ANSWER)
        monkeypatch.setattr(endpoint, "build_client", lambda _settings: model)

        response = await api_client.post("/splits/receipt", json={"text": RECEIPT})

        assert response.status_code == 200, response.text
        body = response.json()
        assert [i["name"] for i in body["items"]] == [
            "Cappuccino",
            "Latte",
            "Blueberry Muffin",
            "Chicken Sandwich",
        ], "the made-up cake is not on the receipt"
        assert body["unreadable"] == 1
        assert (body["tax"], body["total"], body["tip"]) == ("35.50", "745.50", None)
        assert body["merchant"] == "The Coffee House"
        assert model.calls[0][1] == RECEIPT, "only the receipt's text is sent"

    async def test_without_consent_the_model_is_never_asked(
        self, api_client, monkeypatch
    ):
        await a_household(api_client, "+14165580302")
        model = FakeModel(ANSWER)
        monkeypatch.setattr(endpoint, "build_client", lambda _settings: model)

        response = await api_client.post("/splits/receipt", json={"text": RECEIPT})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "consent_required"
        assert model.calls == []

    async def test_a_model_that_cannot_be_built_is_unavailable_not_a_crash(
        self, api_client, monkeypatch
    ):
        await a_household(api_client, "+14165580303")
        await consent_to_ai(api_client)

        def unbuildable(_settings):
            raise LlmError("LLM_API_KEY is not set")

        monkeypatch.setattr(endpoint, "build_client", unbuildable)

        response = await api_client.post("/splits/receipt", json={"text": RECEIPT})

        assert response.status_code == 503

    async def test_a_month_has_a_limit(self, api_client, monkeypatch):
        await a_household(api_client, "+14165580304")
        await consent_to_ai(api_client)
        monkeypatch.setattr(
            endpoint, "build_client", lambda _settings: FakeModel(ANSWER)
        )
        monkeypatch.setattr("app.services.splits.MAX_RECEIPT_READS", 2)

        codes = [
            (
                await api_client.post("/splits/receipt", json={"text": RECEIPT})
            ).status_code
            for _ in range(3)
        ]

        assert codes == [200, 200, 429]
