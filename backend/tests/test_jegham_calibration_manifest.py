import pytest

from tret.services.emissions import JEGHAM_2025


def test_manifest_preserves_legacy_runtime_view_and_exact_source_observations():
    manifest = JEGHAM_2025["manifest"]
    assert manifest["calibration_id"] == "jegham_2025_v1"
    assert manifest["source"]["version"] == "v1"
    assert manifest["source"]["license"] == "CC BY 4.0"
    assert manifest["source"]["underlying_api_data_license"] == "unknown"
    assert JEGHAM_2025["wh_per_query"]["GPT-4o"] == (0.42, 1.21, 1.79)
    assert manifest["observations"]["GPT-4o"]["facility_wh_mean"] == [0.421, 1.214, 1.788]


@pytest.mark.parametrize("model", [
    "GPT-4.1 nano", "GPT-4o", "Claude 3.7 Sonnet", "o3", "DeepSeek-R1",
])
def test_node_it_values_are_the_exact_means_divided_by_provider_pue(model):
    manifest = JEGHAM_2025["manifest"]
    row = manifest["observations"][model]
    pue = manifest["providers"][row["provider"]]["pue"]
    assert row["node_it_wh_mean"] == pytest.approx(
        [value / pue for value in row["facility_wh_mean"]], rel=1e-9
    )


def test_manifest_records_boundary_and_o3_pue_limitations():
    manifest = JEGHAM_2025["manifest"]
    assert manifest["boundary"]["energy_boundary"] == "facility"
    assert manifest["boundary"]["gpu_configuration_evidence"] == "inferred"
    assert manifest["boundary"]["output_reasoning_denominator"] == "unverified"
    assert (
        manifest["observations"]["o3"]["pue_classification_basis"]
        == "inherited_group_row"
    )
