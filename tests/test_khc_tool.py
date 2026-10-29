import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.tools import khc


def ctx(session_id="session-1"):
    return SimpleNamespace(deps=SimpleNamespace(session_id=session_id, question_id="q1"))


def registration(**changes):
    return {
        "mobile_number": "9876543210", "farmer_type": "existing",
        "grievance_text": "Bank refused loan", "farmer_language": "hi",
        "state_id": 10, "village_id": 219159, "scheme_id": 38, "division_id": 31,
        "problem_category_id": 39, "problem_subcategory_id": 1725, "confirmed": True,
        **changes,
    }


def envelope(action="status", code=None, fields=None, message="Retrieved"):
    tag = {
        "descriptor": {
            "code": code or ("grievance_status" if action == "status" else "grievance_created"),
            "short_desc": message,
        },
        "list": [{"descriptor": {"code": key}, "value": value} for key, value in (
            fields if fields is not None else {
                "grievanceNo": "BR/240926/2", "currentStatus": "OPEN",
                "currentLevel": "STATE", "createdAt": "2026-09-24",
            }
        ).items()],
    }
    order = {"tags": [tag]} if action == "status" else {"items": [{"tags": [tag]}]}
    return {"message": {"order": order}}


def fake_http(monkeypatch, response, calls):
    monkeypatch.setenv("BAP_ENDPOINT", "https://bap.test")
    real_client = khc.httpx.AsyncClient

    def respond(request):
        import json
        calls.append(json.loads(request.content))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        khc.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=khc.httpx.MockTransport(respond), **kwargs),
    )


@pytest.mark.parametrize("number", ["", "abc\r\nbad", "a" * 101])
def test_status_asks_for_number_without_http(monkeypatch, number):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    output = asyncio.run(khc.check_khc_grievance_status(ctx(), number))
    assert "ask which scheme or service first, without a source label" in output
    assert "Ask the farmer for their Kisan Help Center grievance number" in output
    call.assert_not_called()


def test_status_preserves_slashes_and_normalizes_regional_digits(monkeypatch):
    call = AsyncMock(return_value="OPEN")
    monkeypatch.setattr(khc, "_call", call)
    assert asyncio.run(khc.check_khc_grievance_status(ctx(), " BR/२४०९२६/२ ")) == "OPEN"
    assert call.call_args.kwargs == {"grievanceNo": "BR/240926/2"}


@pytest.mark.parametrize("changes,expected", [
    ({"mobile_number": ""}, "mobile"),
    ({"farmer_type": ""}, "OTP first"),
    ({"grievance_text": ""}, "describe"),
    ({"grievance_text": "a" * 5001}, "describe"),
    ({"farmer_language": ""}, "language"),
    ({"farmer_type": "new"}, "name"),
    ({"state_id": None}, "mappings"),
    ({"village_id": True}, "mappings"),
    ({"scheme_id": 0}, "mappings"),
    ({"problem_category_id": 9007199254740992}, "mappings"),
    ({"confirmed": False}, "confirmation"),
])
def test_registration_collects_validated_details_and_confirmation(monkeypatch, changes, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.register_khc_grievance(ctx(), **registration(**changes)))
    call.assert_not_called()


@pytest.mark.parametrize("farmer_type", ["new", "existing"])
def test_registration_uses_verified_ids_and_stable_reference(monkeypatch, farmer_type):
    call = AsyncMock(return_value="Registered")
    monkeypatch.setattr(khc, "_call", call)
    values = registration(farmer_type=farmer_type, farmer_name="Test Farmer", gender="M")
    assert asyncio.run(khc.register_khc_grievance(ctx(), **values)) == "Registered"
    sent = call.call_args.kwargs
    assert sent["farmerMobileNumber"] == "9876543210"
    assert sent["confirmed"] == "true"
    assert sent["stateId"] == "10"
    assert sent["farmerType"] == farmer_type
    assert ("farmerName" in sent) == (farmer_type == "new")
    first_reference = sent["externalReferenceNo"]
    asyncio.run(khc.register_khc_grievance(ctx(), **values))
    assert call.call_args.kwargs["externalReferenceNo"] == first_reference


@pytest.mark.parametrize("name,code", [
    ("Hindi", "hi"), ("English", "en"), ("Marathi", "mr"), ("Bengali", "bn"),
    ("Gujarati", "gu"), ("Kannada", "kn"), ("Tamil", "ta"), ("Telugu", "te"),
    ("Assamese", "as"), ("Malayalam", "ml"),
])
def test_registration_normalizes_language_names_and_codes(monkeypatch, name, code):
    call = AsyncMock(return_value="Registered")
    monkeypatch.setattr(khc, "_call", call)
    for value in (f" {name} ", code.upper(), code):
        assert asyncio.run(khc.register_khc_grievance(
            ctx(), **registration(farmer_language=value),
        )) == "Registered"
        assert call.call_args.kwargs["farmerLanguage"] == code


@pytest.mark.parametrize("name,code", [
    ("male", "M"), ("FEMALE", "F"), (" Other ", "O"),
    ("m", "M"), ("f", "F"), ("o", "O"),
])
def test_new_farmer_normalizes_gender_and_preserves_external_reference(monkeypatch, name, code):
    call = AsyncMock(return_value="Registered")
    monkeypatch.setattr(khc, "_call", call)
    for language, gender in (("Hindi", name), ("hi", code)):
        assert asyncio.run(khc.register_khc_grievance(ctx(), **registration(
            farmer_type="new", farmer_name="Test Farmer",
            farmer_language=language, gender=gender,
        ))) == "Registered"
        assert call.call_args.kwargs["farmerLanguage"] == "hi"
        assert call.call_args.kwargs["gender"] == code
    assert call.call_args_list[0].kwargs["externalReferenceNo"] == call.call_args_list[1].kwargs["externalReferenceNo"]


def test_new_farmer_payload_matches_khc_field_contract(monkeypatch):
    call = AsyncMock(return_value="Registered")
    monkeypatch.setattr(khc, "_call", call)
    assert asyncio.run(khc.register_khc_grievance(ctx(), **registration(
        farmer_type="new", farmer_name="UAT New Farmer", gender="male",
        farmer_language="Hindi", grievance_text="Bank refused loan under AIF",
    ))) == "Registered"
    sent = call.call_args.kwargs
    assert sent == {
        "externalReferenceNo": sent["externalReferenceNo"],
        "farmerMobileNumber": "9876543210",
        "farmerName": "UAT New Farmer", "gender": "M", "farmerLanguage": "hi",
        "stateId": "10", "villageId": "219159", "scheme": "38", "division": "31",
        "problemCategory": "39", "problemSubCategory": "1725",
        "farmerText": "Bank refused loan under AIF",
        "farmerType": "new", "confirmed": "true", "request_type": "create_grievance",
    }
    assert sent["externalReferenceNo"].startswith("BV-")


@pytest.mark.parametrize("call_language,accepted", [("hi", True), ("ta", True), ("pa", False), ("od", False)])
def test_registration_defaults_to_the_call_language(monkeypatch, call_language, accepted):
    call = AsyncMock(return_value="ok")
    monkeypatch.setattr(khc, "_call", call)
    voice_ctx = SimpleNamespace(deps=SimpleNamespace(
        session_id="session-1", question_id="q1", language_code=call_language,
    ))
    result = asyncio.run(khc.register_khc_grievance(voice_ctx, **registration(farmer_language="")))
    if accepted:
        assert call.await_args.kwargs["farmerLanguage"] == call_language
    else:
        assert "does not currently accept" in result
        call.assert_not_called()


@pytest.mark.parametrize("changes,expected", [
    ({"farmer_language": "French"}, "does not currently accept"),
    ({"farmer_language": "mai"}, "does not currently accept"),
    ({"farmer_language": "Maithili"}, "does not currently accept"),
    *[
        ({"farmer_language": language}, "does not currently accept")
        for language in (
            "Punjabi", "pa", "Odia", "or", "Urdu", "ur",
            "Nepali", "ne", "Manipuri", "mni",
        )
    ],
    ({"gender": "unknown"}, "gender"),
])
def test_registration_rejects_unsupported_language_or_gender(monkeypatch, changes, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.register_khc_grievance(ctx(), **registration(
        **{"farmer_type": "new", "farmer_name": "Test Farmer", "gender": "M", **changes},
    )))
    call.assert_not_called()


def test_registration_requires_session(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert "active conversation" in asyncio.run(khc.register_khc_grievance(ctx(""), **registration()))
    call.assert_not_called()


@pytest.mark.parametrize("action", ["status", "init"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_network_envelope_and_rendering(monkeypatch, action, wrapped):
    result = envelope(action)
    if wrapped:
        result = {"responses": [result]}
    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=result), calls)
    output = asyncio.run(khc._call(ctx(), action, grievanceNo="BR/240926/2"))
    assert "BR/240926/2" in output and "OPEN" in output and khc.SOURCE in output
    sent = calls[0]
    assert sent["message"]["order"]["provider"]["id"] == "khc-agri"
    assert sent["message"]["order"]["items"][0]["id"] == "khc-grievance"
    assert sent["context"]["tags"] == {"session_id": "session-1", "question_id": "q1"}
    if action == "status":
        assert sent["message"]["order_id"] == sent["context"]["transaction_id"]


@pytest.mark.parametrize("action", ["status", "init"])
@pytest.mark.parametrize("response", [
    khc.httpx.Response(500, text="error"),
    khc.httpx.Response(200, text="not JSON"),
    khc.httpx.Response(200, json=[]),
    khc.httpx.Response(200, json={}),
    khc.httpx.ConnectError("unavailable"),
])
def test_network_failures_are_explicit_and_never_retried(monkeypatch, action, response):
    calls = []
    fake_http(monkeypatch, response, calls)
    output = asyncio.run(khc._call(ctx(), action, grievanceNo="BR/240926/2"))
    assert "not confirmed" in output if action == "init" else ("unavailable" in output or "could not be reached" in output)
    assert len(calls) == 1
    assert khc.SOURCE not in output


def test_missing_configuration_does_not_claim_submission(monkeypatch):
    monkeypatch.delenv("BAP_ENDPOINT", raising=False)
    assert "not configured" in asyncio.run(khc._call(ctx(), "init"))


@pytest.mark.parametrize("code", ["not_found", "submission_unconfirmed", "invalid_request"])
def test_returns_provider_errors_without_source(monkeypatch, code):
    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(code=code, message="Specific error")), calls)
    assert asyncio.run(khc._call(ctx(), "status", grievanceNo="BR/240926/2")) == "Specific error"


@pytest.mark.parametrize("fields", [{}, {"grievanceNo": "OTHER", "currentStatus": "OPEN"}])
def test_incomplete_or_wrong_grievance_is_not_reported_as_success(monkeypatch, fields):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(fields=fields)), [])
    assert "unavailable" in asyncio.run(khc._call(ctx(), "status", grievanceNo="BR/240926/2"))


def test_all_voice_prompts_include_khc_rules():
    from helpers.utils import get_prompt

    for language in ("en", "hi", "bn", "gu", "kn", "ml", "mr", "od", "pa", "ta", "te"):
        prompt = get_prompt(f"voice_{language}", context={
            "today_date": "x", "vector_scheme_count": 0,
            "vector_schemes_bullets": "", "vector_schemes_identifiers": "",
        })
        assert "## KISAN HELPLINE CENTER (KHC) GRIEVANCES" in prompt
        for tool in (
            "find_khc_location", "get_khc_grievance_options", "lookup_khc_reference",
            "initiate_khc_otp", "verify_khc_otp", "register_khc_grievance", "check_khc_grievance_status",
        ):
            assert tool in prompt
        assert "Do not suggest KHC for unrelated questions" in prompt
        assert "Please tell me about your problem in your own words. I am here to help." in prompt
        assert "never submit again on your own" in prompt
        assert "turn the spoken word \"slash\" into \"/\"" in prompt


@pytest.mark.parametrize("mobile,session,expected", [
    ("", "S1", "mobile"), ("9876543210", "", "active conversation"),
])
def test_send_otp_requires_mobile_and_session(monkeypatch, mobile, session, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.initiate_khc_otp(ctx(session), mobile))
    call.assert_not_called()


@pytest.mark.parametrize("mobile,otp,session,expected", [
    ("", "123456", "S1", "mobile"), ("9876543210", "", "S1", "6-digit"),
    ("9876543210", "12345", "S1", "6-digit"), ("9876543210", "123456", "", "active conversation"),
])
def test_verify_otp_requires_valid_inputs(monkeypatch, mobile, otp, session, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.verify_khc_otp(ctx(session), mobile, otp))
    call.assert_not_called()


@pytest.mark.parametrize("otp,expected", [
    ("654321", "654321"), (" 012345 ", "012345"), ("१२३४५६", "123456"),
])
def test_six_digit_otp_is_sent_to_verification_without_reasking(monkeypatch, otp, expected):
    call = AsyncMock(return_value="Mobile verified successfully.")
    monkeypatch.setattr(khc, "_call", call)
    assert "verified successfully" in asyncio.run(khc.verify_khc_otp(ctx(), "9876543210", otp))
    call.assert_awaited_once_with(
        ctx(), "init", request_type="verify_otp", farmerMobileNumber="9876543210", otp=expected,
    )


def test_otp_sent_result_directs_verification_before_submission(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "otp_sent", {})), [])
    output = asyncio.run(khc.initiate_khc_otp(ctx(), "9876543210"))
    assert "call verify_khc_otp with the same mobile" in output
    assert "Do not reject a six-digit reply without a verification tool result" in output
    assert "proceed to submission after verification" not in output


@pytest.mark.parametrize("code,message", [
    ("invalid_otp", "OTP is invalid or expired"),
    ("upstream_error", "KHC service is unavailable"),
])
def test_verification_preserves_actual_failure_without_claiming_success(monkeypatch, code, message):
    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(
        "init", code, {}, message=message,
    )), calls)
    assert asyncio.run(khc.verify_khc_otp(ctx(), "9876543210", "123456")) == message
    assert len(calls) == 1


@pytest.mark.parametrize("registered", [False, True])
def test_otp_and_registration_share_transaction_without_exposing_otp(monkeypatch, registered):
    responses = [
        envelope("init", "otp_sent", {}),
        envelope("init", "otp_verified", {"isRegistered": str(registered).lower()}),
        envelope("init", "grievance_created"),
    ]
    calls = []
    real_client = khc.httpx.AsyncClient

    def respond(request):
        import json
        calls.append(json.loads(request.content))
        return khc.httpx.Response(200, json=responses.pop(0))

    monkeypatch.setenv("BAP_ENDPOINT", "https://bap.test")
    monkeypatch.setattr(khc.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=khc.httpx.MockTransport(respond), **kwargs,
    ))
    assert "OTP sent" in asyncio.run(khc.initiate_khc_otp(ctx(), "+91 9876543210"))
    result = asyncio.run(khc.verify_khc_otp(ctx(), "9876543210", "१२३४५६"))
    assert ("already registered" if registered else "not yet registered") in result
    assert "123456" not in result
    details = registration(farmer_type="existing" if registered else "new")
    if not registered:
        assert "name" in asyncio.run(khc.register_khc_grievance(ctx(), **details))
        assert len(calls) == 2
        details["farmer_name"] = "Test Farmer"
        assert "gender" in asyncio.run(khc.register_khc_grievance(ctx(), **details))
        assert len(calls) == 2
        details["gender"] = "Female"
    assert "BR/240926/2" in asyncio.run(khc.register_khc_grievance(ctx(), **details))
    assert len(calls) == 3
    assert len({call["context"]["transaction_id"] for call in calls}) == 1
    submitted = _tags(calls[-1])
    assert ("farmerName" in submitted) is not registered
    assert ("gender" in submitted) is not registered
    if not registered:
        assert submitted["farmerName"] == "Test Farmer"
        assert submitted["gender"] == "F"


def test_registration_otp_required_reuses_same_mobile_without_reasking(monkeypatch):
    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(
        "init", "otp_required", {}, message="OTP verification required",
    )), calls)
    output = asyncio.run(khc.register_khc_grievance(ctx(), **registration()))
    assert "same mobile number (9876543210)" in output
    assert "Do not ask the farmer for the mobile number again" in output
    assert len(calls) == 1


@pytest.mark.parametrize("registered", ["true", "invalid"])
def test_verification_rendering_requires_registration_state(monkeypatch, registered):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(
        "init", "otp_verified", {"isRegistered": registered},
    )), [])
    result = asyncio.run(khc.verify_khc_otp(ctx(), "9876543210", "123456"))
    assert ("already registered" in result) if registered == "true" else ("unavailable" in result)


@pytest.mark.parametrize("registered", ["false", "true"])
def test_verified_farmer_collects_new_profile_only_after_otp(monkeypatch, registered):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(
        "init", "otp_verified", {"isRegistered": registered},
    )), [])
    result = asyncio.run(khc.verify_khc_otp(ctx(), "9876543210", "123456"))
    assert "This is the last step" in result
    assert "do not ask for them again" in result
    assert ("Call register_khc_grievance now" in result) is (registered == "true")
    if registered == "false":
        assert "isRegistered=false" in result and "farmer_type='new'" in result
        assert "Ask only for the missing name, then gender" in result
        assert "Do not submit until both are known" in result
    else:
        assert "isRegistered=true" in result and "farmer_type='existing'" in result
        assert "Do not ask for name or gender" in result
    assert "lookup_khc_reference" not in result and "own words" not in result


@pytest.mark.parametrize("name,gender,expected", [
    ("", "", "name"),
    ("Test Farmer", "", "gender"),
    ("", "F", "name"),
    ("Test Farmer", "unknown", "gender"),
])
def test_new_farmer_collects_only_missing_profile_field_without_submitting(monkeypatch, name, gender, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    result = asyncio.run(khc.register_khc_grievance(
        ctx(), **registration(farmer_type="new", farmer_name=name, gender=gender),
    ))
    assert expected in result
    assert ("name" if expected == "gender" else "gender") not in result
    call.assert_not_called()


def test_reference_results_say_to_match_first_and_list_only_when_unmatched(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": '[{"scheme":38,"schemename":"Agriculture Infrastructure Fund (AIF)","division":31}]',
        "total": "1", "page": "1",
    })), [])
    output = asyncio.run(khc.lookup_khc_reference(ctx(), "schemes", 10))
    assert "Keep the grievance flow's order" in output
    assert "ignoring case, spacing and small spelling differences (uttarpradesh is Uttar Pradesh)" in output
    assert "If one matches, use it and do not ask again" in output
    assert "Show names to choose from only when none or several match" in output
    assert "look at the unfiltered list and remaining pages before saying nothing matches" in output
    assert "Take the scheme and division from the same row" in output
    assert "never show IDs or ask the farmer for them" in output
    assert "Match the complaint semantically" in output
    assert "If the farmer explicitly asks to change the sub-category, show the requested options" in output
    assert khc.SOURCE in output


@pytest.mark.parametrize("kind,parent,options", [
    ("schemes", 9, '[{"scheme":38,"schemename":"Agriculture Infrastructure Fund (AIF)","division":31}]'),
    ("sub-categories", 39, '[{"problemsubcategory":1725,"subcategoryname":"Bank refused loan"}]'),
    ("sub-categories", 39, "[]"),
])
def test_reference_results_never_pick_a_scheme_or_sub_category_for_the_farmer(
    monkeypatch, kind, parent, options,
):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": options, "total": "0" if options == "[]" else "1", "page": "1",
    })), [])
    output = asyncio.run(khc.lookup_khc_reference(ctx(), kind, parent))
    assert "A failed lookup does not mean a scheme is unsupported" in output
    assert "Never invent a category, sub-category or ID" in output
    assert "Match the complaint semantically" in output
    assert "Pick the single best-fit" not in output


@pytest.mark.parametrize("options,total,page", [
    ('[{"subdistrictid":"1067","subdistrictname":"Test Tehsil"}]', "1", "1"),
    ('[{"subdistrictid":"1068","subdistrictname":"Another Tehsil"}]', "51", "2"),
    ("[]", "0", "1"),
])
def test_location_reference_results_do_not_make_sub_district_a_step(
    monkeypatch, options, total, page,
):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": options, "total": total, "page": page,
    })), calls)
    output = asyncio.run(khc.lookup_khc_reference(
        ctx(), "sub-districts", 221, page=int(page),
    ))
    rendered_options = output.split(" options: ", 1)[1].split("\n", 1)[0]
    assert json.loads(rendered_options) == json.loads(options)
    assert f"Total matches: {total}; page: {page}" in output
    assert "Do not list sub-districts as a step: ask for the village first" in output
    assert "offer the next pages if there are more" in output
    assert "Show names to choose from only when none or several match" in output
    assert len(calls) == 1


def test_reference_hierarchy_ids_are_forwarded_to_registration_payload(monkeypatch):
    import json

    reference_rows = [
        ("states", None, {"stateid": 10, "statename": "Bihar"}),
        ("districts", 10, {"districtid": 221, "districtname": "Test District"}),
        ("sub-districts", 221, {"subdistrictid": "1067", "subdistrictname": "Test Sub-district"}),
        ("villages", 1067, {"villageid": 219159, "villagename": "Test Village"}),
        ("schemes", 10, {"scheme": 38, "schemename": "Agriculture Infrastructure Fund (AIF)", "division": 31}),
        ("categories", 38, {"problemcategory": 39, "categoryname": "Loan"}),
        ("sub-categories", 39, {"problemsubcategory": 1725, "subcategoryname": "Bank refused loan"}),
    ]
    responses = [envelope("init", "khc_reference", {
        "options": json.dumps([row]), "total": "1", "page": "1",
    }) for _, _, row in reference_rows] + [envelope("init", "grievance_created")]
    calls = []
    real_client = khc.httpx.AsyncClient

    def respond(request):
        calls.append(json.loads(request.content))
        return khc.httpx.Response(200, json=responses.pop(0))

    monkeypatch.setenv("BAP_ENDPOINT", "https://bap.test")
    monkeypatch.setattr(khc.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=khc.httpx.MockTransport(respond), **kwargs,
    ))
    resolved = {}
    for kind, parent, _ in reference_rows:
        output = asyncio.run(khc.lookup_khc_reference(ctx(), kind, parent))
        options = json.loads(output.split(" options: ", 1)[1].split("\n", 1)[0])
        resolved[kind] = options[0]

    output = asyncio.run(khc.register_khc_grievance(ctx(), **registration(
        state_id=resolved["states"]["stateid"],
        village_id=resolved["villages"]["villageid"],
        scheme_id=resolved["schemes"]["scheme"],
        division_id=resolved["schemes"]["division"],
        problem_category_id=resolved["categories"]["problemcategory"],
        problem_subcategory_id=resolved["sub-categories"]["problemsubcategory"],
    )))
    assert "BR/240926/2" in output
    for call, (kind, parent, _) in zip(calls, reference_rows):
        tags = {
            tag["descriptor"]["code"]: tag["value"]
            for tag in call["message"]["order"]["fulfillments"][0]["customer"]["person"]["tags"]
        }
        assert tags["reference_type"] == kind
        assert tags.get("parent_id") == (str(parent) if parent is not None else None)
        assert "name" not in tags  # No full complaint passed as a substring filter.
    submitted = {
        tag["descriptor"]["code"]: tag["value"]
        for tag in calls[-1]["message"]["order"]["fulfillments"][0]["customer"]["person"]["tags"]
    }
    assert {key: submitted[key] for key in khc._MASTER_FIELDS} == {
        "stateId": "10", "villageId": "219159", "scheme": "38", "division": "31",
        "problemCategory": "39", "problemSubCategory": "1725",
    }


@pytest.mark.parametrize("kind,parent,page,name,expected", [
    ("unknown", None, 1, "", "valid KHC"), ("states", None, 0, "", "valid KHC"),
    ("schemes", None, 1, "", "parent stateId"), ("villages", True, 1, "", "parent subDistrictId"),
    ("states", None, 1, "x" * 201, "200 characters"),
])
def test_reference_validation(monkeypatch, kind, parent, page, name, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.lookup_khc_reference(ctx(), kind, parent, name, page))
    call.assert_not_called()


def test_reference_lookup_renders_verified_options(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": '[{"scheme":38,"schemename":"AIF","division":31}]',
        "total": "1", "page": "1",
    })), [])
    output = asyncio.run(khc.lookup_khc_reference(ctx(), "schemes", 10, "AIF"))
    assert '"division": 31' in output and khc.SOURCE in output


def test_reference_malformed_options_do_not_reach_farmer(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": "null",
    })), [])
    assert "unavailable" in asyncio.run(khc.lookup_khc_reference(ctx(), "states"))


def test_otp_failure_does_not_claim_grievance_submission(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(500), [])
    output = asyncio.run(khc.initiate_khc_otp(ctx(), "9876543210"))
    assert "unavailable" in output and "submission" not in output


def _tags(call):
    return {
        tag["descriptor"]["code"]: tag["value"]
        for tag in call["message"]["order"]["fulfillments"][0]["customer"]["person"]["tags"]
    }


@pytest.mark.parametrize("place,state,district", [("", "", ""), ("a" * 201, "", ""), ("x", "s" * 101, "")])
def test_find_location_validates_input_without_http(monkeypatch, place, state, district):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert "village or place name" in asyncio.run(khc.find_khc_location(ctx(), place, state, district))
    call.assert_not_called()


def test_find_location_sends_known_details_and_reports_one_village(monkeypatch):
    import json

    village = {
        "level": "village", "id": 5, "name": "Abupur",
        "state": {"id": 9, "name": "Uttar Pradesh"}, "district": {"id": 221, "name": "Meerut"},
        "subDistrict": {"id": 1067, "name": "Meerut Block"},
    }
    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps([village]), "total": "1",
    })), calls)
    output = asyncio.run(khc.find_khc_location(ctx(), " Abupur ", "uttarpredesh", ""))
    assert _tags(calls[0]) == {
        "request_type": "find_location", "name": "Abupur", "state_name": "uttarpredesh",
    }
    assert "Abupur - Meerut Block, Meerut, Uttar Pradesh" in output
    assert "stateId=9, districtId=221, subDistrictId=1067, villageId=5" in output
    assert "do not ask the farmer for state, district, sub-district or village again" in output
    assert khc.SOURCE in output


@pytest.mark.parametrize("needs,expected", [
    ("state", "the scheme comes before the state"),
    ("district", "Can you please share your district name"),
    ("geocode", "forward_geocode"),
])
def test_find_location_without_a_match_says_what_to_ask(monkeypatch, needs, expected):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": "[]", "needs": json.dumps(needs),
    })), [])
    assert expected in asyncio.run(khc.find_khc_location(ctx(), "nowhere"))


def test_find_location_offers_sub_districts_when_several_villages_share_the_name(monkeypatch):
    import json

    base = {"level": "village", "name": "Abupur", "state": {"id": 9, "name": "Uttar Pradesh"}}
    rows = [{**base, "id": 5, "district": {"id": 221, "name": "Meerut"}, "subDistrict": {"id": 1, "name": "Meerut Block"}},
            {**base, "id": 7, "district": {"id": 222, "name": "Ghaziabad"}, "subDistrict": {"id": 2, "name": "Loni"}}]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps(rows), "total": "2",
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "abupur", "Uttar Pradesh"))
    assert "Could you please tell me which sub-district you are from?" in output
    assert "- Abupur: Meerut Block sub-district, Meerut district" in output  # different districts: show both
    assert "- Abupur: Loni sub-district, Ghaziabad district" in output
    assert "block" not in output.lower().replace("meerut block", "")
    assert "call this tool again with place='abupur', the same state and district, and sub_district=<their pick>" in output
    assert "do not ask for the village again" in output
    assert "villageId=5" in output and "villageId=7" in output
    assert "list was cut" not in output


def test_find_location_asks_for_district_only_when_the_list_was_cut(monkeypatch):
    import json

    base = {"level": "village", "name": "Rampur", "state": {"id": 9, "name": "UP"},
            "district": {"id": 221, "name": "Meerut"}}
    rows = [{**base, "id": i, "subDistrict": {"id": i, "name": f"B{i}"}} for i in range(10)]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps(rows), "total": "51",
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "rampur", "Uttar Pradesh"))
    assert "The list was cut" in output
    assert "type their sub-district name" in output and "as sub_district" in output


def test_find_location_malformed_result_is_not_trusted(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": "not json",
    })), [])
    assert "unavailable" in asyncio.run(khc.find_khc_location(ctx(), "abupur"))


@pytest.mark.parametrize("state,scheme,expected", [
    ("", "", "no state has been given"),
    ("s" * 101, "", "too long"),
    ("Bihar", "x" * 101, "too long"),
])
def test_grievance_options_validates_input_without_http(monkeypatch, state, scheme, expected):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    assert expected in asyncio.run(khc.get_khc_grievance_options(ctx(), state, scheme))
    call.assert_not_called()


def test_grievance_options_returns_categories_and_sub_categories_for_a_complaint(monkeypatch):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "Agriculture Infrastructure Fund (AIF)", "division": 31}),
        "categories": json.dumps([{
            "id": 39, "name": "Bank Loan Sanction Issues",
            "subCategories": [{"id": 1725, "name": "Bank refused loan"}],
        }]),
    })), calls)
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "A.I.F.", "my vehicle was stolen"))
    assert _tags(calls[0]) == {
        "request_type": "grievance_tree", "state_name": "Uttar Pradesh", "scheme_name": "A.I.F.",
        "concern": "my vehicle was stolen",
    }
    assert "schemeId=38, divisionId=31" in output
    assert "Match the complaint semantically" in output
    assert '"name": "Bank Loan Sanction Issues"' in output
    assert '"id": 39' in output and '"id": 1725' in output
    assert '"name": "Bank refused loan"' in output
    assert "Internal KHC catalogue (not for display)" in output


def test_grievance_options_without_a_complaint_shows_no_categories(monkeypatch):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "categories": json.dumps([{"id": 39, "name": "Bank Loan Sanction Issues",
                                   "subCategories": [{"id": 1725, "name": "Bank refused loan"}]}]),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "AIF"))
    assert "Nothing to show yet" in output
    assert "Do not show any list, and do not ask anything because of this message" in output
    assert "ask the next step that is not done yet" in output
    assert "Bank Loan Sanction Issues" not in output and "Bank refused loan" not in output
    assert "problemCategoryId" not in output


def test_grievance_options_does_not_list_schemes_before_the_farmer_names_one(monkeypatch):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}), "needs": json.dumps("scheme"),
        "match": json.dumps("unnamed"),
        "schemes": json.dumps([{"id": 38, "name": "AIF", "division": 31}]),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh"))
    assert "has not named a scheme yet" in output and "Do not show a list" in output
    assert "AIF" not in output
    assert 'reference_type="schemes", parent_id=9' in output


def test_grievance_options_lists_only_the_schemes_that_matched(monkeypatch):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}), "needs": json.dumps("scheme"),
        "match": json.dumps("several"),
        "schemes": json.dumps([{"id": 38, "name": "AIF", "division": 31}]),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "a"))
    assert "More than one scheme matched" in output and "- AIF" in output


def test_find_location_lists_districts_when_the_district_did_not_match(monkeypatch):
    import json

    state = {"id": 9, "name": "Uttar Pradesh"}
    rows = [{"level": "district", "id": 221, "name": "Ghaziabad"}, {"level": "district", "id": 222, "name": "Ghazipur"}]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "needs": json.dumps("district"), "state": json.dumps(state), "matches": json.dumps(rows),
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "abupur", "Uttar Pradesh", "ghazi"))
    assert "did not match exactly, or matched more than one" in output
    assert "- Ghaziabad" in output and "- Ghazipur" in output
    assert "Then call this tool again with their pick as district" in output


def test_khc_tools_are_registered_with_the_voice_agent():
    from agents.tools import TOOLS

    assert {
        "initiate_khc_otp", "verify_khc_otp", "lookup_khc_reference", "find_khc_location",
        "get_khc_grievance_options", "check_khc_grievance_status", "register_khc_grievance",
    } <= {tool.name for tool in TOOLS}


def test_rate_limited_message_reaches_the_model(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope(
        "init", "rate_limited", {}, message="Please wait 20 seconds before asking for a new OTP.",
    )), [])
    assert asyncio.run(khc.initiate_khc_otp(ctx(), "9876543210")) == (
        "Please wait 20 seconds before asking for a new OTP."
    )


def test_scheme_not_offered_by_khc_is_explained(monkeypatch):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}), "needs": json.dumps("scheme"),
        "match": json.dumps("none"),
        "schemes": json.dumps([{"id": 38, "name": "AIF", "division": 31}]),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "Rythu Bandhu"))
    assert "'Rythu Bandhu' is not a scheme KHC takes complaints for in this state" in output
    assert "nearest agriculture office" in output and "Do not pick a scheme for them" in output
    assert "- AIF" in output


def test_place_not_found_gives_sub_district_and_village_fallback_with_the_district_id(monkeypatch):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": "[]", "needs": json.dumps("geocode"), "district": json.dumps({"id": 221, "name": "Meerut"}),
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "nowhere", "Uttar Pradesh", "Meerut"))
    assert 'reference_type="sub-districts", parent_id=221' in output
    assert "Could you please tell me which sub-district you are from?" in output
    assert "write their real village name in the complaint text" in output


def test_tree_shows_the_suggested_match_and_how_to_change_it(monkeypatch):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "suggested": json.dumps({"category": {"id": 39, "name": "Bank Loan Sanction Issues"},
                                 "subCategory": {"id": 1725, "name": "Bank refused loan"}}),
        "categories": json.dumps([{"id": 39, "name": "Bank Loan Sanction Issues",
                                   "subCategories": [{"id": 1725, "name": "Bank refused loan"},
                                                     {"id": 1726, "name": "Loan sanction delay"}]}]),
    })), calls)
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "AIF", "  bank   refused my loan "))
    assert _tags(calls[0])["concern"] == "bank refused my loan"
    assert '"name": "Bank Loan Sanction Issues"' in output
    assert '"name": "Bank refused loan"' in output and '"id": 1725' in output
    assert "Complaint: bank refused my loan" in output
    assert "Show the issue summary, scheme, location, category and mapped sub-category name" in output
    assert "confirm or change the sub-category" in output
    assert "do not show the sub-category" not in output
    assert "If both match clearly, do not show a list or ask the farmer to select again" in output
    assert "Loan sanction delay" in output  # The LLM sees alternatives; the farmer does not get a menu.
    assert 'lookup_khc_reference(reference_type="categories", parent_id=38, name="")' in output
    assert 'reference_type="sub-categories"' in output
    assert "each returned category ID" in output
    assert "all pages" in output
    assert "Update both the category and sub-category IDs" in output
    assert "ask for confirmation again" in output


@pytest.mark.parametrize("concern", ["loan refused", "loan denied", "loan rejected", "rain damaged my crop"])
def test_tree_without_a_keyword_match_gives_the_llm_the_catalogue_before_any_menu(monkeypatch, concern):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "categories": json.dumps([{"id": 731, "name": "Loan assessment grievances",
                                   "subCategories": [{"id": 9423, "name": "Finance declined by lender"}]}]),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "AIF", concern))
    assert f"Complaint: {concern}" in output
    assert "Match the complaint semantically" in output
    assert "Do not treat a missing keyword suggestion as a failed semantic match" in output
    assert "Only if no category clearly fits semantically" in output
    assert "Never invent a category, sub-category or ID" in output
    catalogue = output.split("Internal KHC catalogue (not for display):\n", 1)[1].split(f"\n{khc.SOURCE}", 1)[0]
    assert json.loads(catalogue) == [{
        "id": 731, "name": "Loan assessment grievances",
        "subCategories": [{"id": 9423, "name": "Finance declined by lender"}],
    }]
    assert "Bank Loan Sanction Issues" not in output
    assert "call this tool again with the same concern and category=<their pick>" in output


@pytest.mark.parametrize("categories", [
    [], None, [{}], [{"id": 39, "name": "Issues", "subCategories": None}],
])
def test_empty_or_malformed_catalogue_reports_failure_instead_of_guessing(monkeypatch, categories):
    import json

    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "categories": json.dumps(categories),
    })), [])
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "AIF", "loan refused"))
    assert "unavailable" in output
    assert "Internal KHC catalogue" not in output


@pytest.mark.parametrize("changes", [
    {"id": True}, {"id": 0}, {"id": 9007199254740992}, {"name": None}, {"name": " "},
])
@pytest.mark.parametrize("level", ["category", "sub-category"])
def test_catalogue_rejects_invalid_ids_and_names(monkeypatch, changes, level):
    import json

    category = {"id": 39, "name": "Issues", "subCategories": [{"id": 1725, "name": "Refused loan"}]}
    option = category if level == "category" else category["subCategories"][0]
    option.update(changes)
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "categories": json.dumps([category]),
    })), [])
    assert "unavailable" in asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "AIF", "loan refused"))


def test_complaint_longer_than_500_characters_is_rejected_without_http(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    asyncio.run(khc.get_khc_grievance_options(ctx(), "Bihar", "AIF", "x" * 501))
    call.assert_not_called()


def test_find_location_sends_the_picked_sub_district(monkeypatch):
    import json

    calls = []
    village = {"level": "village", "id": 8, "name": "Abupur", "state": {"id": 9, "name": "Uttar Pradesh"},
               "district": {"id": 222, "name": "Ghaziabad"}, "subDistrict": {"id": 2001, "name": "Modinagar"}}
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps([village]), "total": "1",
    })), calls)
    output = asyncio.run(khc.find_khc_location(ctx(), "abupur", "Uttar Pradesh", "", " Modinagar "))
    assert _tags(calls[0]) == {
        "request_type": "find_location", "name": "abupur", "state_name": "Uttar Pradesh",
        "sub_district_name": "Modinagar",
    }
    assert "Found one place" in output and "villageId=8" in output
    assert "Still ask" not in output


@pytest.mark.parametrize("place,state", [("uttar pradesh", "Uttar Pradesh"), ("UttarPradesh", "uttar pradesh")])
def test_find_location_refuses_the_state_name_as_a_place(monkeypatch, place, state):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    output = asyncio.run(khc.find_khc_location(ctx(), place, state))
    assert "That is the state, not a village" in output
    assert "Can you please share your district name" in output and "Can you please share the name of your village" in output
    call.assert_not_called()


def test_a_district_and_sub_district_of_the_same_name_are_not_listed_as_villages(monkeypatch):
    import json

    state = {"id": 9, "name": "Uttar Pradesh"}
    district = {"id": 222, "name": "Ghaziabad"}
    rows = [
        {"level": "district", "id": 222, "name": "Ghaziabad", "state": state, "district": district, "subDistrict": None},
        {"level": "sub-district", "id": 2001, "name": "Ghaziabad", "state": state, "district": district,
         "subDistrict": {"id": 2001, "name": "Ghaziabad"}},
    ]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps(rows), "total": "2",
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "ghaziabad", "Uttar Pradesh"))
    assert "is the name of a district or sub-district, not a village" in output
    assert "ask 'Can you please share the name of your village'" in output
    assert "Do not list these as villages" in output
    assert "Could you please tell me which sub-district you are from?" not in output


def test_a_village_match_beats_a_district_of_the_same_name(monkeypatch):
    import json

    state = {"id": 9, "name": "Uttar Pradesh"}
    district = {"id": 222, "name": "Ghaziabad"}
    rows = [
        {"level": "district", "id": 222, "name": "Ghaziabad", "state": state, "district": district, "subDistrict": None},
        {"level": "village", "id": 5, "name": "Ghaziabad", "state": state, "district": {"id": 9, "name": "Meerut"},
         "subDistrict": {"id": 1, "name": "Daurala"}},
    ]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps(rows), "total": "2",
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "ghaziabad", "Uttar Pradesh"))
    assert "Found one place" in output and "villageId=5" in output
    assert "districtId=222" not in output


def test_find_location_refuses_the_district_name_as_a_place(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    output = asyncio.run(khc.find_khc_location(ctx(), "Meerut", "Uttar Pradesh", "meerut"))
    assert "That is the district, not a village" in output
    assert "Can you please share the name of your village" in output
    call.assert_not_called()


def test_sub_district_options_omit_the_district_when_they_share_one(monkeypatch):
    import json

    base = {"level": "village", "name": "Abupur", "state": {"id": 9, "name": "Uttar Pradesh"},
            "district": {"id": 222, "name": "Ghaziabad"}}
    rows = [{**base, "id": 5, "subDistrict": {"id": 1, "name": "Loni"}},
            {**base, "id": 7, "subDistrict": {"id": 2, "name": "Modinagar"}}]
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_location", {
        "matches": json.dumps(rows), "total": "2",
    })), [])
    output = asyncio.run(khc.find_khc_location(ctx(), "abupur", "Uttar Pradesh", "Ghaziabad"))
    assert "- Abupur: Loni sub-district\n" in output
    assert "- Abupur: Modinagar sub-district\n" in output
    assert "sub-district, Ghaziabad district" not in output


@pytest.mark.parametrize("category_picked", [False, True])
@pytest.mark.parametrize("model_concern", ["", "bank loan sanction issue", "credit guarantee coverage"])
def test_category_scoped_catalogue_preserves_the_original_concern(
    monkeypatch, category_picked, model_concern,
):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 38, "name": "AIF", "division": 31}),
        "suggested": json.dumps({"category": {"id": 39, "name": "Bank Loan Sanction Issues"}, "subCategory": None}),
        "categories": json.dumps([
            {"id": 39, "name": "Bank Loan Sanction Issues", "subCategories": [
                {"id": 1725, "name": "Bank refused loan"}, {"id": 1726, "name": "Loan sanction delay"}]},
        ] + ([] if category_picked else [
            {"id": 41, "name": "Fund Disbursement Issues", "subCategories": [{"id": 1801, "name": "Subsidy not received"}]},
        ])),
    })), calls)
    turns = [
        "i want to create grivance in khc", "AIF", "Uttar pradesh", "Ghaziabad",
        ASKED_VILLAGE, "Abupur", ASKED, "bank loan sanction issue",
    ]
    category = "Bank Loan Sanction Issues" if category_picked else ""
    if category_picked:
        turns.extend([
            ("bot", "Which one feels closest to your problem?\n- Bank Loan Sanction Issues\n- Fund Disbursement Issues"),
            category,
        ])
    output = asyncio.run(khc.get_khc_grievance_options(
        _chat(*turns), "Uttar Pradesh", "AIF", model_concern, category,
    ))
    assert len(calls) == 1
    assert _tags(calls[0])["concern"] == "bank loan sanction issue"
    assert _tags(calls[0]).get("category_name", "") == category
    assert '"name": "Bank Loan Sanction Issues"' in output
    assert '"id": 1725, "name": "Bank refused loan"' in output
    assert '"id": 1726, "name": "Loan sanction delay"' in output
    assert "Which one feels closest to your problem?" in output
    assert "If only the category is clear, show only its sub-category names" in output
    assert "Show the issue summary, scheme, location, category and mapped sub-category name" in output
    assert ("Fund Disbursement Issues" in output) is (not category_picked)
    assert ("Subsidy not received" in output) is (not category_picked)
    assert "No clear match" not in output
    assert "Nothing to show yet" not in output
    assert "Please tell me about your problem" not in output


def test_picked_category_is_sent_to_the_provider(monkeypatch):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 40, "name": "PM-KISAN", "division": 32}),
        "suggested": json.dumps({"category": {"id": 50, "name": "Delay in receiving benefit"},
                                 "subCategory": {"id": 501, "name": "Installment not received"}}),
        "categories": json.dumps([{"id": 50, "name": "Delay in receiving benefit",
                                   "subCategories": [{"id": 501, "name": "Installment not received"}]}]),
    })), calls)
    output = asyncio.run(khc.get_khc_grievance_options(
        ctx(), "Uttar Pradesh", "PM Kisan", "installment not received", " Delay in receiving benefit "))
    assert _tags(calls[0])["category_name"] == "Delay in receiving benefit"
    assert _tags(calls[0])["concern"] == "installment not received"
    assert '"id": 501, "name": "Installment not received"' in output
    assert "mapped sub-category name" in output


def _chat(*turns):
    """A ctx whose history holds these turns: a plain string is the farmer, ("bot", text) is the bot."""
    messages = []
    for turn in turns:
        who, text = ("farmer", turn) if isinstance(turn, str) else turn
        part = (SimpleNamespace(part_kind="user-prompt", content=f'**User:** "{text}"\n**Selected Language:** English')
                if who == "farmer" else SimpleNamespace(part_kind="text", content=text))
        messages.append(SimpleNamespace(parts=[part]))
    return SimpleNamespace(deps=SimpleNamespace(session_id="session-1", question_id="q1"), messages=messages)


ASKED = ("bot", "Please tell me about your problem in your own words. I am here to help.\n\n**Source: Kisan Helpline Center**")
ASKED_VILLAGE = ("bot", "Can you please share the name of your village\n\n**Source: Kisan Helpline Center**")


def _tree_http(monkeypatch):
    import json

    calls = []
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_grievance_tree", {
        "state": json.dumps({"id": 9, "name": "Uttar Pradesh"}),
        "scheme": json.dumps({"id": 40, "name": "PM-KISAN", "division": 32}),
        "categories": json.dumps([{"id": 50, "name": "Delay in receiving benefit",
                                   "subCategories": [{"id": 501, "name": "Installment not received"}]}]),
    })), calls)
    return calls


@pytest.mark.parametrize("invented", ["benefit not received", "PM Kisan grievance", "complaint regarding PM-KISAN"])
def test_a_concern_the_farmer_never_said_is_refused_without_http(monkeypatch, invented):
    calls = _tree_http(monkeypatch)
    chat = _chat("i want to create grivance with KHC", "pm kisan", "Uttar pradesh", "Ghaziabad", "abupur")
    output = asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "PM Kisan", invented))
    assert "the farmer has not described their problem in their own words" in output
    assert "Do not show any list" in output
    assert "Please tell me about your problem in your own words" in output
    assert calls == []


@pytest.mark.parametrize("said,concern", [
    ("installment not recieved", "installment not received"),  # typo in the farmer's words
    ("my installment is not received since 6 months", "installment not received"),
])
def test_a_concern_from_the_farmers_own_words_goes_through(monkeypatch, said, concern):
    calls = _tree_http(monkeypatch)
    chat = _chat("i want to create grivance with KHC", "pm kisan", "abupur", said)
    asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "PM Kisan", concern))
    assert len(calls) == 1 and _tags(calls[0])["concern"] == concern


def test_a_farmer_writing_another_language_is_trusted(monkeypatch):
    calls = _tree_http(monkeypatch)
    chat = _chat("मुझे KHC में शिकायत दर्ज करनी है", "मेरी किस्त नहीं आई")
    asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "PM Kisan", "installment not received"))
    assert len(calls) == 1


def test_the_check_is_skipped_when_there_is_no_chat_history(monkeypatch):
    calls = _tree_http(monkeypatch)
    asyncio.run(khc.get_khc_grievance_options(ctx(), "Uttar Pradesh", "PM Kisan", "installment not received"))
    assert len(calls) == 1


def test_category_lookups_tell_the_model_not_to_list_before_the_problem(monkeypatch):
    fake_http(monkeypatch, khc.httpx.Response(200, json=envelope("init", "khc_reference", {
        "options": '[{"problemcategory":50,"categoryname":"Delay in receiving benefit"}]', "total": "1", "page": "1",
    })), [])
    output = asyncio.run(khc.lookup_khc_reference(ctx(), "categories", 40))
    assert "Do not show these names to the farmer unless they have described their problem" in output


@pytest.mark.parametrize("model_concern", [
    "", "bank loan sanction", "loan sanction", "loan issue", "loan problem",
])
def test_the_farmers_answer_to_the_problem_question_is_used_even_if_the_model_forgets_it(monkeypatch, model_concern):
    calls = _tree_http(monkeypatch)
    chat = _chat("i want to create grivance in khc", "AIF", ASKED_VILLAGE, "abupur", ASKED, "bank loan sanction issue")
    output = asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "AIF", model_concern))
    assert len(calls) == 1
    assert _tags(calls[0])["concern"] == "bank loan sanction issue"
    assert "Nothing to show yet" not in output


def test_a_typo_in_the_farmers_answer_still_goes_through(monkeypatch):
    calls = _tree_http(monkeypatch)
    chat = _chat("khc", "AIF", ASKED, "bank laon sanction issue")
    asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "AIF", ""))
    assert _tags(calls[0])["concern"] == "bank laon sanction issue"


def test_a_translated_problem_answer_is_kept_for_matching():
    chat = _chat("khc", "PM Kisan", ASKED, "मेरी किस्त नहीं आई")
    assert khc._resolve_concern(chat, "installment not received", "PM Kisan") == "installment not received"


def test_a_later_clarification_is_not_replaced_with_the_original_problem():
    chat = _chat(
        "khc", "AIF", ASKED, "bank loan sanction issue",
        ("bot", "Which one feels closest to your problem?"),
        "Bank refused loan under AIF",
    )
    assert khc._resolve_concern(chat, "bank refused loan", "AIF") == "bank refused loan"


def test_an_earlier_problem_answer_is_recovered_after_other_questions():
    chat = _chat(
        "khc", "AIF", ASKED, "bank loan sanction issue",
        ASKED_VILLAGE, "Abupur",
    )
    assert khc._resolve_concern(chat, "", "AIF") == "bank loan sanction issue"


def test_the_latest_problem_answer_is_used_after_category_selection():
    chat = _chat(
        "khc", "AIF", ASKED, "bank loan sanction issue",
        ASKED, "  bank   refused my loan  ",
        ("bot", "Which one feels closest to your problem?\n- Bank Loan Sanction Issues"),
        "Bank Loan Sanction Issues",
    )
    assert khc._resolve_concern(chat, "", "AIF") == "bank refused my loan"


def test_a_category_pick_without_a_problem_answer_is_not_used_as_the_concern():
    chat = _chat(
        "khc", "AIF", ASKED_VILLAGE, "Abupur",
        ("bot", "Which one feels closest to your problem?\n- Bank Loan Sanction Issues"),
        "Bank Loan Sanction Issues",
    )
    assert khc._resolve_concern(chat, "", "AIF") is None


def test_the_village_answer_is_never_taken_as_the_problem(monkeypatch):
    calls = _tree_http(monkeypatch)
    chat = _chat("khc", "AIF", ASKED_VILLAGE, "abupur")
    refused = asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "AIF", "bank loan not sanctioned"))
    assert "Nothing to show yet" in refused and "Do not show any list" in refused
    assert calls == []


def test_a_reply_in_another_language_to_the_problem_question_is_used(monkeypatch):
    calls = _tree_http(monkeypatch)
    chat = _chat("मुझे शिकायत करनी है", ASKED, "मेरी किस्त नहीं आई")
    asyncio.run(khc.get_khc_grievance_options(chat, "Uttar Pradesh", "PM Kisan", ""))
    assert _tags(calls[0])["concern"] == "मेरी किस्त नहीं आई"


def test_a_tool_called_with_no_state_never_asks_for_the_state_first(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(khc, "_call", call)
    output = asyncio.run(khc.get_khc_grievance_options(ctx(), "", "AIF"))
    assert "Nothing to look up yet: no state has been given" in output
    assert "Do not ask anything because of this message" in output
    assert "the scheme comes before the state" in output
    assert "Which state" not in output and "share the name of your state" not in output
    call.assert_not_called()


@pytest.mark.parametrize("opening", [
    "i want to create grivance with KHC",
    "I want to raise a complaint in Kisan Helpline Center",
    "grievance via Kisan Help Centre please",
    "register complaint k.h.c.",
    "i want to create grivance with kisan heliline center for pm KIsan",
    "Create a PM-Kisan grievance through Kisan HELILINE Centre",
    "File a complaint through Kisan Help-Line Center",
])
def test_the_service_note_is_added_once_the_farmer_asked_for_khc(opening):
    chat = _chat(opening, ("bot", "Could you please tell me which scheme your complaint is about?"), "AIF")
    note = khc.khc_service_note(chat.messages)
    assert "## Service already chosen" in note
    assert "do not ask which service to use" in note
    assert "including PM-Kisan, PMFBY and AIF" in note


@pytest.mark.parametrize("later_turns", [
    (),
    (("bot", "Please share the name of your state"), "Uttar Pradesh"),
])
def test_explicit_khc_pm_kisan_request_does_not_mix_workflows(later_turns):
    chat = _chat(
        "i want to create grivance with kisan heliline center for pm KIsan",
        *later_turns,
    )
    note = khc.khc_service_note(chat.messages)
    assert "Never ask for a PM-Kisan registration number" in note
    assert "Do not combine KHC and scheme-service questions" in note
    assert "If the scheme is already named, skip the scheme question" in note
    assert "Ask only the first missing KHC detail" in note
    assert "These KHC rules override the general follow-up and source-order rules" in note
    assert "put the source last with nothing after it" in note


@pytest.mark.parametrize("messages", [
    ["I want to file a grievance for PM-Kisan", "AIF"],
    ["scheme khcx information", "tell me about khcl"],
    ["PM Kisan grievance at a Kisan health center"],
    ["kisan heliline centerpiece", "scheme k.h.cl information"],
    [],
])
def test_the_service_lock_is_absent_when_khc_was_never_asked_for(messages):
    assert "## Service already chosen" not in khc.khc_service_note(_chat(*messages).messages)


def test_khc_in_the_bots_own_words_does_not_count_as_the_farmers_choice():
    chat = _chat("grievance for PM-Kisan", ("bot", "Would you like Kisan Helpline Center or the PM-Kisan service?"), "hmm")
    assert "## Service already chosen" not in khc.khc_service_note(chat.messages)


@pytest.mark.parametrize("turns", [
    (),
    ("i want to know my grievance status",),
    ("check my complaint status",),
    ("i want to know my grievance status",
     ("bot", "Could you please share your grievance number?\nSource: Kisan Helpline Center")),
    ("check grievance 123456789012",),
])
def test_status_routing_gate_does_not_assume_khc(turns):
    note = khc.khc_service_note(_chat(*turns).messages)
    assert "## Grievance status routing" in note
    assert "Could you please tell me which scheme or service your grievance is with?" in note
    assert "Do not ask for a grievance number, registration number, mobile number or OTP until the route is known" in note
    assert "Do not show any service source label while clarifying the route" in note
    assert "A bare grievance number or the bot's own KHC wording does not select KHC" in note
    assert "## Service already chosen" not in note


@pytest.mark.parametrize("scheme,tool", [
    ("PM-Kisan", "pmkisan_grievance_send_otp"),
    ("PMFBY", "pmfby_grievance_status"),
    ("AIF", "initiate_aif_otp"),
    ("KHC", "check_khc_grievance_status"),
])
def test_status_routing_gate_preserves_direct_named_routes(scheme, tool):
    note = khc.khc_service_note(_chat(f"Check my {scheme} grievance status").messages)
    assert tool in note
    assert "Do not ask which scheme or service again when it is already known" in note
    assert "An explicit KHC choice for this grievance overrides the scheme default" in note
    assert "KHC status needs only the grievance number, not the scheme, location, mobile number or OTP" in note


@pytest.mark.parametrize("later_request", [
    "i want to know my grievance status",
    "Check a different PM-Kisan grievance",
    "Check my PMFBY grievance status instead",
    "Switch to the AIF grievance service",
])
def test_old_khc_choice_is_not_a_chat_wide_status_lock(later_request):
    note = khc.khc_service_note(_chat("Create a grievance through KHC", later_request).messages)
    assert "Apply the earlier KHC choice only to that same grievance" in note
    assert "An unrelated earlier KHC mention is not a choice for a new grievance or status request" in note
    assert "Honor an explicit switch away from KHC" in note


def test_khc_status_tool_requires_known_route_before_asking_for_number():
    assert "Use only after KHC is identified as the service for this grievance" in khc.check_khc_grievance_status.__doc__
    assert "ask which scheme or service first" in khc.check_khc_grievance_status.__doc__


def test_the_system_prompt_gets_the_note_on_every_turn():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "agents" / "voice.py").read_text()
    assert "from agents.tools.khc import khc_service_note" in source
    assert "+ khc_service_note(ctx.messages)" in source
