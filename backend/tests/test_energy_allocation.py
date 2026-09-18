from decimal import Decimal as D
import pytest

from tret.services.energy_allocation import EnergyObservation, WorkClaim, allocate_energy


def obs(**extra):
    return EnergyObservation(**({"observation_id": "o", "source_id": "meter", "device_id": "gpu",
                                  "start_ns": 0, "end_ns": 10, "energy_wh": D(10),
                                  "energy_boundary": "gpu", "clock_id": "host"} | extra))


def claim(ident, **extra):
    return WorkClaim(**({"claim_id": ident, "accounting_id": ident, "device_id": "gpu",
                         "start_ns": 0, "end_ns": 10, "clock_id": "host"} | extra))


def test_overlap_conserves_with_explicit_shares_and_reserve():
    result = allocate_energy([obs(reserved_wh=D(1))], [claim("a", resource_share=D('.3')),
                                                      claim("b", resource_share=D('.4'))])
    row = result["observations"][0]
    assert sum(row["allocated_wh"].values()) + row["unallocated_wh"] + row["reserved_wh"] == 10
    assert result["claims"]["a"]["complete"]


def test_ambiguous_overlap_remains_unallocated_and_incomplete():
    result = allocate_energy([obs()], [claim("a"), claim("b", start_ns=5)])
    assert result["claims"]["a"]["allocated_wh_observed"] == 5
    assert not result["claims"]["a"]["complete"]
    assert result["observations"][0]["unallocated_wh"] == 5


def test_gaps_and_duplicate_measurements_fail_closed():
    result = allocate_energy([obs(temporal_coverage=D('.9'))], [claim("a")])
    assert not result["claims"]["a"]["complete"]
    assert result["observations"][0]["unallocated_wh"] == 10
    with pytest.raises(ValueError, match="overlapping"):
        allocate_energy([obs(), obs(observation_id="o2")], [])
    with pytest.raises(ValueError, match="exceed"):
        allocate_energy([obs()], [claim("a", resource_share=D('.6')), claim("b", resource_share=D('.6'))])


def test_overlapping_node_and_gpu_observations_are_rejected_even_on_different_devices():
    node = obs(device_id="node", energy_boundary="node_it")
    gpu = obs(observation_id="gpu-o", device_id="gpu", energy_boundary="gpu")
    with pytest.raises(ValueError, match="nested-boundary"):
        allocate_energy([node, gpu], [])
