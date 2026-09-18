from decimal import Decimal as D
import pytest

from tret.services.embodied_profiles import HardwareFootprint, ProfileError, allocate_by_time
from tret.services.emissions_coverage import ComponentCoverage, coverage_envelope


def test_full_lifetime_recovers_footprint_with_resource_shares():
    component = HardwareFootprint("gpu", D(1000), D(100), "supplier", "e1")
    first = allocate_by_time([component], duration_s=D(100), share_by_component={"gpu": D('.4')})
    second = allocate_by_time([component], duration_s=D(100), share_by_component={"gpu": D('.6')})
    assert first["complete_total_g"] + second["complete_total_g"] == 1000
    assert "grid" not in first


def test_unknown_footprint_stays_unknown_and_duplicate_supplier_rejected():
    component = HardwareFootprint("gpu", None, D(100), "supplier", "e1")
    assert allocate_by_time([component], duration_s=D(1), share_by_component={"gpu": D(1)})["complete_total_g"] is None
    with pytest.raises(ValueError, match="already includes"):
        allocate_by_time([component], duration_s=D(1), share_by_component={}, supplier_includes_hardware=True)


def test_time_allocation_rejects_duration_beyond_service_life():
    component = HardwareFootprint("gpu", D(10), D(100), "supplier", "e1")
    with pytest.raises(ProfileError, match="exceeds service life"):
        allocate_by_time(
            [component], duration_s=D(101), share_by_component={"gpu": D(1)}
        )


def test_unknown_component_withholds_total_and_never_invents_zero():
    components = [ComponentCoverage("energy", "operation", "modeled", D(2), "gCO2e", "node_it", ("model-v2",)),
                  ComponentCoverage("hardware", "manufacture", "unknown", None, "gCO2e", "hardware")]
    result = coverage_envelope(components, required=["energy", "hardware"], functional_unit="run")
    assert result["covered_subtotal"] == {"gCO2e": 2}
    assert result["complete_total"] is None
    assert result["alignment"]["conformity_claim"] is None
    with pytest.raises(ValueError):
        ComponentCoverage("hardware", "manufacture", "unknown", D(0), "gCO2e", "hardware")


def test_na_requires_applicability_evidence_and_unknown_method_cannot_attest_zero():
    from tret.services.emissions_coverage import operational_coverage
    with pytest.raises(ValueError, match="applicability"):
        ComponentCoverage("hardware", "manufacture", "not_applicable", None, "gCO2e", "hardware")
    result = operational_coverage({"co2e_g": 0, "embodied_g": 0})
    assert result["components"][0]["value"] is None
    assert result["components"][0]["status"] == "unknown"
