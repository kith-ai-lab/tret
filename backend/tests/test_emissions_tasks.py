import pytest

from tret.services.emissions_tasks import task_cohort


def activity(**extra):
    return {"activity_id": "a", "accounting_id": "a", "deliverable_id": "d", "energy_wh": 5,
            "co2e_g": 2, "status": "failed", "energy_boundary": "node_it",
            "accounting_basis": "location_based", "factor_boundary": "lifecycle",
            "gas_coverage": "co2e", "gwp_basis": "ar6", "gwp_horizon_years": 100,
            "includes_td_losses": False, "electricity_mix_basis": "production", **extra}


def cohort(items, accepted=True):
    return task_cohort(items, [{"deliverable_id": "d", "quality_gate": "review-v1",
                               "accepted": accepted}], quality_gate="review-v1")


def test_failed_attempts_stay_in_numerator_and_zero_success_is_undefined():
    report = cohort([activity(), activity(activity_id="b", accounting_id="b", status="completed")])
    assert report["failed_activities"] == 1
    assert report["groups"][0]["energy_wh_per_accepted_task"] == 10
    rejected = cohort([activity(co2e_g=0)], False)
    assert rejected["groups"][0]["energy_wh_per_accepted_task"] is None
    assert rejected["combined_carbon_total"] == 0


def test_duplicate_and_inclusive_parent_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        cohort([activity(), activity(activity_id="b")])
    with pytest.raises(ValueError, match="double count"):
        cohort([activity(aggregation_mode="inclusive"),
                activity(activity_id="b", accounting_id="b", parent_id="a")])


def test_unknown_or_mixed_bases_do_not_publish_combined_carbon():
    assert cohort([activity(gwp_basis="unknown")])["combined_carbon_total"] is None
    report = cohort([activity(), activity(activity_id="b", accounting_id="b", accounting_basis="market_based")])
    assert report["combined_carbon_total"] is None
    assert len(report["groups"]) == 2


def test_unknown_group_does_not_leak_a_summed_carbon_total():
    report = cohort([activity(gwp_basis="unknown"),
                     activity(activity_id="b", accounting_id="b", gwp_basis="unknown")])
    assert report["groups"][0]["co2e_g"] is None
    assert report["groups"][0]["energy_wh"] == 10


@pytest.mark.parametrize("field,value", [
    ("factor_boundary", "banana"), ("gwp_horizon_years", True),
    ("includes_td_losses", "false"), ("accounting_basis", "not-a-basis"),
])
def test_invalid_method_metadata_cannot_be_treated_as_known(field, value):
    with pytest.raises(ValueError, match=field):
        cohort([activity(**{field: value})])


def test_different_electricity_mix_or_losses_withholds_combined_total():
    report = cohort([activity(), activity(activity_id="b", accounting_id="b",
                                         electricity_mix_basis="consumption", includes_td_losses=True)])
    assert report["combined_carbon_total"] is None
    assert len(report["groups"]) == 2
